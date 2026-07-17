"""tests/test_bootstrap_null.py — M11.7 bootstrap null significance test."""
from __future__ import annotations

import pytest

from meta.evaluator import (
    BOOTSTRAP_RESAMPLES,
    NULL_MIN_TRADES,
    _compute_sharpe,
    _get_null_per_trade_returns,
    significance_test,
)
from store.db import insert_agent


AGENT_ID = "sharpe_hero"
NULL_AGENT = "benchmark_random_walk"


def _seed_agent_trades(conn, agent_id: str, returns: list[float]) -> None:
    """Insert closed trades with given pnl_pct values."""
    insert_agent(conn, agent_id, agent_id, "2026-01-01T00:00:00Z", "{}")
    for i, ret in enumerate(returns):
        conn.execute(
            """INSERT INTO trades
                   (id, agent_id, mode, asset, direction, status, pnl_pct,
                    entry_timestamp, result)
               VALUES (?, ?, 'paper', 'SOL-PERP', 'long', 'closed', ?,
                       ?, ?)""",
            (f"{agent_id}_{i}", agent_id, ret,
             f"2026-01-01T{i:02d}:00:00Z",
             "win" if ret > 0 else "loss"),
        )
    conn.commit()


class TestBootstrapNull:
    def test_insufficient_data_when_few_agent_trades(self, conn):
        _seed_agent_trades(conn, NULL_AGENT, [0.01] * 25)
        _seed_agent_trades(conn, AGENT_ID, [0.02] * 15)
        null_metrics = {"sharpe": 1.0, "closed_trades": 25, "profit_factor": 1.5, "win_rate": 0.6}
        agent_metrics = {"sharpe": 2.0, "closed_trades": 15, "profit_factor": 2.0, "win_rate": 0.7}
        result = significance_test(agent_metrics, null_metrics, conn=conn)
        assert result["p_value_estimate"] == "insufficient_data"
        assert result["beats_null"] is False

    def test_insufficient_data_when_null_is_none(self, conn):
        agent_metrics = {"sharpe": 2.0, "closed_trades": 50, "profit_factor": 2.0, "win_rate": 0.7}
        result = significance_test(agent_metrics, None, conn=conn)
        assert result["p_value_estimate"] == "insufficient_data"

    def test_bootstrap_with_sufficient_data(self, conn):
        """Agent with high Sharpe should beat a random walk null."""
        null_returns = [0.001, -0.001, 0.002, -0.002, 0.0005,
                        -0.0005, 0.0015, -0.0015, 0.0008, -0.0008] * 5
        _seed_agent_trades(conn, NULL_AGENT, null_returns)
        agent_returns = [0.02, 0.015, 0.03, 0.01, 0.025,
                         0.02, 0.015, 0.03, 0.01, 0.025,
                         0.02, 0.015, 0.03, 0.01, 0.025,
                         0.02, 0.015, 0.03, 0.01, 0.025,
                         0.02, 0.015, 0.03, 0.01, 0.025,
                         0.02, 0.015, 0.03, 0.01, 0.025,
                         0.02, 0.015, 0.03, 0.01, 0.025]
        _seed_agent_trades(conn, AGENT_ID, agent_returns)
        null_metrics = {"sharpe": 0.1, "closed_trades": 50, "profit_factor": 1.1, "win_rate": 0.52}
        agent_metrics = {"sharpe": 3.0, "closed_trades": 35, "profit_factor": 5.0, "win_rate": 0.8}
        result = significance_test(agent_metrics, null_metrics, conn=conn)
        assert result["p_value_estimate"] == "<0.05"
        assert result["beats_null"] is True

    def test_low_sharpe_does_not_beat_null(self, conn):
        null_returns = [0.001, -0.001, 0.002, -0.002, 0.0005] * 20
        _seed_agent_trades(conn, NULL_AGENT, null_returns)
        agent_returns = [0.0005, -0.0005, 0.001, -0.001, 0.0003] * 10
        _seed_agent_trades(conn, AGENT_ID, agent_returns)
        null_metrics = {"sharpe": 0.5, "closed_trades": 100, "profit_factor": 1.2, "win_rate": 0.55}
        agent_metrics = {"sharpe": 0.3, "closed_trades": 50, "profit_factor": 1.0, "win_rate": 0.50}
        result = significance_test(agent_metrics, null_metrics, conn=conn)
        assert result["beats_null"] is False
        assert result["p_value_estimate"] in (">0.10", "<0.10")

    def test_null_insufficient_trades_gives_insufficient_data(self, conn):
        _seed_agent_trades(conn, NULL_AGENT, [0.01] * 20)
        _seed_agent_trades(conn, AGENT_ID, [0.02] * 40)
        null_metrics = {"sharpe": 1.0, "closed_trades": 20, "profit_factor": 1.5, "win_rate": 0.6}
        agent_metrics = {"sharpe": 2.0, "closed_trades": 40, "profit_factor": 2.0, "win_rate": 0.7}
        result = significance_test(agent_metrics, null_metrics, conn=conn)
        assert result["p_value_estimate"] == "insufficient_data"

    def test_fallback_without_conn_uses_normal_approx(self, conn):
        """When conn is not provided, falls back to normal approximation."""
        null_metrics = {"sharpe": 0.1, "closed_trades": 100, "profit_factor": 1.1, "win_rate": 0.52}
        agent_metrics = {"sharpe": 3.0, "closed_trades": 50, "profit_factor": 5.0, "win_rate": 0.8}
        result = significance_test(agent_metrics, null_metrics, conn=None)
        assert result["p_value_estimate"] in ("<0.05", "<0.10", ">0.10")

    def test_returns_correct_keys(self, conn):
        _seed_agent_trades(conn, NULL_AGENT, [0.01] * 50)
        _seed_agent_trades(conn, AGENT_ID, [0.02] * 50)
        null_metrics = {"sharpe": 0.5, "closed_trades": 50, "profit_factor": 1.2, "win_rate": 0.55}
        agent_metrics = {"sharpe": 1.5, "closed_trades": 50, "profit_factor": 2.0, "win_rate": 0.65}
        result = significance_test(agent_metrics, null_metrics, conn=conn)
        assert "beats_null" in result
        assert "p_value_estimate" in result
        assert "sharpe_diff" in result
        assert "profit_factor_diff" in result
        assert "win_rate_diff" in result

    def test_get_null_per_trade_returns(self, conn):
        _seed_agent_trades(conn, NULL_AGENT, [0.01, -0.005, 0.02, -0.01])
        returns = _get_null_per_trade_returns(conn)
        assert len(returns) == 4
        assert returns[0] == 0.01

    def test_compute_sharpe_basic(self):
        assert _compute_sharpe([]) == 0.0
        assert _compute_sharpe([0.5]) == 0.0
        assert _compute_sharpe([0.1, 0.1, 0.1]) == 0.0  # zero std
        sharpe = _compute_sharpe([0.1, -0.05, 0.2, -0.1])
        assert isinstance(sharpe, float)
        assert sharpe != 0.0

    def test_bootstrap_resample_count(self):
        """Verify the bootstrap uses the configured resample count."""
        assert BOOTSTRAP_RESAMPLES == 1000

    def test_null_min_trades_constant(self):
        """Verify the R12 latch threshold."""
        assert NULL_MIN_TRADES == 30
