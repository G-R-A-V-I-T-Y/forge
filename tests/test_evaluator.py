"""tests/test_evaluator.py — Unit tests for meta/evaluator.py functions.

Covers significance_test, get_lifecycle_decision, and harvest_best_trades
in isolation from the controller loop.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from store.db import insert_account_snapshot
from meta.evaluator import (
    significance_test,
    get_lifecycle_decision,
    harvest_best_trades,
)


def _iso(hours_ago: float = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _seed_agent(conn, agent_id, status="active", balance=10000.0, peak_balance=10000.0):
    conn.execute(
        "INSERT INTO agents (id, name, status, spawn_date, config_json) VALUES (?, ?, ?, ?, ?)",
        (agent_id, agent_id, status, _iso(2000), "{}"),
    )
    insert_account_snapshot(conn, agent_id, "paper", balance, peak_balance)


def _seed_trades(conn, agent_id, n, n_win, win_pnl_pct, loss_pnl_pct, hours_ago=1,
                 key_conditions_met=None):
    ts = _iso(hours_ago)
    for i in range(n):
        is_win = i < n_win
        pnl_pct = win_pnl_pct if is_win else loss_pnl_pct
        result = "win" if is_win else "loss"
        conn.execute(
            """INSERT INTO trades (id, agent_id, asset, direction, entry_price, exit_price,
               leverage, status, pnl_pct, pnl_usd, result, entry_timestamp, exit_timestamp,
               key_conditions_met, hypothesis)
               VALUES (?, ?, 'BTC-PERP', 'long', 50000, 50500, 1,
               'closed', ?, ?, ?, ?, ?, ?, ?)""",
            (
                f"{agent_id}_{i}", agent_id, pnl_pct, pnl_pct * 10000, result,
                ts, ts, key_conditions_met, f"thesis_{i}",
            ),
        )
    conn.commit()


# ===================================================================
# significance_test
# ===================================================================

class TestSignificanceTest:

    def test_insufficient_data(self):
        result = significance_test({"closed_trades": 10}, {"closed_trades": 30})
        assert result["beats_null"] is False
        assert result["p_value_estimate"] == "insufficient_data"

    def test_null_metrics_none(self):
        result = significance_test({"closed_trades": 50}, None)
        assert result["beats_null"] is False
        assert result["p_value_estimate"] == "insufficient_data"

    def test_beats_null_high_sharpe(self):
        agent = {"closed_trades": 50, "sharpe": 1.5, "profit_factor": 2.0, "win_rate": 0.60}
        null = {"closed_trades": 30, "sharpe": 0.3, "profit_factor": 1.0, "win_rate": 0.50}
        result = significance_test(agent, null)
        assert result["beats_null"] is True
        assert result["sharpe_diff"] > 0

    def test_does_not_beat_null_low_sharpe(self):
        agent = {"closed_trades": 50, "sharpe": 0.1, "profit_factor": 0.9, "win_rate": 0.45}
        null = {"closed_trades": 30, "sharpe": 0.5, "profit_factor": 1.0, "win_rate": 0.50}
        result = significance_test(agent, null)
        assert result["beats_null"] is False

    def test_p_value_buckets(self):
        null = {"closed_trades": 30, "sharpe": 0.0, "profit_factor": 1.0, "win_rate": 0.50}
        # Very high sharpe should be <0.05
        agent_strong = {"closed_trades": 50, "sharpe": 2.0, "profit_factor": 3.0, "win_rate": 0.70}
        result = significance_test(agent_strong, null)
        assert result["p_value_estimate"] == "<0.05"

        # Moderate sharpe should be <0.10
        agent_moderate = {"closed_trades": 50, "sharpe": 0.8, "profit_factor": 1.5, "win_rate": 0.55}
        result = significance_test(agent_moderate, null)
        assert result["p_value_estimate"] in ("<0.10", "<0.05")


# ===================================================================
# get_lifecycle_decision
# ===================================================================

class TestLifecycleDecision:

    def test_win_rate_below_35_terminates(self, conn):
        _seed_agent(conn, "wr_term")
        _seed_trades(conn, "wr_term", n=50, n_win=15, win_pnl_pct=0.02, loss_pnl_pct=-0.015)
        from store.performance import compute_metrics
        metrics = compute_metrics(conn, "wr_term")
        result = get_lifecycle_decision(conn, "wr_term", metrics, None)
        assert result["decision"] == "terminate"
        assert result["trigger"] == "win_rate_below_35"

    def test_drawdown_over_20_suspends(self, conn):
        _seed_agent(conn, "dd_suspend", balance=7000.0, peak_balance=10000.0)
        _seed_trades(conn, "dd_suspend", n=5, n_win=3, win_pnl_pct=0.02, loss_pnl_pct=-0.015)
        from store.performance import compute_metrics
        metrics = compute_metrics(conn, "dd_suspend")
        result = get_lifecycle_decision(conn, "dd_suspend", metrics, None)
        assert result["decision"] == "suspend"
        assert result["trigger"] == "drawdown_exceeds_20pct"

    def test_zero_trades_5d_returns_review(self, conn):
        _seed_agent(conn, "stale_rev")
        _seed_trades(
            conn, "stale_rev", n=10, n_win=6,
            win_pnl_pct=0.02, loss_pnl_pct=-0.015, hours_ago=6 * 24,
        )
        from store.performance import compute_metrics
        metrics = compute_metrics(conn, "stale_rev")
        result = get_lifecycle_decision(conn, "stale_rev", metrics, None)
        assert result["decision"] == "review"
        assert result["trigger"] == "zero_trades_5d"

    def test_active_when_healthy(self, conn):
        _seed_agent(conn, "healthy")
        _seed_trades(conn, "healthy", n=30, n_win=20, win_pnl_pct=0.02, loss_pnl_pct=-0.01)
        from store.performance import compute_metrics
        metrics = compute_metrics(conn, "healthy")
        result = get_lifecycle_decision(conn, "healthy", metrics, None)
        assert result["decision"] == "active"
        assert result["trigger"] == "none"

    def test_suspended_agent_restored(self, conn):
        _seed_agent(conn, "restored", status="suspended")
        _seed_trades(conn, "restored", n=30, n_win=20, win_pnl_pct=0.02, loss_pnl_pct=-0.01)

        conn.execute(
            """INSERT INTO evaluations
                   (agent_id, evaluated_at, trades_evaluated, metrics_json, decision, reason)
               VALUES (?, ?, 20, '{}', 'suspend', 'test')""",
            ("restored", _iso(10)),
        )
        conn.commit()

        from store.performance import compute_metrics
        metrics = compute_metrics(conn, "restored")
        result = get_lifecycle_decision(conn, "restored", metrics, None)
        assert result["decision"] == "active"
        assert result["trigger"] == "restore_after_suspension"

    def test_not_found_agent(self, conn):
        result = get_lifecycle_decision(conn, "nonexistent", {}, None)
        assert result["decision"] == "terminate"
        assert result["trigger"] == "not_found"


# ===================================================================
# harvest_best_trades
# ===================================================================

class TestHarvestBestTrades:

    def test_harvests_top_trades_by_pnl(self, conn):
        _seed_agent(conn, "harvest_basic")
        _seed_trades(
            conn, "harvest_basic", n=10, n_win=5,
            win_pnl_pct=0.05, loss_pnl_pct=-0.01,
        )
        seeds = harvest_best_trades(conn, "harvest_basic", count=3)
        assert len(seeds) == 3
        pnls = [s["pnl_pct"] for s in seeds]
        assert pnls == sorted(pnls, reverse=True)

    def test_prefers_cleanest_thesis_execution(self, conn):
        _seed_agent(conn, "harvest_clean")
        _seed_trades(conn, "harvest_clean", n=3, n_win=3, win_pnl_pct=0.02,
                     loss_pnl_pct=-0.01, key_conditions_met=None)
        ts = _iso(1)
        conn.execute(
            """INSERT INTO trades (id, agent_id, asset, direction, entry_price, exit_price,
               leverage, status, pnl_pct, pnl_usd, result, entry_timestamp, exit_timestamp,
               key_conditions_met, hypothesis)
               VALUES (?, ?, 'BTC-PERP', 'long', 50000, 50500, 1,
               'closed', 0.01, 100.0, 'win', ?, ?, ?, ?)""",
            ("clean_low_pnl", "harvest_clean", ts, ts, "vol_breakout", "low pnl but clean"),
        )
        conn.commit()

        seeds = harvest_best_trades(conn, "harvest_clean", count=2)
        assert len(seeds) == 2
        assert seeds[0]["id"] == "clean_low_pnl"
        assert seeds[0]["key_conditions_met"] == "vol_breakout"

    def test_inserts_correct_columns(self, conn):
        _seed_agent(conn, "harvest_cols")
        _seed_trades(conn, "harvest_cols", n=1, n_win=1, win_pnl_pct=0.03, loss_pnl_pct=-0.01)
        harvest_best_trades(conn, "harvest_cols", count=1)

        row = conn.execute(
            "SELECT * FROM seeds WHERE agent_id = 'harvest_cols'"
        ).fetchone()
        assert row is not None
        assert row["agent_id"] == "harvest_cols"
        assert row["trade_id"] == "harvest_cols_0"
        assert row["pnl_pct"] == 0.03
        assert row["used"] == 0
        assert row["spawned_agent_id"] is None
        assert row["created_at"] is not None
