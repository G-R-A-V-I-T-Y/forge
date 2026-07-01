"""Tests for agents/prompt_builder.py's build_portfolio_snapshot()."""
from store.db import insert_agent, insert_account_snapshot, insert_position, insert_trade
from agents.prompt_builder import build_portfolio_snapshot

AGENT_ID = "jade_hawk"

CONFIG = {
    "desk": {
        "starting_balance": 50000.0,
        "max_concurrent_positions": 3,
    }
}


def test_build_portfolio_snapshot_no_account_defaults_to_starting_balance(conn):
    insert_agent(conn, AGENT_ID, AGENT_ID, "2026-06-29T00:00:00Z", "{}")
    snap = build_portfolio_snapshot(conn, AGENT_ID, CONFIG)
    assert snap["cash"] == 50000.0
    assert snap["equity"] == 50000.0
    assert snap["drawdown_pct"] == 0.0
    assert snap["open_position_count"] == 0
    assert snap["open_positions"] == []
    assert snap["risk_utilization"]["max_concurrent_positions"] == 3
    assert snap["risk_utilization"]["position_utilization_pct"] == 0.0


def test_build_portfolio_snapshot_reflects_account_and_positions(conn):
    insert_agent(conn, AGENT_ID, AGENT_ID, "2026-06-29T00:00:00Z", "{}")
    insert_account_snapshot(conn, AGENT_ID, "paper", 48000.0, 51000.0)
    insert_trade(conn, {
        "id": "t1", "agent_id": AGENT_ID, "thesis_version": 1,
        "account_balance_at_entry": 50000.0, "mode": "paper", "asset": "SOL-PERP",
        "direction": "long", "entry_price": 145.20, "stop_loss_price": 143.00,
        "take_profit_price": 152.00, "leverage": 3, "position_size_pct": 0.10,
        "notional_usd": 5000.0, "entry_timestamp": "2026-06-29T14:00:00Z",
        "status": "open",
    })
    insert_position(conn, {
        "id": "pos1", "agent_id": AGENT_ID, "asset": "SOL-PERP", "direction": "long",
        "entry_price": 145.20, "stop_loss_price": 143.00, "take_profit_price": 152.00,
        "leverage": 3, "position_size_pct": 0.10, "notional_usd": 5000.0,
        "opened_at": "2026-06-29T14:00:00Z", "current_pnl_pct": 0.02,
        "mode": "paper", "trade_id": "t1",
    })

    snap = build_portfolio_snapshot(conn, AGENT_ID, CONFIG)
    assert snap["cash"] == 48000.0
    assert snap["peak_balance"] == 51000.0
    assert snap["drawdown_pct"] == (51000.0 - 48000.0) / 51000.0
    assert snap["open_position_count"] == 1
    assert snap["exposure_usd"] == 5000.0
    assert snap["unrealized_pnl_pct"] == 0.02
    assert snap["open_positions"][0]["asset"] == "SOL-PERP"
    assert snap["risk_utilization"]["open_positions"] == 1
    assert snap["risk_utilization"]["position_utilization_pct"] == 1 / 3


def test_build_portfolio_snapshot_handles_missing_config(conn):
    insert_agent(conn, AGENT_ID, AGENT_ID, "2026-06-29T00:00:00Z", "{}")
    snap = build_portfolio_snapshot(conn, AGENT_ID, config=None)
    assert snap["cash"] == 50000.0  # module default starting_balance
    assert snap["risk_utilization"]["max_concurrent_positions"] is None
    assert snap["risk_utilization"]["position_utilization_pct"] is None
