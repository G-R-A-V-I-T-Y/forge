import json
import sqlite3
from pathlib import Path

import pytest

from store.db import init_schema, insert_account_snapshot, insert_agent, insert_position, insert_trade
from store.positions import execute_close


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    init_schema(c)
    yield c
    c.close()


def _seed_open_trade(conn):
    insert_agent(conn, "sage_turtle", "sage_turtle", "2026-06-29T00:00:00Z", "{}")
    insert_account_snapshot(conn, "sage_turtle", "paper", 50000.0, 50000.0)
    trade = {
        "id": "sage_turtle_20260706_120000_FET",
        "agent_id": "sage_turtle",
        "mode": "paper",
        "asset": "FET-PERP",
        "direction": "short",
        "entry_price": 1.50,
        "stop_loss_price": 1.545,
        "take_profit_price": 1.41,
        "leverage": 3,
        "position_size_pct": 0.10,
        "notional_usd": 5000.0,
        "entry_timestamp": "2026-07-06T12:00:00Z",
        "status": "open",
        "ohlcv_15m_40_blob": b"\x81\xa4test",
    }
    insert_trade(conn, trade)
    # The `positions` table schema (data/schema.sql) has a narrower column
    # set than `trades` -- it has no entry_timestamp/status/blob columns --
    # so build the position row from only the columns positions actually
    # has, rather than `dict(trade)` (which fails insert_position with
    # "table positions has no column named entry_timestamp").
    position = {
        "id": "pos_" + trade["id"],
        "agent_id": trade["agent_id"],
        "asset": trade["asset"],
        "direction": trade["direction"],
        "entry_price": trade["entry_price"],
        "stop_loss_price": trade["stop_loss_price"],
        "take_profit_price": trade["take_profit_price"],
        "leverage": trade["leverage"],
        "position_size_pct": trade["position_size_pct"],
        "notional_usd": trade["notional_usd"],
        "opened_at": trade["entry_timestamp"],
        "mode": trade["mode"],
        "trade_id": trade["id"],
    }
    insert_position(conn, position)
    return position


def test_execute_close_writes_full_trade_record_to_ledger(conn, tmp_path, monkeypatch):
    import store.ledger as ledger_module

    monkeypatch.setattr(ledger_module, "LEDGER_DIR", str(tmp_path / "ledger"))
    position = _seed_open_trade(conn)

    execute_close(
        conn=conn, position_id=position["id"], exit_price=1.44, reason="take_profit",
        config={"taker_fee": 0.00035}, position_dict=position, funding_history=[],
    )

    from datetime import datetime, timezone
    month = f"{datetime.now(timezone.utc):%Y-%m}"
    trades_path = tmp_path / "ledger" / "trades" / f"{month}.jsonl"
    assert trades_path.exists()
    record = json.loads(trades_path.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert record["id"] == position["trade_id"]
    assert record["status"] == "closed"
    assert record["exit_price"] == 1.44
    assert "ohlcv_15m_40_blob" not in record  # excluded: redundant with the market-data ledger, and bytes don't round-trip through JSON


def test_execute_close_writes_account_snapshot_to_ledger(conn, tmp_path, monkeypatch):
    import store.ledger as ledger_module

    monkeypatch.setattr(ledger_module, "LEDGER_DIR", str(tmp_path / "ledger"))
    position = _seed_open_trade(conn)

    execute_close(
        conn=conn, position_id=position["id"], exit_price=1.44, reason="take_profit",
        config={"taker_fee": 0.00035}, position_dict=position, funding_history=[],
    )

    from datetime import datetime, timezone
    month = f"{datetime.now(timezone.utc):%Y-%m}"
    accounts_path = tmp_path / "ledger" / "accounts" / f"{month}.jsonl"
    assert accounts_path.exists()
    record = json.loads(accounts_path.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert record["agent_id"] == "sage_turtle"
    assert record["mode"] == "paper"
    assert record["balance"] > 0
