"""tests/test_counterfactuals.py — Deterministic counterfactual replay engine tests.

Covers:
- run_counterfactual_replay: selection, replay, write-back, error handling
- get_counterfactual_coverage: metric computation
- _replay_one: SL hit, TP hit, max_hold_timeout, insufficient data,
    insufficient candidate data, compiled-agent candidate key path
- _has_sufficient_forward_data: boundary conditions
- AC5: insufficient forward data leaves counterfactual_result null
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from store.counterfactuals import (
    _has_sufficient_forward_data,
    _replay_one,
    get_counterfactual_coverage,
    run_counterfactual_replay,
)
from store.db import init_schema


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def conn():
    """In-memory SQLite with schema initialized."""
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.row_factory = sqlite3.Row
    init_schema(c)
    yield c
    c.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _insert_wait(
    conn,
    agent_id: str,
    timestamp: str,
    details: dict | None = None,
    counterfactual_result: str | None = None,
):
    """Insert a wait decision row and return its id."""
    conn.execute(
        """INSERT INTO decisions
        (agent_id, timestamp, decision_action, decision_reason,
            decision_details_json, counterfactual_result, counterfactual_was_better)
        VALUES (?, ?, 'wait', 'test_reason', ?, ?, ?)""",
        (
            agent_id,
            timestamp,
            json.dumps(details or {}),
            counterfactual_result,
            0,
        ),
    )
    conn.commit()
    row_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    return row_id


def _make_candles(
    entry_ts: int,
    n: int,
    direction: str,
    entry_price: float = 100.0,
    sl: float | None = None,
    tp: float | None = None,
    sl_hit_at: int | None = None,
    tp_hit_at: int | None = None,
) -> list[list]:
    """Build [ts_ms, o, h, l, c, v] candles that may hit SL/TP.

    *sl_hit_at* / *tp_hit_at* are candle indices (0-based) at which the
    respective level is crossed.  When neither is set the price stays
    safely between *sl* and *tp* (or drifts up for long if they are None).
    """
    candles: list[list] = []
    for i in range(n):
        ts_ms = (entry_ts + i * 300) * 1000
        o = entry_price + i * 0.01

        if sl_hit_at is not None and i == sl_hit_at:
            if direction == "long":
                h, l = o + 0.01, sl - 0.01
            else:
                h, l = sl + 0.01, o - 0.01
        elif tp_hit_at is not None and i == tp_hit_at:
            if direction == "long":
                h, l = tp + 0.01, o - 0.01
            else:
                h, l = o + 0.01, tp - 0.01
        else:
            if direction == "long":
                safe_low = (sl if sl is not None else entry_price - 5.0) + 0.5
                safe_high = (tp if tp is not None else entry_price + 5.0) - 0.5
                o = max(o, safe_low)
                o = min(o, safe_high)
            else:
                safe_low = (tp if tp is not None else entry_price - 5.0) + 0.5
                safe_high = (sl if sl is not None else entry_price + 5.0) - 0.5
                o = max(o, safe_low)
                o = min(o, safe_high)
            h, l = o + 0.01, o - 0.01

        c = o + 0.005
        candles.append([ts_ms, o, h, l, c, 1000.0])
    return candles


# ---------------------------------------------------------------------------
# AC1: run_counterfactual_replay — select, replay, write
# ---------------------------------------------------------------------------

class TestRunCounterfactualReplay:
    """AC1: Select unfilled waits >N hours, replay via find_first_cross,
    write counterfactual_result + counterfactual_was_better."""

    def _make_wait_row(
        self,
        conn,
        entry_ts: int,
        asset: str,
        direction: str,
        entry_price: float,
        sl: float,
        tp: float,
        candidate_key: bool = True,
        max_hold_hours: int = 48,
    ):
        """Insert a wait decision and return its dict representation."""
        now = datetime.now(timezone.utc)
        ts = (now - timedelta(hours=3)).isoformat().replace("+00:00", "Z")
        details = {
            "candidate": {
                "asset": asset,
                "direction": direction,
                "entry_price": entry_price,
                "stop_loss_price": sl,
                "take_profit_price": tp,
                "max_hold_hours": max_hold_hours,
            }
        } if candidate_key else {
            "asset": asset,
            "direction": direction,
            "entry_price": entry_price,
            "stop_loss_price": sl,
            "take_profit_price": tp,
        }
        row_id = _insert_wait(conn, "test_agent", ts, details)
        row = conn.execute(
            "SELECT * FROM decisions WHERE id = ?", (row_id,)
        ).fetchone()
        return dict(row)

    def test_replay_long_sl_hit(self, conn):
        """Long position — SL hit → counterfactual_was_better = 0."""
        entry_ts = int(datetime.now(timezone.utc).timestamp()) - 3600
        candles = _make_candles(entry_ts, 20, "long", 100.0, sl=99.0, sl_hit_at=3)

        with patch(
            "store.counterfactuals._fetch_candles",
            return_value=candles,
        ):
            result = run_counterfactual_replay(conn, {})

        assert result["processed"] == 0  # no waits inserted yet
        assert result["filled"] == 0

        # Insert a wait that should trigger SL.
        row = self._make_wait_row(conn, entry_ts, "BTC-PERP", "long",
                                100.0, sl=99.0, tp=110.0)

        with patch(
            "store.counterfactuals._fetch_candles",
            return_value=candles,
        ):
            result = run_counterfactual_replay(conn, {})
        assert result["processed"] == 1
        assert result["filled"] == 1

        updated = conn.execute(
            "SELECT counterfactual_result, counterfactual_was_better "
            "FROM decisions WHERE id = ?", (row["id"],)
        ).fetchone()
        outcome = json.loads(updated["counterfactual_result"])
        assert outcome["reason"] == "stop_loss"
        assert outcome["profitable"] is False
        assert updated["counterfactual_was_better"] == 0

    def test_replay_long_tp_hit(self, conn):
        """Long position — TP hit → counterfactual_was_better = 1."""
        entry_ts = int(datetime.now(timezone.utc).timestamp()) - 3600
        candles = _make_candles(entry_ts, 20, "long", 100.0, tp=110.0, tp_hit_at=5)

        with patch(
            "store.counterfactuals._fetch_candles",
            return_value=candles,
        ):
            row = self._make_wait_row(conn, entry_ts, "ETH-PERP", "long",
                                    100.0, sl=90.0, tp=110.0)
            result = run_counterfactual_replay(conn, {})

        assert result["processed"] == 1
        assert result["filled"] == 1

        updated = conn.execute(
            "SELECT counterfactual_result FROM decisions WHERE id = ?",
            (row["id"],)
        ).fetchone()
        outcome = json.loads(updated["counterfactual_result"])
        assert outcome["reason"] == "take_profit"
        assert outcome["profitable"] is True

    def test_replay_short_sl_hit(self, conn):
        """Short position — SL hit → not profitable."""
        entry_ts = int(datetime.now(timezone.utc).timestamp()) - 3600
        candles = _make_candles(entry_ts, 20, "short", 100.0, sl=102.0, sl_hit_at=3)

        with patch(
            "store.counterfactuals._fetch_candles",
            return_value=candles,
        ):
            row = self._make_wait_row(conn, entry_ts, "SOL-PERP", "short",
                                    100.0, sl=102.0, tp=90.0)
            result = run_counterfactual_replay(conn, {})

        assert result["filled"] == 1
        updated = conn.execute(
            "SELECT counterfactual_result FROM decisions WHERE id = ?",
            (row["id"],)
        ).fetchone()
        outcome = json.loads(updated["counterfactual_result"])
        assert outcome["reason"] == "stop_loss"
        assert outcome["profitable"] is False

    def test_replay_skips_already_filled(self, conn):
        """Already-filled counterfactual_result should not be re-processed."""
        entry_ts = int(datetime.now(timezone.utc).timestamp()) - 3600
        _insert_wait(
            conn, "test_agent",
            (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat().replace("+00:00", "Z"),
            {"asset": "BTC-PERP", "direction": "long", "entry_price": 100.0,
            "stop_loss_price": 99.0, "take_profit_price": 110.0},
            counterfactual_result='{"reason": "already_done"}',
        )
        result = run_counterfactual_replay(conn, {})
        assert result["processed"] == 0  # already filled → skipped

    def test_replay_skips_too_recent(self, conn):
        """Waits < MIN_WAIT_AGE_HOURS (2h) old are not selected."""
        entry_ts = int(datetime.now(timezone.utc).timestamp()) - 3600
        # Insert a wait only 30 minutes old
        ts = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat().replace("+00:00", "Z")
        _insert_wait(
            conn, "test_agent", ts,
            {"asset": "BTC-PERP", "direction": "long", "entry_price": 100.0,
            "stop_loss_price": 99.0, "take_profit_price": 110.0},
        )
        result = run_counterfactual_replay(conn, {})
        assert result["processed"] == 0

    def test_replay_non_wait_decisions_ignored(self, conn):
        """Only 'wait' decisions are replayed."""
        conn.execute(
            """INSERT INTO decisions
            (agent_id, timestamp, decision_action, decision_reason,
                decision_details_json, counterfactual_result, counterfactual_was_better)
            VALUES (?, ?, 'buy', 'test', '{}', NULL, 0)""",
            ("test_agent",
            (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat().replace("+00:00", "Z")),
        )
        conn.commit()
        result = run_counterfactual_replay(conn, {})
        assert result["processed"] == 0

    def test_replay_multiple_waits(self, conn):
        """Multiple eligible waits are all processed."""
        entry_ts = int(datetime.now(timezone.utc).timestamp()) - 3600
        candles = _make_candles(entry_ts, 20, "long", 100.0, tp=110.0, tp_hit_at=5)

        for i in range(3):
            _insert_wait(
                conn, f"agent_{i}",
                (datetime.now(timezone.utc) - timedelta(hours=3 + i)).isoformat().replace("+00:00", "Z"),
                {"candidate": {"asset": "BTC-PERP", "direction": "long",
                            "entry_price": 100.0, "stop_loss_price": 99.0,
                            "take_profit_price": 110.0}},
            )

        with patch(
            "store.counterfactuals._fetch_candles",
            return_value=candles,
        ):
            result = run_counterfactual_replay(conn, {})

        assert result["processed"] == 3
        assert result["filled"] == 3

    def test_replay_error_counting(self, conn):
        """Errors during replay are counted, not raised."""
        entry_ts = int(datetime.now(timezone.utc).timestamp()) - 3600
        _insert_wait(
            conn, "test_agent",
            (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat().replace("+00:00", "Z"),
            {"candidate": {"asset": "BTC-PERP", "direction": "long",
                        "entry_price": 100.0, "stop_loss_price": 99.0,
                        "take_profit_price": 110.0}},
        )
        with patch(
            "store.counterfactuals._fetch_candles",
            side_effect=RuntimeError("ledger read failure"),
        ):
            result = run_counterfactual_replay(conn, {})

        assert result["errors"] == 1

    def test_replay_no_eligible_waits(self, conn):
        """Empty queue returns zero counts."""
        result = run_counterfactual_replay(conn, {})
        assert result == {"total_queued": 0, "processed": 0, "filled": 0, "errors": 0}


# ---------------------------------------------------------------------------
# AC4: get_counterfactual_coverage
# ---------------------------------------------------------------------------

class TestGetCounterfactualCoverage:
    """AC4: Coverage exposed at /health (verified via the metric function)."""

    def test_coverage_empty_db(self, conn):
        """No waits → coverage_pct = 0.0."""
        result = get_counterfactual_coverage(conn)
        assert result["total_waits"] == 0
        assert result["coverage_pct"] == 0.0

    def test_coverage_partial(self, conn):
        """Some waits filled, some not → correct percentage."""
        now = datetime.now(timezone.utc)
        ts_old = (now - timedelta(hours=25)).isoformat().replace("+00:00", "Z")

        # 3 old waits
        _insert_wait(conn, "a", ts_old,
                    {"asset": "BTC", "direction": "long", "entry_price": 100.0,
                    "stop_loss_price": 99.0, "take_profit_price": 110.0})
        _insert_wait(conn, "b", ts_old,
                    {"asset": "ETH", "direction": "short", "entry_price": 50.0,
                    "stop_loss_price": 52.0, "take_profit_price": 45.0})
        _insert_wait(conn, "c", ts_old,
                    {"asset": "SOL", "direction": "long", "entry_price": 100.0,
                    "stop_loss_price": 99.0, "take_profit_price": 110.0})

        # Fill first two
        conn.execute(
            "UPDATE decisions SET counterfactual_result = ?, counterfactual_was_better = ? "
            "WHERE decision_details_json LIKE '%BTC%'",
            ('{"reason":"tp"}', 1),
        )
        conn.execute(
            "UPDATE decisions SET counterfactual_result = ?, counterfactual_was_better = ? "
            "WHERE decision_details_json LIKE '%ETH%'",
            ('{"reason":"sl"}', 0),
        )
        conn.commit()

        result = get_counterfactual_coverage(conn)
        assert result["total_waits"] == 3
        assert result["eligible_waits"] == 3
        assert result["filled"] == 2
        assert result["coverage_pct"] == round(2 / 3 * 100, 2)

    def test_coverage_excludes_recent_waits(self, conn):
        """Waits < 24h old are excluded from coverage count."""
        now = datetime.now(timezone.utc)
        ts_old = (now - timedelta(hours=25)).isoformat().replace("+00:00", "Z")
        ts_recent = (now - timedelta(hours=1)).isoformat().replace("+00:00", "Z")

        _insert_wait(conn, "a", ts_old,
                    {"asset": "BTC", "direction": "long", "entry_price": 100.0,
                    "stop_loss_price": 99.0, "take_profit_price": 110.0})
        _insert_wait(conn, "b", ts_recent,
                    {"asset": "ETH", "direction": "short", "entry_price": 50.0,
                    "stop_loss_price": 52.0, "take_profit_price": 45.0})

        result = get_counterfactual_coverage(conn)
        assert result["total_waits"] == 1  # only the old one
        assert result["filled"] == 0


# ---------------------------------------------------------------------------
# _replay_one — single wait replay logic
# ---------------------------------------------------------------------------

class TestReplayOne:
    """Unit tests for _replay_one covering all paths."""

    def _make_wait(self, details: dict, timestamp: str | None = None) -> dict:
        """Build a wait_row dict for _replay_one."""
        ts = timestamp or (
            datetime.now(timezone.utc) - timedelta(hours=3)
        ).isoformat().replace("+00:00", "Z")
        return {
            "id": 1,
            "agent_id": "test",
            "timestamp": ts,
            "decision_action": "wait",
            "decision_reason": "test",
            "decision_details_json": json.dumps(details),
        }

    def test_replay_insufficient_candidate_data(self):
        """Missing fields → None (no write)."""
        wait = self._make_wait({})
        result = _replay_one(None, wait, "/tmp")
        assert result is None

    def test_replay_insufficient_candidate_data_missing_sl(self):
        """Missing SL price → None."""
        wait = self._make_wait({
            "asset": "BTC", "direction": "long",
            "entry_price": 100.0, "take_profit_price": 110.0,
        })
        result = _replay_one(None, wait, "/tmp")
        assert result is None

    def test_replay_insufficient_candidate_data_missing_tp(self):
        """Missing TP price → None."""
        wait = self._make_wait({
            "asset": "BTC", "direction": "long",
            "entry_price": 100.0, "stop_loss_price": 99.0,
        })
        result = _replay_one(None, wait, "/tmp")
        assert result is None

    def test_replay_top_level_details_path(self):
        """Non-compiled agents: details at top level."""
        now = datetime.now(timezone.utc)
        entry_ts = int((now - timedelta(hours=3)).timestamp())
        wait = self._make_wait({
            "asset": "BTC", "direction": "long",
            "entry_price": 100.0, "stop_loss_price": 99.0,
            "take_profit_price": 110.0,
        })
        candles = _make_candles(entry_ts, 20, "long", 100.0, tp=110.0, tp_hit_at=5)
        with patch("store.counterfactuals._fetch_candles", return_value=candles):
            result = _replay_one(None, wait, "/tmp")
        assert result is not None
        assert result["reason"] == "take_profit"
        assert result["profitable"] is True

    def test_replay_compiled_candidate_path(self):
        """Compiled agents: details has 'candidate' key."""
        now = datetime.now(timezone.utc)
        entry_ts = int((now - timedelta(hours=3)).timestamp())
        wait = self._make_wait({
            "candidate": {
                "asset": "BTC", "direction": "long",
                "entry_price": 100.0, "stop_loss_price": 99.0,
                "take_profit_price": 110.0,
                "max_hold_hours": 24,
            }
        })
        candles = _make_candles(entry_ts, 20, "long", 100.0, tp=110.0, tp_hit_at=5)
        with patch("store.counterfactuals._fetch_candles", return_value=candles):
            result = _replay_one(None, wait, "/tmp")
        assert result is not None
        assert result["max_hold_hours"] == 24

    def test_replay_no_candles(self):
        """No candle data → None."""
        wait = self._make_wait({
            "candidate": {
                "asset": "BTC", "direction": "long",
                "entry_price": 100.0, "stop_loss_price": 99.0,
                "take_profit_price": 110.0,
            }
        })
        with patch("store.counterfactuals._fetch_candles", return_value=None):
            result = _replay_one(None, wait, "/tmp")
        assert result is None

    def test_replay_max_hold_timeout_long(self):
        """No SL/TP hit, sufficient data → max_hold_timeout."""
        now = datetime.now(timezone.utc)
        entry_ts = int((now - timedelta(hours=3)).timestamp())
        wait = self._make_wait({
            "candidate": {
                "asset": "BTC", "direction": "long",
                "entry_price": 100.0, "stop_loss_price": 99.0,
                "take_profit_price": 110.0,
                "max_hold_hours": 48,
            }
        })
        # 600 candles = 50 hours, price stays between SL and TP
        candles = []
        for i in range(600):
            ts_ms = (entry_ts + i * 300) * 1000
            price = 100.0 + i * 0.001  # slow drift, stays well within SL/TP
            candles.append([ts_ms, price, price + 0.01, price - 0.01,
                            price + 0.005, 1000.0])
        with patch("store.counterfactuals._fetch_candles", return_value=candles):
            result = _replay_one(None, wait, "/tmp")
        assert result is not None
        assert result["reason"] == "max_hold_timeout"

    def test_replay_insufficient_forward_data(self):
        """No SL/TP hit, insufficient forward data → None (AC5)."""
        wait = self._make_wait({
            "candidate": {
                "asset": "BTC", "direction": "long",
                "entry_price": 100.0, "stop_loss_price": 99.0,
                "take_profit_price": 110.0,
                "max_hold_hours": 48,
            }
        })
        # Only 10 candles = 50 minutes — far less than 48h
        candles = _make_candles(1000000, 10, "long")
        with patch("store.counterfactuals._fetch_candles", return_value=candles):
            result = _replay_one(None, wait, "/tmp")
        assert result is None  # AC5: insufficient data → null

    def test_replay_invalid_timestamp(self):
        """Bad timestamp → None."""
        wait = self._make_wait({
            "candidate": {
                "asset": "BTC", "direction": "long",
                "entry_price": 100.0, "stop_loss_price": 99.0,
                "take_profit_price": 110.0,
            }
        }, timestamp="not-a-date")
        result = _replay_one(None, wait, "/tmp")
        assert result is None

    def test_replay_pnl_calculation_long(self):
        """Long PnL = (exit - entry) / entry."""
        wait = self._make_wait({
            "candidate": {
                "asset": "BTC", "direction": "long",
                "entry_price": 100.0, "stop_loss_price": 99.0,
                "take_profit_price": 110.0,
            }
        })
        now = datetime.now(timezone.utc)
        entry_ts = int((now - timedelta(hours=3)).timestamp())
        candles = _make_candles(entry_ts, 20, "long", 100.0, tp=110.0, tp_hit_at=5)
        with patch("store.counterfactuals._fetch_candles", return_value=candles):
            result = _replay_one(None, wait, "/tmp")
        assert result is not None
        assert result["pnl_pct"] > 0  # profitable long

    def test_replay_pnl_calculation_short(self):
        """Short PnL = (entry - exit) / entry."""
        now = datetime.now(timezone.utc)
        entry_ts = int((now - timedelta(hours=3)).timestamp())
        wait = self._make_wait({
            "candidate": {
                "asset": "BTC", "direction": "short",
                "entry_price": 100.0, "stop_loss_price": 102.0,
                "take_profit_price": 90.0,
            }
        })
        candles = _make_candles(entry_ts, 20, "short", 100.0, tp=90.0, tp_hit_at=5)
        with patch("store.counterfactuals._fetch_candles", return_value=candles):
            result = _replay_one(None, wait, "/tmp")
        assert result is not None
        assert result["pnl_pct"] > 0  # profitable short


# ---------------------------------------------------------------------------
# _has_sufficient_forward_data
# ---------------------------------------------------------------------------

class TestHasSufficientForwardData:
    """Boundary conditions for forward-data check."""

    def test_empty_candles(self):
        assert _has_sufficient_forward_data([], 1000000, 48) is False

    def test_sufficient_data(self):
        """577 candles = 48 hours → True."""
        entry_ts = 1000000
        candles = [[(entry_ts + i * 300) * 1000, 100, 100.1, 99.9, 100.05, 1000]
                    for i in range(577)]
        assert _has_sufficient_forward_data(candles, entry_ts, 48) is True

    def test_insufficient_data(self):
        """100 candles = 8.33 hours < 48 → False."""
        entry_ts = 1000000
        candles = [[(entry_ts + i * 300) * 1000, 100, 100.1, 99.9, 100.05, 1000]
                    for i in range(100)]
        assert _has_sufficient_forward_data(candles, entry_ts, 48) is False

    def test_exact_boundary(self):
        """Exactly 48 hours (577 candles) → True."""
        entry_ts = 1000000
        candles = [[(entry_ts + i * 300) * 1000, 100, 100.1, 99.9, 100.05, 1000]
                    for i in range(577)]
        assert _has_sufficient_forward_data(candles, entry_ts, 48) is True


# ---------------------------------------------------------------------------
# AC2: Zero LLM calls
# ---------------------------------------------------------------------------

class TestNoLLMCalls:
    """AC2: Deterministic replay — no LLM calls at all."""

    def test_replay_does_not_import_llm(self):
        """The module only imports from store and standard library."""
        import store.counterfactuals as mod
        imported_modules = set()
        for name in dir(mod):
            obj = getattr(mod, name)
            if hasattr(obj, "__module__"):
                imported_modules.add(obj.__module__.split(".")[0])
        # LLM-related modules should not be imported
        assert "llm" not in imported_modules, \
            "counterfactuals.py must not import any LLM modules (AC2)"

    def test_replay_is_deterministic(self, conn):
        """Same inputs produce same outputs — no randomness."""
        entry_ts = int(datetime.now(timezone.utc).timestamp()) - 3600
        candles = _make_candles(entry_ts, 20, "long", 100.0, tp=110.0, tp_hit_at=5)

        for _ in range(3):
            _insert_wait(
                conn, "test_agent",
                (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat().replace("+00:00", "Z"),
                {"candidate": {"asset": "BTC-PERP", "direction": "long",
                            "entry_price": 100.0, "stop_loss_price": 99.0,
                            "take_profit_price": 110.0}},
            )

        with patch(
            "store.counterfactuals._fetch_candles",
            return_value=candles,
        ):
            run_counterfactual_replay(conn, {})

        # All rows should have identical results
        rows = conn.execute(
            "SELECT counterfactual_result FROM decisions "
            "WHERE counterfactual_result IS NOT NULL"
        ).fetchall()
        assert len(rows) == 3
        results = [json.loads(r["counterfactual_result"]) for r in rows]
        assert all(r["reason"] == "take_profit" for r in results)
        assert all(r["profitable"] is True for r in results)


# ---------------------------------------------------------------------------
# Production ledger format: candles_5m partitions carry ISO-string ts
# ---------------------------------------------------------------------------

class TestLedgerIsoTimestamps:
    def test_replay_fills_wait_from_iso_ts_ledger_partition(self, conn, tmp_path):
        """The real ledger (export_heartbeat_to_ledger) writes candle ts as
        an ISO string ("2026-07-11T17:43:58Z").  pd.to_numeric turns those
        into NaN, so without ISO handling the ledger path silently returns
        no candles and every wait older than the 25h heartbeat window can
        never be counterfactually filled."""
        import store.counterfactuals as cf

        wait_dt = datetime.now(timezone.utc) - timedelta(hours=3)
        wait_ts_iso = wait_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

        details = {
            "candidate": {
                "asset": "SOL-PERP",
                "direction": "long",
                "entry_price": 100.0,
                "stop_loss_price": 97.0,
                "take_profit_price": 103.0,
            }
        }
        _insert_wait(conn, "iso_agent", wait_ts_iso, details=details)
        # _insert_wait seeds counterfactual_was_better=0 with NULL result;
        # ensure result is NULL so the row is selected.
        conn.execute("UPDATE decisions SET counterfactual_result = NULL")
        conn.commit()

        # Write a real-format partition: ISO ts strings, one candle per
        # 5 minutes from the wait forward, wicking through TP on candle 3.
        ledger_dir = tmp_path / "ledger"
        part = ledger_dir / "candles_5m" / f"{wait_dt:%Y-%m}.jsonl"
        part.parent.mkdir(parents=True)
        lines = []
        for i in range(6):
            c_dt = wait_dt + timedelta(minutes=5 * (i + 1))
            price = 100.0 if i < 3 else 104.0
            lines.append(json.dumps({
                "ts": c_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "asset": "SOL-PERP",
                "o": price, "h": price + 0.5, "l": price - 0.5, "c": price,
                "v": 1.0,
            }))
        part.write_text("\n".join(lines) + "\n", encoding="utf-8")

        summary = run_counterfactual_replay(conn, {}, ledger_dir)
        assert summary["errors"] == 0
        assert summary["filled"] == 1, (
            "ISO-ts ledger candles must be replayable — this is the only "
            "format production writes"
        )

        row = conn.execute(
            "SELECT counterfactual_result, counterfactual_was_better FROM decisions"
        ).fetchone()
        result = json.loads(row["counterfactual_result"])
        assert result["reason"] == "take_profit"
        assert row["counterfactual_was_better"] == 1
