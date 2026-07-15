"""tests/test_labeling.py — Tests for meta/labeling.py forward-labeling engine."""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

# ── Helpers ──────────────────────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _ts_iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _make_candles(
    start_ts_ms: int,
    count: int,
    base_price: float = 100.0,
    interval_ms: int = 5 * 60 * 1000,
    drift: float = 0.0,
) -> list[list]:
    """Generate synthetic 5m candles: [ts_ms, o, h, l, c, v]."""
    candles = []
    price = base_price
    for i in range(count):
        ts = start_ts_ms + i * interval_ms
        o = price
        h = price * (1 + abs(drift) * 0.5 + 0.002)
        l = price * (1 - abs(drift) * 0.5 - 0.001)
        c = price * (1 + drift)
        candles.append([ts, o, h, l, c, 1000.0])
        price = c
    return candles


def _init_db(conn: sqlite3.Connection) -> None:
    """Create the decisions and decision_labels tables."""
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS agents (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'rookie',
            spawn_date TEXT NOT NULL,
            cull_date TEXT,
            config_json TEXT NOT NULL DEFAULT '{}',
            current_thesis_version INTEGER NOT NULL DEFAULT 1,
            last_model_used TEXT,
            wallet_address TEXT,
            keystore_path TEXT,
            live_enabled INTEGER DEFAULT 0,
            active_spec_version INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_id TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            decision_action TEXT NOT NULL,
            decision_reason TEXT,
            decision_details_json TEXT,
            counterfactual_result TEXT,
            counterfactual_was_better INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (agent_id) REFERENCES agents(id)
        );
        CREATE TABLE IF NOT EXISTS trades (
            id TEXT PRIMARY KEY,
            agent_id TEXT NOT NULL,
            thesis_version INTEGER NOT NULL DEFAULT 1,
            account_balance_at_entry REAL,
            mode TEXT NOT NULL DEFAULT 'paper',
            asset TEXT NOT NULL,
            direction TEXT NOT NULL,
            entry_price REAL,
            stop_loss_price REAL,
            take_profit_price REAL,
            leverage INTEGER,
            position_size_pct REAL,
            notional_usd REAL,
            true_notional REAL,
            entry_timestamp TEXT,
            exit_price REAL,
            exit_timestamp TEXT,
            exit_reason TEXT,
            duration_minutes REAL,
            pnl_pct REAL,
            pnl_usd REAL,
            result TEXT,
            status TEXT NOT NULL DEFAULT 'open',
            model_used TEXT,
            voided INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS decision_labels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            decision_id INTEGER NOT NULL,
            horizon TEXT NOT NULL,
            fwd_return_pct REAL,
            max_runup_pct REAL,
            max_drawdown_pct REAL,
            chosen_outcome_pct REAL,
            best_action TEXT,
            best_outcome_pct REAL,
            regret_pct REAL,
            labeled_at TEXT NOT NULL,
            FOREIGN KEY (decision_id) REFERENCES decisions(id)
        );
        CREATE INDEX IF NOT EXISTS idx_decision_labels_decision ON decision_labels(decision_id);
        CREATE INDEX IF NOT EXISTS idx_decision_labels_horizon ON decision_labels(horizon);
    """)
    conn.execute(
        "INSERT INTO agents (id, name, status, spawn_date) VALUES (?, ?, ?, ?)",
        ("test_agent", "Test Agent", "active", _now_iso()),
    )
    conn.commit()


def _write_candle_ledger(
    ledger_dir: str, asset: str, candles: list[list], dt: datetime
) -> None:
    """Write candles to a ledger partition."""
    from store.ledger import append_ledger_record

    for c in candles:
        append_ledger_record(
            "candles_5m",
            {"ts": c[0], "asset": asset, "o": c[1], "h": c[2], "l": c[3], "c": c[4], "v": c[5]},
            when=dt,
            ledger_dir=ledger_dir,
        )


def _insert_enter_decision(
    conn: sqlite3.Connection,
    agent_id: str,
    dt: datetime,
    asset: str,
    direction: str,
    entry_price: float,
    sl_pct: float = 0.02,
    tp_pct: float = 0.05,
) -> int:
    """Insert an enter decision with typical details structure."""
    if direction == "long":
        sl = entry_price * (1 - sl_pct)
        tp = entry_price * (1 + tp_pct)
    else:
        sl = entry_price * (1 + sl_pct)
        tp = entry_price * (1 - tp_pct)

    order = {
        "action": "enter",
        "asset": asset,
        "direction": direction,
        "entry_price": entry_price,
        "stop_loss_price": sl,
        "take_profit_price": tp,
        "leverage": 5,
        "position_size_pct": 0.1,
    }
    details = {"order": str(order), "fill": "{'trade_id': 't1'}"}

    cur = conn.execute(
        """INSERT INTO decisions
           (agent_id, timestamp, decision_action, decision_reason, decision_details_json)
           VALUES (?, ?, ?, ?, ?)""",
        (agent_id, _ts_iso(dt), "enter", f"entered {asset}", json.dumps(details)),
    )
    conn.commit()
    return cur.lastrowid


def _insert_wait_decision(
    conn: sqlite3.Connection,
    agent_id: str,
    dt: datetime,
    asset: str,
    direction: str,
    entry_price: float,
) -> int:
    """Insert a wait decision with candidate info."""
    candidate = {
        "asset": asset,
        "direction": direction,
        "entry_price": entry_price,
        "stop_loss_price": entry_price * (1 - 0.02),
        "take_profit_price": entry_price * (1 + 0.05),
    }
    details = {"candidate": candidate}

    cur = conn.execute(
        """INSERT INTO decisions
           (agent_id, timestamp, decision_action, decision_reason, decision_details_json)
           VALUES (?, ?, ?, ?, ?)""",
        (agent_id, _ts_iso(dt), "wait", "confidence too low", json.dumps(details)),
    )
    conn.commit()
    return cur.lastrowid


# ── Tests ────────────────────────────────────────────────────────────────


class TestLabelingBasic:
    """Core labeling functionality."""

    def test_import(self):
        """Module imports cleanly."""
        from meta.labeling import (
            get_labeling_coverage,
            run_labeling_job,
        )
        assert callable(run_labeling_job)
        assert callable(get_labeling_coverage)

    def test_empty_db_returns_zero(self):
        """No decisions → nothing processed."""
        conn = sqlite3.connect(":memory:")
        _init_db(conn)
        from meta.labeling import run_labeling_job

        with tempfile.TemporaryDirectory() as td:
            result = run_labeling_job(conn, td)
        assert result["total_processed"] == 0
        assert result["total_labeled"] == 0
        assert result["errors"] == 0

    def test_no_candle_data_returns_zero(self):
        """Decisions exist but no candle ledger → nothing labeled."""
        conn = sqlite3.connect(":memory:")
        _init_db(conn)
        from meta.labeling import run_labeling_job

        dt = datetime.now(timezone.utc) - timedelta(hours=48)
        _insert_enter_decision(conn, "test_agent", dt, "BTC-PERP", "long", 100000.0)

        with tempfile.TemporaryDirectory() as td:
            result = run_labeling_job(conn, td)
        assert result["total_processed"] == 0
        assert result["total_labeled"] == 0

    def test_labels_enter_decision(self):
        """An enter decision older than longest horizon gets labeled."""
        from meta.labeling import LONGEST_HOURS, run_labeling_job

        conn = sqlite3.connect(":memory:")
        _init_db(conn)

        now = datetime.now(timezone.utc)
        dec_ts = now - timedelta(hours=LONGEST_HOURS + 12)
        entry_price = 100.0

        # Write candles: entry → 30 hours forward at 5m intervals (360 candles).
        start_ms = int(dec_ts.timestamp() * 1000)
        candles = _make_candles(start_ms, 360, base_price=entry_price, drift=0.001)
        # Put a month label on the candles.
        candle_month = dec_ts.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

        _insert_enter_decision(conn, "test_agent", dec_ts, "BTC-PERP", "long", entry_price)

        with tempfile.TemporaryDirectory() as td:
            _write_candle_ledger(td, "BTC-PERP", candles, candle_month)
            result = run_labeling_job(conn, td)

        assert result["total_processed"] == 1
        assert result["total_labeled"] == 3  # 1h, 4h, 24h
        assert result["errors"] == 0

        labels = conn.execute(
            "SELECT horizon, best_action, regret_pct FROM decision_labels ORDER BY horizon"
        ).fetchall()
        assert len(labels) == 3
        horizons = [r[0] for r in labels]
        assert horizons == ["1h", "24h", "4h"]  # alphabetical

    def test_labels_wait_decision_with_candidate(self):
        """A wait decision with candidate info gets labeled."""
        from meta.labeling import LONGEST_HOURS, run_labeling_job

        conn = sqlite3.connect(":memory:")
        _init_db(conn)

        now = datetime.now(timezone.utc)
        dec_ts = now - timedelta(hours=LONGEST_HOURS + 6)
        entry_price = 200.0

        start_ms = int(dec_ts.timestamp() * 1000)
        candles = _make_candles(start_ms, 360, base_price=entry_price, drift=-0.001)
        candle_month = dec_ts.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

        _insert_wait_decision(conn, "test_agent", dec_ts, "ETH-PERP", "long", entry_price)

        with tempfile.TemporaryDirectory() as td:
            _write_candle_ledger(td, "ETH-PERP", candles, candle_month)
            result = run_labeling_job(conn, td)

        assert result["total_processed"] == 1
        assert result["total_labeled"] == 3

        # Wait decisions have chosen_action = "wait", so chosen_outcome = 0.
        labels = conn.execute(
            "SELECT chosen_outcome_pct FROM decision_labels"
        ).fetchall()
        for row in labels:
            assert row[0] == 0.0

    def test_wait_without_candidate_skipped(self):
        """Wait decision without candidate info → skipped (no asset to evaluate)."""
        from meta.labeling import LONGEST_HOURS, run_labeling_job

        conn = sqlite3.connect(":memory:")
        _init_db(conn)

        now = datetime.now(timezone.utc)
        dec_ts = now - timedelta(hours=LONGEST_HOURS + 6)

        conn.execute(
            """INSERT INTO decisions
               (agent_id, timestamp, decision_action, decision_reason, decision_details_json)
               VALUES (?, ?, ?, ?, ?)""",
            ("test_agent", _ts_iso(dec_ts), "wait", "no good setup", None),
        )
        conn.commit()

        with tempfile.TemporaryDirectory() as td:
            # Write some candles so the ledger head is valid.
            start_ms = int(dec_ts.timestamp() * 1000)
            candles = _make_candles(start_ms, 360, base_price=100.0)
            candle_month = dec_ts.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            _write_candle_ledger(td, "BTC-PERP", candles, candle_month)
            result = run_labeling_job(conn, td)

        assert result["total_processed"] == 1
        assert result["total_labeled"] == 0  # skipped

    def test_idempotent(self):
        """Running labeling twice doesn't duplicate labels."""
        from meta.labeling import LONGEST_HOURS, run_labeling_job

        conn = sqlite3.connect(":memory:")
        _init_db(conn)

        now = datetime.now(timezone.utc)
        dec_ts = now - timedelta(hours=LONGEST_HOURS + 6)
        entry_price = 100.0

        start_ms = int(dec_ts.timestamp() * 1000)
        candles = _make_candles(start_ms, 360, base_price=entry_price)
        candle_month = dec_ts.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

        _insert_enter_decision(conn, "test_agent", dec_ts, "BTC-PERP", "long", entry_price)

        with tempfile.TemporaryDirectory() as td:
            _write_candle_ledger(td, "BTC-PERP", candles, candle_month)
            r1 = run_labeling_job(conn, td)
            r2 = run_labeling_job(conn, td)

        assert r1["total_labeled"] == 3
        assert r2["total_labeled"] == 0  # already labeled → skipped
        assert r2["total_processed"] == 0  # no eligible rows

    def test_coverage(self):
        """get_labeling_coverage returns correct percentages."""
        from meta.labeling import (
            LONGEST_HOURS,
            get_labeling_coverage,
            run_labeling_job,
        )

        conn = sqlite3.connect(":memory:")
        _init_db(conn)

        now = datetime.now(timezone.utc)
        dec_ts = now - timedelta(hours=LONGEST_HOURS + 6)
        entry_price = 100.0

        start_ms = int(dec_ts.timestamp() * 1000)
        candles = _make_candles(start_ms, 360, base_price=entry_price)
        candle_month = dec_ts.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

        # Two eligible decisions.
        _insert_enter_decision(conn, "test_agent", dec_ts, "BTC-PERP", "long", entry_price)
        _insert_enter_decision(
            conn, "test_agent", dec_ts - timedelta(hours=1), "BTC-PERP", "short", entry_price
        )

        with tempfile.TemporaryDirectory() as td:
            _write_candle_ledger(td, "BTC-PERP", candles, candle_month)
            run_labeling_job(conn, td)

        cov = get_labeling_coverage(conn)
        assert cov["eligible_decisions"] == 2
        assert cov["labeled"] == 2
        assert cov["coverage_pct"] == 100.0


