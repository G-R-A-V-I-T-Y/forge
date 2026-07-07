import json
import sqlite3

import pytest

from store.db import init_schema, insert_account_snapshot, insert_agent, insert_position, insert_trade
from store.state_snapshot import write_current_state


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    init_schema(c)
    yield c
    c.close()


def test_write_current_state_captures_agents_and_balances(conn, tmp_path):
    insert_agent(conn, "sage_turtle", "sage_turtle", "2026-06-29T00:00:00Z", "{}")
    insert_account_snapshot(conn, "sage_turtle", "paper", 51200.0, 52000.0)

    path = str(tmp_path / "state" / "current.json")
    write_current_state(conn, path)

    state = json.loads((tmp_path / "state" / "current.json").read_text(encoding="utf-8"))
    assert state["agents"][0]["id"] == "sage_turtle"
    assert state["agents"][0]["paper_balance"] == 51200.0


def test_write_current_state_captures_open_positions(conn, tmp_path):
    insert_agent(conn, "sage_turtle", "sage_turtle", "2026-06-29T00:00:00Z", "{}")
    insert_account_snapshot(conn, "sage_turtle", "paper", 50000.0, 50000.0)
    trade = {
        "id": "t1", "agent_id": "sage_turtle", "mode": "paper", "asset": "FET-PERP",
        "direction": "short", "entry_price": 1.5, "status": "open",
    }
    insert_trade(conn, trade)
    insert_position(conn, {
        "id": "pos_t1", "agent_id": "sage_turtle", "asset": "FET-PERP", "direction": "short",
        "entry_price": 1.5, "stop_loss_price": 1.545, "take_profit_price": 1.41,
        "leverage": 3, "position_size_pct": 0.10, "notional_usd": 5000.0,
        "opened_at": "2026-07-06T12:00:00Z", "mode": "paper", "trade_id": "t1",
    })

    path = str(tmp_path / "state" / "current.json")
    write_current_state(conn, path)

    state = json.loads((tmp_path / "state" / "current.json").read_text(encoding="utf-8"))
    assert len(state["open_positions"]) == 1
    assert state["open_positions"][0]["id"] == "pos_t1"


def test_write_current_state_is_atomic(conn, tmp_path):
    """No .tmp file left behind after a successful write."""
    insert_agent(conn, "sage_turtle", "sage_turtle", "2026-06-29T00:00:00Z", "{}")
    path = str(tmp_path / "state" / "current.json")
    write_current_state(conn, path)

    assert not (tmp_path / "state" / "current.json.tmp").exists()
