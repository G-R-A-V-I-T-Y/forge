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


# ── find_first_cross wick-based tests ──────────────────────────────────


def test_find_first_cross_long_sl_hit():
    # Long: price wicks down through SL (1.50) and recovers
    from store.positions import find_first_cross
    candles = [
        [1000, 1.60, 1.62, 1.58, 1.60, 100],   # before entry
        [2000, 1.60, 1.61, 1.48, 1.59, 200],   # wick through 1.50 SL
        [3000, 1.59, 1.60, 1.58, 1.59, 150],
    ]
    cross = find_first_cross(candles, entry_ts_unix=1.5, direction="long", sl=1.50, tp=1.70)
    assert cross is not None
    price, reason = cross
    assert price == 1.50
    assert reason == "stop_loss"


def test_find_first_cross_long_tp_hit():
    # Long: price wicks up through TP (1.70) and falls back
    from store.positions import find_first_cross
    candles = [
        [1000, 1.60, 1.62, 1.58, 1.60, 100],
        [2000, 1.60, 1.72, 1.59, 1.61, 200],   # wick through 1.70 TP
    ]
    cross = find_first_cross(candles, entry_ts_unix=1.5, direction="long", sl=1.50, tp=1.70)
    assert cross is not None
    price, reason = cross
    assert price == 1.70
    assert reason == "take_profit"


def test_wick_through_sl_within_candle_closes():
    # Price wicks through SL (breaks below SL then recovers above it within
    # one 5m candle) → position must close at SL price.
    from store.positions import find_first_cross
    candles = [
        [1000, 1.60, 1.62, 1.58, 1.60, 100],
        [2000, 1.60, 1.61, 1.48, 1.59, 200],   # wick to 1.48, below SL of 1.50
        [3000, 1.59, 1.60, 1.58, 1.59, 150],
    ]
    cross = find_first_cross(candles, entry_ts_unix=1.5, direction="long", sl=1.50, tp=1.70)
    assert cross is not None
    price, reason = cross
    assert price == 1.50
    assert reason == "stop_loss"


def test_wick_sl_before_tp_tiebreak():
    # Both SL and TP are hit in the same candle (wick down to SL then wick
    # up to TP). SL takes priority per exchange behavior.
    from store.positions import find_first_cross
    candles = [
        [1000, 1.60, 1.62, 1.58, 1.60, 100],
        # Same candle: low hits 1.49 (SL at 1.50), high hits 1.72 (TP at 1.70)
        [2000, 1.60, 1.72, 1.49, 1.61, 200],
    ]
    cross = find_first_cross(candles, entry_ts_unix=1.5, direction="long", sl=1.50, tp=1.70)
    assert cross is not None
    price, reason = cross
    # SL should win in the tiebreak
    assert reason == "stop_loss", f"expected stop_loss, got {reason}"
    assert price == 1.50


def test_find_first_cross_short_sl_hit():
    # Short: price wicks up through SL
    from store.positions import find_first_cross
    candles = [
        [1000, 1.60, 1.62, 1.58, 1.60, 100],
        [2000, 1.60, 1.66, 1.59, 1.62, 200],   # wick through 1.65 SL
    ]
    cross = find_first_cross(candles, entry_ts_unix=1.5, direction="short", sl=1.65, tp=1.45)
    assert cross is not None
    price, reason = cross
    assert price == 1.65
    assert reason == "stop_loss"


def test_find_first_cross_no_cross():
    # Price stays within bounds → no cross
    from store.positions import find_first_cross
    candles = [
        [1000, 1.60, 1.62, 1.58, 1.60, 100],
        [2000, 1.60, 1.61, 1.59, 1.60, 200],
    ]
    cross = find_first_cross(candles, entry_ts_unix=1.5, direction="long", sl=1.50, tp=1.70)
    assert cross is None


# ── execute_close with true_notional ───────────────────────────────────