class TestSimulateTrade:
    """Trade simulation logic."""

    def test_sl_hit_long(self):
        """Long trade hits SL → negative PnL."""
        from meta.labeling import _simulate_trade

        entry = 100.0
        sl = 98.0  # 2% below
        tp = 105.0  # 5% above
        start_ms = 1_700_000_000_000

        # Candle drops to 97 (below SL).
        candles = [
            [start_ms, 100, 101, 97, 99, 1000],
            [start_ms + 300_000, 99, 100, 96, 98, 1000],
        ]

        result = _simulate_trade(candles, start_ms / 1000, entry, "long", sl, tp)
        assert result < 0  # SL hit → loss

    def test_tp_hit_short(self):
        """Short trade hits TP → positive PnL."""
        from meta.labeling import _simulate_trade

        entry = 100.0
        sl = 102.0
        tp = 95.0
        start_ms = 1_700_000_000_000

        # Candle drops to 94 (below TP for short).
        candles = [
            [start_ms, 100, 101, 94, 95, 1000],
        ]

        result = _simulate_trade(candles, start_ms / 1000, entry, "short", sl, tp)
        assert result > 0  # TP hit → profit

    def test_no_cross_returns_fwd(self):
        """No SL/TP hit → forward return at last candle."""
        from meta.labeling import _simulate_trade

        entry = 100.0
        sl = 98.0
        tp = 105.0
        start_ms = 1_700_000_000_000

        candles = [
            [start_ms, 100, 102, 99, 101, 1000],  # within range
            [start_ms + 300_000, 101, 103, 100, 102, 1000],  # still within range
        ]

        result = _simulate_trade(candles, start_ms / 1000, entry, "long", sl, tp)
        # Should be ~2% (102 - 100) / 100 * 100
        assert abs(result - 2.0) < 0.1

    def test_default_sl_tp(self):
        """When sl/tp is None, uses DEFAULT_SL_PCT/DEFAULT_TP_PCT."""
        from meta.labeling import _simulate_trade

        entry = 100.0
        start_ms = 1_700_000_000_000

        # Price drops 3% — should hit default 2% SL.
        candles = [
            [start_ms, 100, 101, 96, 97, 1000],
        ]

        result = _simulate_trade(candles, start_ms / 1000, entry, "long", None, None)
        assert result < 0  # SL hit


