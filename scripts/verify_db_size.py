"""Verify that 500 simulated trades with full OHLCV blobs stay under 50MB."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from store.db import get_connection, init_schema, insert_agent, insert_trade  # noqa: E402
from store.fingerprint import write_entry, write_outcome  # noqa: E402

N_TRADES = 500
BUDGET_MB = 50.0


def _candles(n, base=100.0):
    return [[1700000000000 + i * 900_000, base, base * 1.004, base * 0.996,
             base * 1.001, 12345.0] for i in range(n)]


def main():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
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
            "hypothesis": "SOL funding squeeze setup with sustained short pressure",
            "key_conditions_met": ["persistent_negative_funding", "support_hold_15m"],
            "key_conditions_missing": ["volume_confirmation_on_bounce"],
            "confidence": 0.68,
            "expected_value": "+0.9% EV",
        }

        for i in range(N_TRADES):
            trade_id = f"trade_{i:04d}"
            insert_trade(conn, {
                "id": trade_id, "agent_id": "jade_hawk", "thesis_version": 1,
                "account_balance_at_entry": 50000.0, "mode": "paper",
                "asset": "SOL-PERP", "direction": "long", "entry_price": 145.20,
                "stop_loss_price": 143.00, "take_profit_price": 152.00,
                "leverage": 3, "position_size_pct": 0.10, "notional_usd": 5000.0,
                "entry_timestamp": "2026-06-29T14:37:12Z", "status": "closed",
            })
            write_entry(conn, trade_id, snapshot, regime="range_high_vol",
                        reasoning=reasoning)
            write_outcome(conn, trade_id, {
                "exit_price": 149.60, "pnl_pct": 0.031, "pnl_usd": 1621.40,
                "result": "win", "agent_postmortem": "Setup played out cleanly.",
            })

        conn.execute("VACUUM")
        conn.commit()
        conn.close()

        size_mb = os.path.getsize(db_path) / (1024 * 1024)
        ok = size_mb < BUDGET_MB
        status = "PASS" if ok else "FAIL"
        print(f"[{status}] {N_TRADES} trades with full OHLCV fingerprints = {size_mb:.3f}MB "
              f"(budget: {BUDGET_MB}MB)")
        if not ok:
            sys.exit(1)
    finally:
        if os.path.exists(db_path):
            os.unlink(db_path)


if __name__ == "__main__":
    main()
