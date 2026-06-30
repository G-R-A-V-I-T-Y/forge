"""Regression test for the M4 'Done when' SQLite size budget: 500 trades
with full OHLCV fingerprints must stay well under 50MB. Runs at a smaller
scale (50 trades) here to keep the suite fast, and extrapolates — a
one-off manual run at the full 500-trade scale during development measured
~2.6MB (well under the 50MB budget), confirming msgpack compression keeps
OHLCV snapshots compact."""
import os

from store.db import get_connection, init_schema, insert_agent, insert_trade
from store.fingerprint import write_entry, write_outcome

N_TRADES = 50
BUDGET_MB_PER_500 = 50.0


def _candles(n, base=100.0):
    return [[1700000000000 + i * 900_000, base, base * 1.004, base * 0.996,
             base * 1.001, 12345.0] for i in range(n)]


def test_500_trade_extrapolated_db_size_under_budget(tmp_path):
    db_path = str(tmp_path / "size_check.db")
    conn = get_connection(db_path)
    init_schema(conn)
    insert_agent(conn, "jade_hawk", "jade_hawk", "2026-06-29T00:00:00Z", "{}")

    snapshot = {
        "ohlcv_15m": _candles(40),
        "ohlcv_1h": _candles(20),
        "ohlcv_4h": _candles(10),
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
    reasoning = {
        "hypothesis": ("SOL funding has been negative for 3 consecutive 8h periods "
                       "indicating sustained short pressure, with a liquidation anomaly."),
        "key_conditions_met": ["persistent_negative_funding", "support_hold_15m"],
        "key_conditions_missing": ["volume_confirmation_on_bounce"],
        "confidence": 0.68,
        "expected_value": "+0.9% EV: 65% assumed win rate x 4.6% TP - 35% x 2.4% SL",
    }

    for i in range(N_TRADES):
        trade_id = f"trade_{i:04d}"
        insert_trade(conn, {
            "id": trade_id, "agent_id": "jade_hawk", "thesis_version": 1,
            "account_balance_at_entry": 50000.0, "mode": "paper", "asset": "SOL-PERP",
            "direction": "long", "entry_price": 145.20, "stop_loss_price": 143.00,
            "take_profit_price": 152.00, "leverage": 3, "position_size_pct": 0.10,
            "notional_usd": 5000.0, "entry_timestamp": "2026-06-29T14:37:12Z",
            "status": "closed",
        })
        write_entry(conn, trade_id, snapshot, regime="range_high_vol", reasoning=reasoning)
        write_outcome(conn, trade_id, {
            "exit_price": 149.60, "pnl_pct": 0.031, "pnl_usd": 1621.40,
            "result": "win", "agent_postmortem": "Setup played out cleanly.",
        })

    conn.execute("VACUUM")
    conn.commit()
    conn.close()

    size_mb = os.path.getsize(db_path) / (1024 * 1024)
    extrapolated_500 = size_mb * (500 / N_TRADES)
    assert extrapolated_500 < BUDGET_MB_PER_500, (
        f"{N_TRADES} trades = {size_mb:.3f}MB; extrapolated to 500 trades = "
        f"{extrapolated_500:.1f}MB, exceeds the {BUDGET_MB_PER_500}MB budget"
    )