class TestHorizonLabel:
    """Horizon label computation."""

    def test_best_action_picked(self):
        """best_action is the one with highest outcome."""
        from meta.labeling import _compute_horizon_label

        entry = 100.0
        start_ms = 1_700_000_000_000

        # Price rallies hard → enter_long should be best.
        candles = _make_candles(start_ms, 60, base_price=entry, drift=0.005)

        label = _compute_horizon_label(
            candles, start_ms / 1000, entry, None, None, 1, "wait"
        )
        assert label is not None
        assert label["best_action"] == "enter_long"
        assert label["best_outcome_pct"] > 0
        assert label["chosen_outcome_pct"] == 0.0  # wait chosen
        assert label["regret_pct"] > 0  # missed out

    def test_regret_zero_when_best_chosen(self):
        """Regret = 0 when the chosen action was the best."""
        from meta.labeling import _compute_horizon_label

        entry = 100.0
        start_ms = 1_700_000_000_000

        # Price rallies → enter_long is best.
        candles = _make_candles(start_ms, 60, base_price=entry, drift=0.005)

        label = _compute_horizon_label(
            candles, start_ms / 1000, entry, None, None, 1, "enter_long"
        )
        assert label is not None
        assert label["regret_pct"] == 0.0
        assert label["chosen_outcome_pct"] == label["best_outcome_pct"]


