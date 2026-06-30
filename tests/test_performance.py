"""Tests for store/performance.py."""
import math
from store.db import insert_agent, insert_trade
from store.performance import compute_metrics, format_performance_summary
from uuid import uuid4

AGENT_ID = "jade_hawk"

def _trade(asset="SOL-PERP", pnl_pct=0.05, result="win",
           direction="long", exit_reason="tp_hit"):
    ts = "2026-06-30T12:00:00Z"
    uid = str(uuid4())
    return {
        "id": uid,
        "agent_id": AGENT_ID,
        "thesis_version": 1,
        "account_balance_at_entry": 50000.0,
        "mode": "paper",
        "asset": asset,
        "direction": direction,
        "entry_price": 100.0,
        "stop_loss_price": 95.0,
        "take_profit_price": 110.0,
        "leverage": 1,
        "position_size_pct": 0.10,
        "notional_usd": 5000.0,
        "entry_timestamp": ts,
        "exit_price": 100.0 * (1 + pnl_pct),
        "exit_timestamp": ts,
        "exit_reason": exit_reason,
        "duration_minutes": 60.0,
        "pnl_pct": pnl_pct,
        "pnl_usd": 5000.0 * pnl_pct,
        "result": result,
        "status": "closed",
        "market_context_json": "{}",
        "agent_reasoning_json": "{}",
        "postmortem": None,
        "hypothesis": "test",
        "key_conditions_met": None,
        "key_conditions_missing": None,
        "confidence": None,
        "expected_value": None,
        "agent_postmortem": None,
    }

def _setup_agent(conn):
    insert_agent(conn, AGENT_ID, AGENT_ID, "2026-06-01T00:00:00Z", "{}")

def test_no_trades_returns_defaults(conn):
    _setup_agent(conn)
    metrics = compute_metrics(conn, AGENT_ID)
    assert metrics["win_rate"] == 0.0
    assert metrics["profit_factor"] == 0.0
    assert metrics["total_trades"] == 0
    assert metrics["closed_trades"] == 0

def test_all_wins(conn):
    _setup_agent(conn)
    for i in range(3):
        insert_trade(conn, _trade(pnl_pct=0.05 * (i + 1), result="win"))
    metrics = compute_metrics(conn, AGENT_ID)
    assert metrics["win_rate"] == 1.0
    assert metrics["closed_trades"] == 3

def test_mixed_results(conn):
    _setup_agent(conn)
    insert_trade(conn, _trade("SOL-PERP", 0.10, "win"))
    insert_trade(conn, _trade("BTC-PERP", -0.05, "loss"))
    insert_trade(conn, _trade("ETH-PERP", 0.03, "win"))
    insert_trade(conn, _trade("ARB-PERP", -0.02, "loss"))
    metrics = compute_metrics(conn, AGENT_ID)
    assert metrics["closed_trades"] == 4
    assert metrics["win_rate"] == 0.5
    assert math.isclose(metrics["profit_factor"], 0.13 / 0.07, rel_tol=1e-3)
    assert math.isclose(metrics["avg_win_pct"], 0.065, rel_tol=1e-3)
    assert math.isclose(metrics["avg_loss_pct"], -0.035, rel_tol=1e-3)

def test_sharpe_with_varying_pnl(conn):
    _setup_agent(conn)
    for pnl in [0.02, 0.03, -0.01, 0.04, 0.01, -0.02, 0.03, 0.02, 0.01, -0.01]:
        r = "win" if pnl > 0 else "loss"
        insert_trade(conn, _trade("SOL-PERP", pnl, r))
    metrics = compute_metrics(conn, AGENT_ID)
    assert metrics["sharpe"] > 0
    assert metrics["closed_trades"] == 10

def test_best_and_worst_trade(conn):
    _setup_agent(conn)
    insert_trade(conn, _trade("SOL-PERP", 0.25, "win"))
    insert_trade(conn, _trade("SOL-PERP", -0.12, "loss"))
    metrics = compute_metrics(conn, AGENT_ID)
    assert math.isclose(metrics["best_trade_pct"], 0.25)
    assert math.isclose(metrics["worst_trade_pct"], -0.12)

def test_format_performance_summary(conn):
    _setup_agent(conn)
    insert_trade(conn, _trade("SOL-PERP", 0.05, "win"))
    insert_trade(conn, _trade("BTC-PERP", -0.03, "loss"))
    metrics = compute_metrics(conn, AGENT_ID)
    summary = format_performance_summary(metrics, AGENT_ID)
    assert AGENT_ID in summary
    assert "Win rate" in summary
    assert "Profit factor" in summary

