"""Tests for POST /api/positions/{id}/close."""
from fastapi.testclient import TestClient

from store.db import insert_agent, insert_account_snapshot, insert_trade, insert_position
from web.app import app

AGENT_ID = "jade_hawk"


class FakeProvider:
    """Minimal async market provider returning a fixed price for testing."""

    async def get_orderbook(self, asset, depth=1):
        return {"bids": [[149.00, 1.0]], "asks": [[149.02, 1.0]]}

    async def get_mid_price(self, asset):
        return 149.01


def _seed_open_position(conn):
    insert_agent(conn, AGENT_ID, AGENT_ID, "2026-06-29T00:00:00Z", "{}")
    insert_account_snapshot(conn, AGENT_ID, "paper", 50000.0, 50000.0)
    insert_trade(conn, {
        "id": "t1",
        "agent_id": AGENT_ID,
        "thesis_version": 1,
        "account_balance_at_entry": 50000.0,
        "mode": "paper",
        "asset": "SOL-PERP",
        "direction": "long",
        "entry_price": 145.20,
        "stop_loss_price": 143.00,
        "take_profit_price": 152.00,
        "leverage": 3,
        "position_size_pct": 0.10,
        "notional_usd": 5000.0,
        "entry_timestamp": "2026-06-29T14:37:12Z",
        "status": "open",
    })
    insert_position(conn, {
        "id": "pos_t1",
        "agent_id": AGENT_ID,
        "asset": "SOL-PERP",
        "direction": "long",
        "entry_price": 145.20,
        "stop_loss_price": 143.00,
        "take_profit_price": 152.00,
        "leverage": 3,
        "position_size_pct": 0.10,
        "notional_usd": 5000.0,
        "opened_at": "2026-06-29T14:37:12Z",
        "mode": "paper",
        "trade_id": "t1",
    })


def _client(conn) -> TestClient:
    app.state.conn = conn
    app.state.provider = FakeProvider()
    app.state.config = None
    return TestClient(app)


def test_close_position_success(conn):
    _seed_open_position(conn)
    r = _client(conn).post("/api/positions/pos_t1/close")
    assert r.status_code == 200
    data = r.json()
    assert data["trade_id"] == "t1"
    assert "exit_price" in data
    assert "pnl_pct" in data
    assert "pnl_usd" in data

    trade = conn.execute("SELECT * FROM trades WHERE id = ?", ("t1",)).fetchone()
    assert trade["status"] == "closed"
    assert trade["exit_reason"] == "manual_close"

    position = conn.execute("SELECT * FROM positions WHERE id = ?", ("pos_t1",)).fetchone()
    assert position is None


def test_close_position_not_found(conn):
    _seed_open_position(conn)
    r = _client(conn).post("/api/positions/does_not_exist/close")
    assert r.status_code == 404
    assert "error" in r.json()
