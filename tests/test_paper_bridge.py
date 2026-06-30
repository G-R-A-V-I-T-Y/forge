import pytest
from store.db import init_schema, insert_agent, insert_account_snapshot
from execution.paper_bridge import PaperBridge

AGENT_ID = "jade_hawk"
MARKET_STATE = {
    "SOL-PERP": {
        "mid_price": 145.20,
        "bid": 145.18,
        "ask": 145.22,
    }
}

ORDER = {
    "asset": "SOL-PERP",
    "direction": "long",
    "entry_price": 145.20,
    "stop_loss_price": 143.00,
    "take_profit_price": 152.00,
    "leverage": 3,
    "position_size_pct": 0.10,
}


@pytest.fixture
def bridge(conn):
    insert_agent(conn, AGENT_ID, AGENT_ID, "2026-06-29T00:00:00Z", "{}")
    insert_account_snapshot(conn, AGENT_ID, "paper", 50000.0, 50000.0)
    return PaperBridge(agent_id=AGENT_ID, conn=conn, market_state=MARKET_STATE)


def test_enter_creates_trade_record(bridge, conn):
    fill = bridge.enter(ORDER)
    assert fill["fill_price"] == pytest.approx(145.20, abs=0.01)
    assert "trade_id" in fill

    from store.db import get_trades
    trades = get_trades(conn, AGENT_ID)
    assert len(trades) == 1
    assert trades[0]["status"] == "open"
    assert trades[0]["asset"] == "SOL-PERP"


def test_enter_creates_position_record(bridge, conn):
    bridge.enter(ORDER)
    positions = bridge.get_positions()
    assert len(positions) == 1
    assert positions[0]["asset"] == "SOL-PERP"


def test_enter_debits_account(bridge, conn):
    bridge.enter(ORDER)
    account = bridge.get_account()
    # 10% of 50000 = 5000 notional; balance should reflect open position
    # For M1 we track notional as "reserved" — balance unchanged until close
    assert account["balance"] == pytest.approx(50000.0, abs=1.0)


def test_close_removes_position(bridge, conn):
    fill = bridge.enter(ORDER)
    positions_before = bridge.get_positions()
    assert len(positions_before) == 1
    pos_id = positions_before[0]["id"]
    bridge.close(pos_id, "take_profit")
    assert bridge.get_positions() == []


def test_close_marks_trade_closed(bridge, conn):
    bridge.enter(ORDER)
    pos_id = bridge.get_positions()[0]["id"]
    bridge.close(pos_id, "take_profit")
    from store.db import get_trades
    trades = get_trades(conn, AGENT_ID)
    assert trades[0]["status"] == "closed"
    assert trades[0]["exit_reason"] == "take_profit"
