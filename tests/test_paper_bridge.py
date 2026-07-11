from datetime import datetime, timedelta, timezone

import pytest
from store.db import init_schema, insert_agent, insert_account_snapshot
from execution.paper_bridge import PaperBridge
from market.heartbeat import write_heartbeat

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


def _heartbeat_packet(price: float, timestamp: str | None = None) -> dict:
    ts = timestamp or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "timestamp": ts,
        "assets": {"SOL-PERP": {"price": price}},
        "cross_asset": {},
        "regime": {},
    }


def _config(heartbeat_path: str) -> dict:
    return {
        "desk": {
            "starting_balance": 50000.0,
            "heartbeat_path": heartbeat_path,
            "heartbeat_interval_seconds": 300,
        }
    }


@pytest.fixture
def bridge(conn, tmp_path):
    insert_agent(conn, AGENT_ID, AGENT_ID, "2026-06-29T00:00:00Z", "{}")
    insert_account_snapshot(conn, AGENT_ID, "paper", 50000.0, 50000.0)
    heartbeat_path = str(tmp_path / "heartbeat.json")
    write_heartbeat(heartbeat_path, _heartbeat_packet(145.20))
    return PaperBridge(
        agent_id=AGENT_ID, conn=conn, provider=None, config=_config(heartbeat_path)
    )


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
async def test_close_updates_account_balance(bridge, conn, tmp_path):
    await bridge.enter(ORDER)

    # Rewrite the heartbeat with a higher price before closing.
    heartbeat_path = bridge.config["desk"]["heartbeat_path"]
    write_heartbeat(heartbeat_path, _heartbeat_packet(149.01))

    pos_id = bridge.get_positions()[0]["id"]
    result = await bridge.close(pos_id, "take_profit")

    # With true_notional (3x leverage), net pnl_pct = net_pnl_usd / margin
    # Gross PnL on $15k true notional at 0.0787 = $1180.79
    # Fees = $15k * 0.00035 * 2 = $10.50
    # Net = $1170.29 / $5k margin = 0.234
    expected_net_pnl_pct = (
        15000.0 * (149.01 - 145.20) / 145.20 * 3 - 15000.0 * 0.00035 * 2
    ) / (15000.0 / 3)
    assert result["pnl_pct"] == pytest.approx(expected_net_pnl_pct, rel=0.01)

    account = await bridge.get_account()
    assert account["balance"] > 50000.0
    assert account["peak"] >= account["balance"]


@pytest.mark.asyncio
async def test_fill_price_raises_when_heartbeat_missing(conn, tmp_path):
    insert_agent(conn, AGENT_ID, AGENT_ID, "2026-06-29T00:00:00Z", "{}")
    insert_account_snapshot(conn, AGENT_ID, "paper", 50000.0, 50000.0)
    missing_path = str(tmp_path / "does_not_exist.json")
    b = PaperBridge(agent_id=AGENT_ID, conn=conn, provider=None, config=_config(missing_path))

    with pytest.raises(RuntimeError, match="heartbeat data unavailable or stale"):
        await b.enter(ORDER)


@pytest.mark.asyncio
async def test_fill_price_raises_when_heartbeat_stale(conn, tmp_path):
    insert_agent(conn, AGENT_ID, AGENT_ID, "2026-06-29T00:00:00Z", "{}")
    insert_account_snapshot(conn, AGENT_ID, "paper", 50000.0, 50000.0)
    heartbeat_path = str(tmp_path / "heartbeat.json")
    stale_ts = (datetime.now(timezone.utc) - timedelta(seconds=1000)).strftime("%Y-%m-%dT%H:%M:%SZ")
    write_heartbeat(heartbeat_path, _heartbeat_packet(145.20, timestamp=stale_ts))
    b = PaperBridge(agent_id=AGENT_ID, conn=conn, provider=None, config=_config(heartbeat_path))

    with pytest.raises(RuntimeError, match="heartbeat data unavailable or stale"):
        await b.enter(ORDER)


@pytest.mark.asyncio
async def test_fill_price_raises_when_asset_missing_from_heartbeat(conn, tmp_path):
    insert_agent(conn, AGENT_ID, AGENT_ID, "2026-06-29T00:00:00Z", "{}")
    insert_account_snapshot(conn, AGENT_ID, "paper", 50000.0, 50000.0)
    heartbeat_path = str(tmp_path / "heartbeat.json")
    write_heartbeat(heartbeat_path, {
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "assets": {},
        "cross_asset": {},
        "regime": {},
    })
    b = PaperBridge(agent_id=AGENT_ID, conn=conn, provider=None, config=_config(heartbeat_path))

    with pytest.raises(RuntimeError, match="heartbeat data unavailable or stale"):
        await b.enter(ORDER)
