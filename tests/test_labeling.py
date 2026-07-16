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


def _insert_trade(
    conn: sqlite3.Connection,
    trade_id: str,
    agent_id: str,
    asset: str,
    direction: str,
    exit_price: float,
    exit_dt: datetime,
    sl_pct: float = 0.02,
    tp_pct: float = 0.05,
) -> None:
    """Insert a closed trade row (minimal columns labeling reads)."""
    if direction == "long":
        sl = exit_price * (1 - sl_pct)
        tp = exit_price * (1 + tp_pct)
    else:
        sl = exit_price * (1 + sl_pct)
        tp = exit_price * (1 - tp_pct)

    conn.execute(
        """INSERT INTO trades
           (id, agent_id, asset, direction, entry_price, stop_loss_price,
            take_profit_price, exit_price, exit_timestamp, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'closed')""",
        (
            trade_id, agent_id, asset, direction, exit_price, sl, tp,
            exit_price, _ts_iso(exit_dt),
        ),
    )
    conn.commit()


def _insert_close_decision(
    conn: sqlite3.Connection,
    agent_id: str,
    dt: datetime,
    trade_id: str,
    exit_price: float,
) -> int:
    """Insert a close decision with the exact decision_details_json shape
    agents/decision_loop.py's log_decision() writes for a close action:
    {"position_id": ..., "fill": "<dict repr of execute_close()'s return>"}.
    """
    fill = {
        "trade_id": trade_id,
        "exit_price": exit_price,
        "pnl_pct": 0.0,
        "pnl_usd": 0.0,
        "fees_paid": 0.0,
        "funding_paid": 0.0,
    }
    details = {"position_id": f"pos_{trade_id}", "fill": str(fill)}

    cur = conn.execute(
        """INSERT INTO decisions
           (agent_id, timestamp, decision_action, decision_reason, decision_details_json)
           VALUES (?, ?, ?, ?, ?)""",
        (agent_id, _ts_iso(dt), "close", "agent_close", json.dumps(details)),
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


# ── Finding 1: close decisions must correlate to their actual trade ───────


class TestExtractCloseInfo:
    """_extract_close_info must correlate a close decision to the trade it
    actually closed (via the trade_id embedded in decision_details_json's
    "fill" field) — never the agent's most-recently-closed trade."""

    def test_extract_close_info_correlates_via_trade_id(self):
        from meta.labeling import _extract_close_info

        conn = sqlite3.connect(":memory:")
        _init_db(conn)
        now = datetime.now(timezone.utc)

        _insert_trade(conn, "trade_a", "test_agent", "BTC-PERP", "long", 100.0, now)
        # A second, more-recently-closed trade for the SAME agent on a
        # DIFFERENT asset/direction — the old "most recently closed" query
        # would pick this one instead.
        _insert_trade(
            conn, "trade_b", "test_agent", "ETH-PERP", "short", 500.0,
            now + timedelta(hours=2),
        )

        decision_id = _insert_close_decision(conn, "test_agent", now, "trade_a", 100.0)
        row = conn.execute(
            "SELECT * FROM decisions WHERE id = ?", (decision_id,)
        ).fetchone()

        info = _extract_close_info(dict(row), conn)
        assert info is not None
        assert info["asset"] == "BTC-PERP"
        assert info["direction"] == "long"
        assert info["entry_price"] == 100.0

    def test_extract_close_info_missing_fill_is_null(self):
        """No decision_details_json at all → no correlation possible → None."""
        from meta.labeling import _extract_close_info

        conn = sqlite3.connect(":memory:")
        _init_db(conn)

        cur = conn.execute(
            """INSERT INTO decisions
               (agent_id, timestamp, decision_action, decision_reason, decision_details_json)
               VALUES (?, ?, ?, ?, ?)""",
            ("test_agent", _now_iso(), "close", "agent_close", None),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM decisions WHERE id = ?", (cur.lastrowid,)
        ).fetchone()

        assert _extract_close_info(dict(row), conn) is None

    def test_extract_close_info_unknown_trade_id_is_null(self):
        """trade_id present in details but no matching trade row → None,
        never guessed from a different trade."""
        from meta.labeling import _extract_close_info

        conn = sqlite3.connect(":memory:")
        _init_db(conn)
        now = datetime.now(timezone.utc)

        # Some OTHER closed trade exists, but not the one referenced.
        _insert_trade(conn, "trade_other", "test_agent", "SOL-PERP", "long", 10.0, now)

        decision_id = _insert_close_decision(
            conn, "test_agent", now, "trade_does_not_exist", 10.0
        )
        row = conn.execute(
            "SELECT * FROM decisions WHERE id = ?", (decision_id,)
        ).fetchone()

        assert _extract_close_info(dict(row), conn) is None


class TestLabelCloseDecision:
    """End-to-end run_labeling_job coverage for close-action decisions —
    there was previously NO test exercising a close decision at all."""

    def test_earlier_close_decision_labeled_from_correct_trade(self):
        """Regression for T6 review finding 1: an agent with MULTIPLE closed
        trades on different assets/directions must have its earlier close
        decision labeled from ITS OWN trade, not the agent's latest closed
        trade.

        trade_b (ETH-PERP) closes AFTER trade_a (BTC-PERP) but the decision
        under test is trade_a's close. Only BTC-PERP candles are written to
        the ledger, so the old "most-recently-closed trade" query (which
        would resolve to trade_b/ETH-PERP) finds no candles and labels
        nothing (total_labeled == 0). The fix must correlate to trade_a and
        label successfully.
        """
        from meta.labeling import LONGEST_HOURS, run_labeling_job

        conn = sqlite3.connect(":memory:")
        _init_db(conn)

        now = datetime.now(timezone.utc)
        dec_ts = now - timedelta(hours=LONGEST_HOURS + 6)
        exit_price = 100.0

        _insert_trade(
            conn, "trade_a", "test_agent", "BTC-PERP", "long", exit_price, dec_ts,
        )
        # Closed later than trade_a, different asset/direction — this is
        # what the buggy "most recently closed" query would return.
        _insert_trade(
            conn, "trade_b", "test_agent", "ETH-PERP", "short", 500.0,
            dec_ts + timedelta(hours=2),
        )

        _insert_close_decision(conn, "test_agent", dec_ts, "trade_a", exit_price)

        start_ms = int(dec_ts.timestamp() * 1000)
        candles = _make_candles(start_ms, 360, base_price=exit_price, drift=0.001)
        candle_month = dec_ts.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

        with tempfile.TemporaryDirectory() as td:
            # Only BTC-PERP (trade_a's asset) has candle data.
            _write_candle_ledger(td, "BTC-PERP", candles, candle_month)
            result = run_labeling_job(conn, td)

        assert result["total_processed"] == 1
        assert result["total_labeled"] == 3, (
            "close decision must be labeled from trade_a (BTC-PERP, which "
            "has candle data), not trade_b (ETH-PERP, which has none)"
        )
        assert result["errors"] == 0

        labels = conn.execute(
            "SELECT horizon FROM decision_labels ORDER BY horizon"
        ).fetchall()
        assert {r[0] for r in labels} == {"1h", "4h", "24h"}

    def test_close_decision_without_correlatable_trade_left_unlabeled(self):
        """A close decision whose fill can't be correlated to a trade row
        (e.g. the trade was later deleted/voided) is skipped, not guessed."""
        from meta.labeling import LONGEST_HOURS, run_labeling_job

        conn = sqlite3.connect(":memory:")
        _init_db(conn)

        now = datetime.now(timezone.utc)
        dec_ts = now - timedelta(hours=LONGEST_HOURS + 6)

        _insert_close_decision(conn, "test_agent", dec_ts, "trade_missing", 100.0)

        start_ms = int(dec_ts.timestamp() * 1000)
        candles = _make_candles(start_ms, 360, base_price=100.0)
        candle_month = dec_ts.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

        with tempfile.TemporaryDirectory() as td:
            _write_candle_ledger(td, "BTC-PERP", candles, candle_month)
            result = run_labeling_job(conn, td)

        assert result["total_processed"] == 1
        assert result["total_labeled"] == 0


# ── Finding 4: staleness bound on the forward-return candle lookup ────────


class TestForwardReturnStaleness:
    """_fwd_return_at_cutoff must never bridge a ledger gap with a stale
    candle — labels must be left null across gaps, not interpolated."""

    def test_candle_within_threshold_labels(self):
        from meta.labeling import STALENESS_THRESHOLD_MS, _fwd_return_at_cutoff

        candles = [[0, 100.0, 101.0, 99.0, 105.0, 1000.0]]
        result = _fwd_return_at_cutoff(candles, 100.0, STALENESS_THRESHOLD_MS)
        assert result is not None
        assert abs(result - 5.0) < 1e-9

    def test_candle_beyond_threshold_is_null(self):
        from meta.labeling import STALENESS_THRESHOLD_MS, _fwd_return_at_cutoff

        candles = [[0, 100.0, 101.0, 99.0, 105.0, 1000.0]]
        result = _fwd_return_at_cutoff(
            candles, 100.0, STALENESS_THRESHOLD_MS + 1,
        )
        assert result is None

    def test_gap_at_horizon_boundary_leaves_that_horizon_null(self):
        """A decision whose forward window has a mid-window gap spanning the
        1h horizon boundary must get a null (unwritten) label for 1h, while
        4h/24h — unaffected by the gap — label normally."""
        from meta.labeling import LONGEST_HOURS, run_labeling_job

        conn = sqlite3.connect(":memory:")
        _init_db(conn)

        now = datetime.now(timezone.utc)
        dec_ts = now - timedelta(hours=LONGEST_HOURS + 6)
        entry_price = 100.0
        interval_ms = 5 * 60 * 1000
        start_ms = int(dec_ts.timestamp() * 1000)

        # Segment 1: minutes 0..40 (9 candles) — covers up to 40 min.
        seg1 = _make_candles(start_ms, 9, base_price=entry_price, interval_ms=interval_ms)

        # Gap: no candles between minute 40 and minute 80. The 1h (60 min)
        # cutoff falls inside this gap — nearest available candle (40 min)
        # is 20 min away, well beyond the 10-min staleness threshold.
        seg2_start_ms = start_ms + 80 * 60 * 1000
        # Segment 2 resumes at minute 80 and runs every 5 min through
        # minute 1450 — covers the 4h (240 min) and 24h (1440 min) cutoffs
        # exactly, so those horizons are unaffected by the gap.
        count2 = (1450 - 80) // 5 + 1
        seg2 = _make_candles(
            seg2_start_ms, count2, base_price=seg1[-1][4], interval_ms=interval_ms,
        )
        candles = seg1 + seg2

        _insert_enter_decision(conn, "test_agent", dec_ts, "BTC-PERP", "long", entry_price)

        candle_month = dec_ts.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        with tempfile.TemporaryDirectory() as td:
            _write_candle_ledger(td, "BTC-PERP", candles, candle_month)
            result = run_labeling_job(conn, td)

        assert result["errors"] == 0
        assert result["total_labeled"] == 2, "1h must be skipped, 4h and 24h must label"

        horizons = {
            r[0] for r in conn.execute(
                "SELECT horizon FROM decision_labels"
            ).fetchall()
        }
        assert horizons == {"4h", "24h"}
        assert "1h" not in horizons