class TestExtractInfo:
    """Decision info extraction."""

    def test_extract_enter_info(self):
        from meta.labeling import _extract_enter_info

        order = {
            "asset": "BTC-PERP",
            "direction": "long",
            "entry_price": 50000.0,
            "stop_loss_price": 49000.0,
            "take_profit_price": 52500.0,
        }
        details = {"order": str(order), "fill": "{'trade_id': 'x'}"}
        info = _extract_enter_info(details)
        assert info is not None
        assert info["asset"] == "BTC-PERP"
        assert info["direction"] == "long"
        assert info["entry_price"] == 50000.0
        assert info["sl"] == 49000.0
        assert info["tp"] == 52500.0

    def test_extract_wait_info(self):
        from meta.labeling import _extract_wait_info

        candidate = {
            "asset": "SOL-PERP",
            "direction": "short",
            "entry_price": 150.0,
            "stop_loss_price": 155.0,
            "take_profit_price": 140.0,
        }
        details = {"candidate": candidate}
        info = _extract_wait_info(details)
        assert info is not None
        assert info["asset"] == "SOL-PERP"
        assert info["direction"] == "short"
        assert info["sl"] == 155.0

    def test_extract_none_for_empty(self):
        from meta.labeling import _extract_enter_info, _extract_wait_info

        assert _extract_enter_info(None) is None
        assert _extract_enter_info({}) is None
        assert _extract_wait_info(None) is None
        assert _extract_wait_info({}) is None
        assert _extract_wait_info({"candidate": None}) is None
