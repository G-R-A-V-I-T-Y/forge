"""Tests for the /trades page and /api/query, /api/trades/{id} endpoints."""
from fastapi.testclient import TestClient

from store.db import insert_agent, insert_trade
from store.fingerprint import write_entry, write_outcome
from web.app import app

AGENT_ID = "jade_hawk"


def _seed(conn):
    insert_agent(conn, AGENT_ID, AGENT_ID, "2026-06-29T00:00:00Z", "{}")
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
        "status": "closed",
    })
    write_entry(conn, "t1", {
        "ohlcv_15m": [[1, 1.0, 2.0, 0.5, 1.5, 100.0]] * 5,
        "funding_rate_current": -0.0042,
        "open_interest_24h_change_pct": -3.2,
    }, regime="range_high_vol", reasoning={
        "hypothesis": "squeeze setup",
        "key_conditions_met": ["a"],
        "key_conditions_missing": ["b"],
        "confidence": 0.68,
        "expected_value": "+0.9%",
    })
    write_outcome(conn, "t1", {"exit_price": 149.6, "pnl_pct": 0.031, "result": "win"})


def _client(conn) -> TestClient:
    app.state.conn = conn
    return TestClient(app)


def test_trades_page_renders(conn):
    _seed(conn)
    r = _client(conn).get("/trades")
    assert r.status_code == 200
    assert "SOL-PERP" in r.text
    assert "Trade Bank" in r.text


def test_trades_page_filters_apply(conn):
    _seed(conn)
    r = _client(conn).get("/trades", params={"asset": "ETH-PERP"})
    assert r.status_code == 200
    assert "No trades match these filters" in r.text


def test_api_query_returns_json_list(conn):
    _seed(conn)
    r = _client(conn).get("/api/query", params={"asset": "SOL-PERP", "direction": "long"})
    assert r.status_code == 200
    data = r.json()
    assert len(data) == 1
    assert data[0]["id"] == "t1"
    assert "ohlcv_15m_40_blob" not in data[0]
    assert "ohlcv_15m" not in data[0]  # omitted by default (include_ohlcv=false)


def test_api_query_negative_funding_win_rate(conn):
    _seed(conn)
    r = _client(conn).get("/api/query", params={
        "asset": "SOL-PERP", "direction": "long", "status": "closed", "funding_rate_max": 0,
    })
    data = r.json()
    assert len(data) == 1
    assert data[0]["result"] == "win"


def test_api_trade_detail_includes_ohlcv(conn):
    _seed(conn)
    r = _client(conn).get("/api/trades/t1")
    assert r.status_code == 200
    data = r.json()
    assert data["ohlcv_15m"] == [[1, 1.0, 2.0, 0.5, 1.5, 100.0]] * 5
    assert data["regime"] == "range_high_vol"


def test_api_trade_detail_404(conn):
    _seed(conn)
    r = _client(conn).get("/api/trades/does_not_exist")
    assert r.status_code == 404
