import pytest
from store.db import init_schema, insert_agent, insert_account_snapshot
from execution.paper_bridge import PaperBridge

AGENT_ID = "jade_hawk"

ORDER = {
    "asset": "SOL-PERP",
    "direction": "long",
    "entry_price": 145.20,
    "stop_loss_price": 143.00,
    "take_profit_price": 152.00,
    "leverage": 3,
    "position_size_pct": 0.10,
}


class FakeProvider:
    """Minimal async market provider returning fixed prices for testing."""

    async def get_orderbook(self, asset, depth=1):
        return {"bids": [[145.18, 1.0]], "asks": [[145.22, 1.0]]}

    async def get_mid_price(self, asset):
        return 145.20

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


@pytest.fixture
def bridge(conn):
    insert_agent(conn, AGENT_ID, AGENT_ID, "2026-06-29T00:00:00Z", "{}")
    insert_account_snapshot(conn, AGENT_ID, "paper", 50000.0, 50000.0)
    return PaperBridge(agent_id=AGENT_ID, conn=conn, provider=FakeProvider())


@pytest.mark.asyncio
async def test_enter_creates_trade_record(bridge, conn):
    fill = await bridge.enter(ORDER)
    assert fill["fill_price"] == pytest.approx(145.20, abs=0.01)
    assert "trade_id" in fill

    from store.db import get_trades
    trades = get_trades(conn, AGENT_ID)
    assert len(trades) == 1
    assert trades[0]["status"] == "open"
    assert trades[0]["asset"] == "SOL-PERP"


@pytest.mark.asyncio
async def test_enter_creates_position_record(bridge, conn):
    await bridge.enter(ORDER)
    positions = bridge.get_positions()
    assert len(positions) == 1
    assert positions[0]["asset"] == "SOL-PERP"


@pytest.mark.asyncio
async def test_enter_debits_account(bridge, conn):
    await bridge.enter(ORDER)
    account = await bridge.get_account()
    assert account["balance"] == pytest.approx(50000.0, abs=1.0)


@pytest.mark.asyncio
async def test_close_removes_position(bridge, conn):
    await bridge.enter(ORDER)
    positions_before = bridge.get_positions()
    assert len(positions_before) == 1
    pos_id = positions_before[0]["id"]
    await bridge.close(pos_id, "take_profit")
    assert bridge.get_positions() == []


@pytest.mark.asyncio
async def test_close_marks_trade_closed(bridge, conn):
    await bridge.enter(ORDER)
    pos_id = bridge.get_positions()[0]["id"]
    await bridge.close(pos_id, "take_profit")
    from store.db import get_trades
    trades = get_trades(conn, AGENT_ID)
    assert trades[0]["status"] == "closed"
    assert trades[0]["exit_reason"] == "take_profit"


@pytest.mark.asyncio
async def test_close_updates_account_balance(bridge, conn):
    await bridge.enter(ORDER)

    class ProfitProvider:
        async def get_orderbook(self, asset, depth=1):
            return {"bids": [[149.00, 1.0]], "asks": [[149.02, 1.0]]}
        async def get_mid_price(self, asset):
            return 149.01
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass

    bridge.provider = ProfitProvider()

    pos_id = bridge.get_positions()[0]["id"]
    result = await bridge.close(pos_id, "take_profit")

    expected_pnl_pct = (149.01 - 145.20) / 145.20 * 3
    assert result["pnl_pct"] == pytest.approx(expected_pnl_pct, rel=0.01)

    account = await bridge.get_account()
    assert account["balance"] > 50000.0
    assert account["peak"] >= account["balance"]
