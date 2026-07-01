"""Regression test for the M4 'Done when' SQLite size budget: 500 trades
with full OHLCV fingerprints must stay well under 50MB. Runs at a smaller
scale (50 trades) here to keep the suite fast, and extrapolates.

Task B (heartbeat wiring) added a much richer `market_context_json` blob
per trade — the consolidated portfolio/cross_asset/regime/asset "trade
thumbprint", including the asset's candles_5m (300 candles),
candles_30m (~50), and candles_4h (~6) OHLCV series (see
agents/decision_loop.py's build_trade_market_context() and
market/heartbeat.py's per-asset candle fields). This test builds a
realistic, heartbeat-shaped market_context fixture (not an empty dict) so
the measured size below reflects the real cost of that richer capture, and
the budget assertion was re-verified against it (see the design doc
addendum for the actual measured numbers)."""
import os

from store.db import get_connection, init_schema, insert_agent, insert_trade
from store.fingerprint import write_entry, write_outcome

N_TRADES = 50
BUDGET_MB_PER_500 = 50.0


def _candles(n, base=100.0):
    return [[1700000000000 + i * 900_000, base, base * 1.004, base * 0.996,
             base * 1.001, 12345.0] for i in range(n)]


def _realistic_asset_fields(asset="SOL-PERP"):
    """A full per-asset heartbeat field dict (~29 derived fields plus the
    three OHLCV candle series), matching market/heartbeat.py's
    PER_ASSET_FIELDS shape and real candle counts (300 x 5m, 50 x 30m,
    ~6 x 4h)."""
    return {
        "price": 145.20, "return_5m": 0.0012, "return_30m": 0.004,
        "return_4h": 0.018, "return_24h": 0.052, "volume": 128934.5,
        "open_interest": 420_000_000.0, "funding": -0.0042, "spread": 0.0004,
        "atr": 3.21, "realized_vol": 0.58, "rsi": 61.3, "ema20": 144.9,
        "ema50": 143.2, "ema200": 139.8, "vwap_distance": 0.0021,
        "volume_zscore": 0.8, "funding_zscore": -1.2, "oi_zscore": 0.4,
        "bid_depth": 5210.0, "ask_depth": 4830.0, "depth_imbalance": 0.04,
        "top5_imbalance": 0.04, "slippage_estimate": 0.0006,
        "buy_volume": 610.2, "sell_volume": 590.4, "aggressor_ratio": 0.508,
        "avg_trade_size": 1.8, "largest_trade": 42000.0,
        "candles_5m": _candles(300),
        "candles_30m": _candles(50),
        "candles_4h": _candles(6),
    }


def _realistic_market_context(asset="SOL-PERP"):
    return {
        "portfolio": {
            "cash": 48213.55, "equity": 48213.55, "peak_balance": 51004.20,
            "drawdown_pct": 0.0547, "exposure_usd": 5000.0,
            "unrealized_pnl_pct": 0.021, "open_position_count": 1,
            "open_positions": [{
                "id": "pos1", "agent_id": "jade_hawk", "asset": asset,
                "direction": "long", "entry_price": 145.20,
                "stop_loss_price": 143.00, "take_profit_price": 152.00,
                "leverage": 3, "position_size_pct": 0.10,
                "notional_usd": 5000.0, "opened_at": "2026-06-29T14:00:00Z",
                "current_pnl_pct": 0.021, "mode": "paper", "trade_id": "t0",
            }],
            "performance": {
                "win_rate": 0.62, "profit_factor": 1.8, "avg_win_pct": 0.025,
                "avg_loss_pct": -0.014, "avg_wl_ratio": 1.79, "sharpe": 1.1,
                "total_trades": 34, "closed_trades": 33, "best_trade_pct": 0.09,
                "worst_trade_pct": -0.04, "by_regime": {}, "last_20_win_rate": 0.6,
                "last_20_pf": 1.7, "last_7d_return": 0.03,
            },
            "risk_utilization": {
                "open_positions": 1, "max_concurrent_positions": 3,
                "position_utilization_pct": 0.333,
            },
        },
        "cross_asset": {
            "market_breadth": 0.6, "average_return": 0.021, "median_return": 0.018,
            "leader": "SOL-PERP", "laggard": "XLM-PERP",
            "correlation_matrix": {a: {b: 0.42 for b in range(20)} for a in range(20)},
            "pca": {"explained_variance_ratio": [0.61, 0.22, 0.09],
                    "first_component_loadings": {f"A{i}-PERP": 0.1 * i for i in range(20)}},
            "sector_strength": {
                "L1": 0.03, "L2": 0.02, "Modular_DA": 0.01, "DeFi_Oracle": 0.015,
                "AI": 0.04, "Exchange": 0.02, "Legacy_Payments": -0.01,
            },
            "momentum_rankings": [f"A{i}-PERP" for i in range(20)],
            "relative_strength": {f"A{i}-PERP": 0.05 * i for i in range(20)},
        },
        "regime": {
            "crypto_fear_index": 42, "btc_dominance": 0.51,
            "average_volatility": 0.55, "average_funding": -0.001,
            "average_oi_growth": 0.02, "market_breadth": 0.6,
            "risk_on_score": 0.55, "trend_score": 0.2,
            "regime_tag": "range_high_vol",
        },
        "asset": _realistic_asset_fields(asset),
    }


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
    market_context = _realistic_market_context()

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
        write_entry(conn, trade_id, snapshot, regime="range_high_vol",
                    reasoning=reasoning, market_context=market_context)
        write_outcome(conn, trade_id, {
            "exit_price": 149.60, "pnl_pct": 0.031, "pnl_usd": 1621.40,
            "result": "win", "agent_postmortem": "Setup played out cleanly.",
        })

    conn.execute("VACUUM")
    conn.commit()
    conn.close()

    size_mb = os.path.getsize(db_path) / (1024 * 1024)
    per_trade_kb = (size_mb * 1024) / N_TRADES
    extrapolated_500 = size_mb * (500 / N_TRADES)
    print(
        f"\n[test_db_size] {N_TRADES} trades (with realistic market_context_json) "
        f"= {size_mb:.3f}MB total, {per_trade_kb:.1f}KB/trade; "
        f"extrapolated to 500 trades = {extrapolated_500:.1f}MB"
    )
    assert extrapolated_500 < BUDGET_MB_PER_500, (
        f"{N_TRADES} trades = {size_mb:.3f}MB; extrapolated to 500 trades = "
        f"{extrapolated_500:.1f}MB, exceeds the {BUDGET_MB_PER_500}MB budget"
    )
