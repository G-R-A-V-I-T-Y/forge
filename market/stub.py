"""Hardcoded deterministic market data for skeleton testing."""
import time

# Reference prices for each asset (close to real prices as of mid-2026)
_PRICES = {
    "BTC-PERP":   65_000.0,
    "ETH-PERP":    3_500.0,
    "SOL-PERP":      145.2,
    "BNB-PERP":      580.0,
    "XRP-PERP":        0.52,
    "DOGE-PERP":      0.12,
    "AVAX-PERP":      38.0,
    "LINK-PERP":      14.5,
    "ARB-PERP":        1.05,
    "OP-PERP":         2.40,
    "SUI-PERP":        1.80,
    "TON-PERP":        7.20,
    "PEPE-PERP":  0.0000142,
    "WIF-PERP":        2.10,
    "TRUMP-PERP":     12.50,
}

_FUNDING = {
    "BTC-PERP":  0.0001,
    "ETH-PERP":  0.0002,
    "SOL-PERP": -0.0042,   # negative — short pressure
    "BNB-PERP":  0.0003,
    "XRP-PERP": -0.0015,
    "DOGE-PERP": 0.0005,
    "AVAX-PERP": 0.0001,
    "LINK-PERP": 0.0002,
    "ARB-PERP":  0.0003,
    "OP-PERP":   0.0001,
    "SUI-PERP":  0.0008,
    "TON-PERP":  0.0002,
    "PEPE-PERP": 0.0010,
    "WIF-PERP":  0.0015,
    "TRUMP-PERP": -0.0020,
}


def _make_candles(price: float, n: int, interval_seconds: int) -> list:
    now_ms = int(time.time() * 1000)
    candles = []
    for i in range(n - 1, -1, -1):
        ts = now_ms - i * interval_seconds * 1000
        # Simulate mild oscillation around reference price
        offset = price * 0.002 * ((i % 5) - 2)
        o = price + offset
        h = o * 1.003
        l = o * 0.997
        c = o + price * 0.001
        v = price * 500
        candles.append([ts, round(o, 6), round(h, 6), round(l, 6), round(c, 6), round(v, 2)])
    return candles


def get_market_state(assets: list[str]) -> dict:
    state = {}
    for asset in assets:
        price = _PRICES.get(asset, 100.0)
        funding = _FUNDING.get(asset, 0.0001)
        spread = price * 0.0001
        state[asset] = {
            "ohlcv_15m": _make_candles(price, 40, 900),
            "ohlcv_1h": _make_candles(price, 20, 3600),
            "ohlcv_4h": _make_candles(price, 10, 14400),
            "mid_price": price,
            "bid": round(price - spread, 6),
            "ask": round(price + spread, 6),
            "funding_rate_current": funding,
            "funding_rate_8h_history": [funding * 0.9, funding * 0.95, funding],
            "open_interest_usd": 420_000_000,
            "open_interest_24h_change_pct": -3.2,
            "liquidation_volume_1h_usd": 8_500_000,
            "liquidation_direction_dominant": "long",
        }
    return state
