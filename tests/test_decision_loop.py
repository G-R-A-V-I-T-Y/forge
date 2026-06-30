import pytest
from store.db import init_schema, insert_agent, insert_account_snapshot
from agents.decision_loop import run_decision
from market.stub import get_market_state
from llm.stub import decide
from execution.paper_bridge import PaperBridge

AGENT_ID = "jade_hawk"
THESIS = "Funding rate mean reversion: persistent negative funding signals short squeeze."
CONFIG = {
    "universe": ["SOL-PERP"],
    "desk": {
        "starting_balance": 50000.0,
        "max_leverage": 10,
        "max_position_size_pct": 0.20,
        "max_concurrent_positions": 3,
        "drawdown_kill_pct": 0.15,
    },
}


def bridge_factory(agent_id, conn, market_state):
    return PaperBridge(agent_id=agent_id, conn=conn, market_state=market_state)


def test_decision_loop_enter_creates_trade(conn):
    insert_agent(conn, AGENT_ID, AGENT_ID, "2026-06-29T00:00:00Z", "{}")
    insert_account_snapshot(conn, AGENT_ID, "paper", 50000.0, 50000.0)

    result = run_decision(
        agent_id=AGENT_ID,
        thesis_text=THESIS,
        config=CONFIG,
        conn=conn,
        get_market_fn=get_market_state,
        llm_fn=decide,
        bridge_factory=bridge_factory,
    )

    assert result["action"] == "enter"
    from store.db import get_trades
    trades = get_trades(conn, AGENT_ID)
    assert len(trades) == 1


def test_decision_loop_risk_block_does_not_create_trade(conn):
    insert_agent(conn, AGENT_ID, AGENT_ID, "2026-06-29T00:00:00Z", "{}")
    insert_account_snapshot(conn, AGENT_ID, "paper", 50000.0, 50000.0)

    # LLM that returns an invalid order (leverage over cap)
    def bad_llm(sys, prompt):
        return {
            "action": "enter",
            "asset": "SOL-PERP",
            "direction": "long",
            "entry_price": 145.20,
            "stop_loss_price": 143.00,
            "take_profit_price": 152.00,
            "leverage": 15,  # over cap
            "position_size_pct": 0.10,
            "hypothesis": "test",
            "key_conditions_met": [],
            "key_conditions_missing": [],
            "confidence": 0.5,
            "expected_value": "test",
        }

    result = run_decision(
        agent_id=AGENT_ID,
        thesis_text=THESIS,
        config=CONFIG,
        conn=conn,
        get_market_fn=get_market_state,
        llm_fn=bad_llm,
        bridge_factory=bridge_factory,
    )

    assert result["action"] == "risk_blocked"
    from store.db import get_trades
    assert get_trades(conn, AGENT_ID) == []


def test_decision_loop_wait_does_not_create_trade(conn):
    insert_agent(conn, AGENT_ID, AGENT_ID, "2026-06-29T00:00:00Z", "{}")
    insert_account_snapshot(conn, AGENT_ID, "paper", 50000.0, 50000.0)

    def wait_llm(sys, prompt):
        return {"action": "wait", "reason": "no setup fits thesis today"}

    result = run_decision(
        agent_id=AGENT_ID,
        thesis_text=THESIS,
        config=CONFIG,
        conn=conn,
        get_market_fn=get_market_state,
        llm_fn=wait_llm,
        bridge_factory=bridge_factory,
    )

    assert result["action"] == "wait"
    from store.db import get_trades
    assert get_trades(conn, AGENT_ID) == []
