"""Tests for store/fingerprint.py."""
import json

import msgpack

from store.db import insert_agent, insert_trade
from store.fingerprint import write_entry, write_outcome, pack_ohlcv, unpack_ohlcv

AGENT_ID = "jade_hawk"


def _base_trade(trade_id="t1", asset="SOL-PERP"):
    return {
        "id": trade_id,
        "agent_id": AGENT_ID,
        "thesis_version": 1,
        "account_balance_at_entry": 50000.0,
        "mode": "paper",
        "asset": asset,
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


def _snapshot():
    return {
        "ohlcv_15m": [[1, 1.0, 2.0, 0.5, 1.5, 100.0]] * 40,
        "ohlcv_1h": [[1, 1.0, 2.0, 0.5, 1.5, 100.0]] * 20,
        "ohlcv_4h": [[1, 1.0, 2.0, 0.5, 1.5, 100.0]] * 10,
        "funding_rate_current": -0.0042,
        "funding_rate_8h_history": [-0.0038, -0.0041, -0.0042],
        "open_interest_usd": 420_000_000,
        "open_interest_24h_change_pct": -3.2,
        "liquidation_volume_1h_usd": 8_500_000,
        "liquidation_direction_dominant": "long",
        "mid_price": 145.2,
        "bid": 145.1,
        "ask": 145.3,
    }


def test_pack_unpack_ohlcv_roundtrip():
    candles = [[1, 1.0, 2.0, 0.5, 1.5, 100.0], [2, 1.5, 2.5, 1.0, 2.0, 80.0]]
    blob = pack_ohlcv(candles)
    assert isinstance(blob, bytes)
    assert unpack_ohlcv(blob) == candles


def test_unpack_ohlcv_handles_none():
    assert unpack_ohlcv(None) == []
    assert unpack_ohlcv(b"") == []


def test_write_entry_enriches_existing_row(conn):
    insert_agent(conn, AGENT_ID, AGENT_ID, "2026-06-29T00:00:00Z", "{}")
    insert_trade(conn, _base_trade())

    write_entry(
        conn, "t1", _snapshot(), regime="range_high_vol",
        reasoning={
            "hypothesis": "SOL funding squeeze setup",
            "key_conditions_met": ["persistent_negative_funding", "support_hold_15m"],
            "key_conditions_missing": ["volume_confirmation"],
            "confidence": 0.68,
            "expected_value": "+0.9% EV",
        },
    )

    row = dict(conn.execute("SELECT * FROM trades WHERE id = ?", ("t1",)).fetchone())
    assert row["regime"] == "range_high_vol"
    assert row["funding_rate_current"] == -0.0042
    assert unpack_ohlcv(row["funding_history_blob"]) == [-0.0038, -0.0041, -0.0042]
    oi = json.loads(row["oi_data_json"])
    assert oi["open_interest_usd"] == 420_000_000
    liq = json.loads(row["liquidation_data_json"])
    assert liq["liquidation_direction_dominant"] == "long"
    assert row["hypothesis"] == "SOL funding squeeze setup"
    assert json.loads(row["key_conditions_met"]) == ["persistent_negative_funding", "support_hold_15m"]
    assert row["confidence"] == 0.68
    assert unpack_ohlcv(row["ohlcv_15m_40_blob"]) == _snapshot()["ohlcv_15m"]
    assert unpack_ohlcv(row["ohlcv_1h_20_blob"]) == _snapshot()["ohlcv_1h"]

    # market_context_json remains whatever was set at trade insert (write_entry
    # writes only to dedicated fingerprint columns)
    assert row.get("market_context_json") is None


def test_write_entry_stores_market_context_msgpack_encoded(conn):
    insert_agent(conn, AGENT_ID, AGENT_ID, "2026-06-29T00:00:00Z", "{}")
    insert_trade(conn, _base_trade())

    market_context = {
        "portfolio": {"cash": 50000.0, "equity": 50000.0, "open_position_count": 1},
        "cross_asset": {"market_breadth": 0.6, "leader": "SOL-PERP"},
        "regime": {"regime_tag": "range_high_vol", "risk_on_score": 0.55},
        "asset": {
            "price": 145.2,
            "funding": -0.0042,
            "candles_5m": [[1, 1.0, 2.0, 0.5, 1.5, 100.0]] * 3,
            "candles_30m": [[1, 1.0, 2.0, 0.5, 1.5, 300.0]],
        },
    }

    write_entry(conn, "t1", _snapshot(), regime="range_high_vol", market_context=market_context)

    row = dict(conn.execute("SELECT * FROM trades WHERE id = ?", ("t1",)).fetchone())
    blob = row["market_context_json"]
    assert isinstance(blob, (bytes, bytearray))
    decoded = msgpack.unpackb(blob, raw=False)
    assert decoded == market_context
    assert decoded["asset"]["candles_5m"] == market_context["asset"]["candles_5m"]


def test_write_entry_without_market_context_leaves_column_null(conn):
    insert_agent(conn, AGENT_ID, AGENT_ID, "2026-06-29T00:00:00Z", "{}")
    insert_trade(conn, _base_trade())
    write_entry(conn, "t1", _snapshot(), regime="range_high_vol")
    row = dict(conn.execute("SELECT * FROM trades WHERE id = ?", ("t1",)).fetchone())
    assert row["market_context_json"] is None


def test_write_entry_without_reasoning_does_not_raise(conn):
    insert_agent(conn, AGENT_ID, AGENT_ID, "2026-06-29T00:00:00Z", "{}")
    insert_trade(conn, _base_trade())
    write_entry(conn, "t1", _snapshot(), regime="trending_bull")
    row = dict(conn.execute("SELECT * FROM trades WHERE id = ?", ("t1",)).fetchone())
    assert row["regime"] == "trending_bull"
    assert row["hypothesis"] == ""


def test_write_outcome_updates_only_known_fields(conn):
    insert_agent(conn, AGENT_ID, AGENT_ID, "2026-06-29T00:00:00Z", "{}")
    insert_trade(conn, _base_trade())

    write_outcome(conn, "t1", {
        "exit_price": 149.60,
        "exit_timestamp": "2026-06-29T16:52:44Z",
        "exit_reason": "take_profit",
        "pnl_pct": 0.031,
        "pnl_usd": 1621.40,
        "result": "win",
        "agent_postmortem": "Setup played out cleanly.",
        "not_a_real_column": "ignored",
    })

    row = dict(conn.execute("SELECT * FROM trades WHERE id = ?", ("t1",)).fetchone())
    assert row["exit_price"] == 149.60
    assert row["result"] == "win"
    assert row["agent_postmortem"] == "Setup played out cleanly."


def test_write_outcome_partial_postmortem_only(conn):
    insert_agent(conn, AGENT_ID, AGENT_ID, "2026-06-29T00:00:00Z", "{}")
    insert_trade(conn, _base_trade())
    write_outcome(conn, "t1", {"agent_postmortem": "Clean execution."})
    row = dict(conn.execute("SELECT * FROM trades WHERE id = ?", ("t1",)).fetchone())
    assert row["agent_postmortem"] == "Clean execution."
    assert row["exit_price"] is None


def test_write_outcome_empty_dict_is_noop(conn):
    insert_agent(conn, AGENT_ID, AGENT_ID, "2026-06-29T00:00:00Z", "{}")
    insert_trade(conn, _base_trade())
    write_outcome(conn, "t1", {})  # should not raise
    row = dict(conn.execute("SELECT * FROM trades WHERE id = ?", ("t1",)).fetchone())
    assert row["status"] == "open"