def test_execute_close_with_true_notional(conn, tmp_path, monkeypatch):
    """Closing a trade with true_notional set should compute fees on
    leveraged notional, not on margin."""
    import store.ledger as ledger_module
    monkeypatch.setattr(ledger_module, "LEDGER_DIR", str(tmp_path / "ledger"))

    insert_agent(conn, "test_agent", "test_agent", "2026-07-01T00:00:00Z", "{}")
    insert_account_snapshot(conn, "test_agent", "paper", 50000.0, 50000.0)

    trade = {
        "id": "test_agent_20260706_120000_SOL",
        "agent_id": "test_agent",
        "mode": "paper",
        "asset": "SOL-PERP",
        "direction": "long",
        "entry_price": 100.0,
        "stop_loss_price": 95.0,
        "take_profit_price": 110.0,
        "leverage": 3,
        "position_size_pct": 0.10,
        "notional_usd": 5000.0,  # margin
        "true_notional": 15000.0,  # margin * leverage
        "entry_timestamp": "2026-07-06T12:00:00Z",
        "status": "open",
    }
    insert_trade(conn, trade)
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
        "true_notional": trade["true_notional"],
        "opened_at": trade["entry_timestamp"],
        "mode": trade["mode"],
        "trade_id": trade["id"],
    }
    insert_position(conn, position)

    from store.positions import execute_close
    result = execute_close(
        conn=conn, position_id=position["id"],
        exit_price=110.0, reason="take_profit",
        config={"taker_fee": 0.00035}, position_dict=position,
        funding_history=[],
    )
    # Fees should be on $15k true notional: 15000 * 0.00035 * 2 = $10.50
    assert result["fees_paid"] == pytest.approx(10.50)
    # Gross PnL = notional × price move = 15000 * (110-100)/100 = 1500
    # (leverage already lives inside true_notional — it must not multiply
    # the dollar PnL again).  Net PnL = 1500 - 10.50 = 1489.50.
    assert result["pnl_usd"] == pytest.approx(1489.50, rel=1e-4)
    # Return on the $5k margin: 1489.50 / 5000 = 29.79%
    assert result["pnl_pct"] == pytest.approx(1489.50 / 5000.0, rel=1e-4)


def test_execute_close_with_true_notional_funding(conn, tmp_path, monkeypatch):
    """Funding is computed on true_notional position size (coins = true_notional / entry_price)
    not margin position size (coins = margin / entry_price)."""
    import store.ledger as ledger_module
    monkeypatch.setattr(ledger_module, "LEDGER_DIR", str(tmp_path / "ledger"))

    insert_agent(conn, "fund_test", "fund_test", "2026-07-01T00:00:00Z", "{}")
    insert_account_snapshot(conn, "fund_test", "paper", 50000.0, 50000.0)

    trade = {
        "id": "fund_test_20260706_120000_BTC",
        "agent_id": "fund_test",
        "mode": "paper",
        "asset": "BTC-PERP",
        "direction": "long",
        "entry_price": 60000.0,
        "stop_loss_price": 58000.0,
        "take_profit_price": 65000.0,
        "leverage": 5,
        "position_size_pct": 0.10,
        "notional_usd": 5000.0,   # margin
        "true_notional": 25000.0,  # margin * 5
        "entry_timestamp": "2026-07-06T12:00:00Z",
        "status": "open",
    }
    insert_trade(conn, trade)
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
        "true_notional": trade["true_notional"],
        "opened_at": trade["entry_timestamp"],
        "mode": trade["mode"],
        "trade_id": trade["id"],
    }
    insert_position(conn, position)

    # Funding history with known rates
    funding_history = [
        {"time": 1760000000000, "fundingRate": 0.0001},
        {"time": 1760003600000, "fundingRate": 0.0001},
    ]

    from store.positions import execute_close
    result = execute_close(
        conn=conn, position_id=position["id"],
        exit_price=62000.0, reason="take_profit",
        config={"taker_fee": 0.00035}, position_dict=position,
        funding_history=funding_history,
    )
    # Funding is a fraction of position VALUE: each event pays
    # true_notional × rate = 25000 × 0.0001 = $2.50 — the per-coin price of
    # BTC must not appear in the arithmetic.  The two history events here
    # predate the entry, so with close_ts = now (>72h held) the sparse-
    # sample fallback applies: true_notional × avg_rate × duration_hours,
    # which is well above the two-event floor of $5.
    assert result["funding_paid"] > 0, "funding_paid should be positive cost for long"
    assert result["funding_paid"] >= 5.0, (
        "funding on $25k notional at 1bp/h must be dollars, not the "
        "price-less coin-count arithmetic that produced ~$0.00002"
    )


# ── existing tests below ──────────────────────────────────────────────


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
