import sqlite3
from datetime import datetime, timezone
import pytest
from store.db import (
    get_connection, init_schema, insert_agent, get_agent,
    insert_trade, get_trades, insert_position, get_positions,
    delete_position, insert_account_snapshot, get_latest_account,
)


def test_init_schema_creates_all_tables(tmp_path):
    db_file = str(tmp_path / "test.db")
    conn = get_connection(db_file)
    init_schema(conn)
    tables = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    expected = {"agents", "theses", "trades", "accounts", "positions",
                "reflections", "evaluations", "settings", "chat_history"}
    assert expected.issubset(tables)
    conn.close()


def test_insert_and_get_agent(conn):
    insert_agent(conn, "jade_hawk", "jade_hawk", "2026-06-29T00:00:00Z", "{}")
    agent = get_agent(conn, "jade_hawk")
    assert agent is not None
    assert agent["name"] == "jade_hawk"
    assert agent["status"] == "rookie"


def test_get_agent_missing_returns_none(conn):
    assert get_agent(conn, "does_not_exist") is None


def test_insert_and_get_trades(conn):
    insert_agent(conn, "jade_hawk", "jade_hawk", "2026-06-29T00:00:00Z", "{}")
    trade = {
        "id": "jade_hawk_20260629_143712_SOL",
        "agent_id": "jade_hawk",
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
    }
    insert_trade(conn, trade)
    trades = get_trades(conn, "jade_hawk", limit=10)
    assert len(trades) == 1
    assert trades[0]["asset"] == "SOL-PERP"


def test_positions_crud(conn):
    insert_agent(conn, "jade_hawk", "jade_hawk", "2026-06-29T00:00:00Z", "{}")
    trade = {
        "id": "jade_hawk_20260629_143712_SOL",
        "agent_id": "jade_hawk",
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
    }
    insert_trade(conn, trade)
    pos = {
        "id": "pos_jade_hawk_20260629_143712_SOL",
        "agent_id": "jade_hawk",
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
        "trade_id": "jade_hawk_20260629_143712_SOL",
    }
    insert_position(conn, pos)
    positions = get_positions(conn, "jade_hawk")
    assert len(positions) == 1
    delete_position(conn, "pos_jade_hawk_20260629_143712_SOL")
    assert get_positions(conn, "jade_hawk") == []


def test_account_snapshot(conn):
    insert_agent(conn, "jade_hawk", "jade_hawk", "2026-06-29T00:00:00Z", "{}")
    insert_account_snapshot(conn, "jade_hawk", "paper", 50000.0, 50000.0)
    insert_account_snapshot(conn, "jade_hawk", "paper", 51000.0, 51000.0)
    latest = get_latest_account(conn, "jade_hawk", "paper")
    assert latest["balance"] == 51000.0
