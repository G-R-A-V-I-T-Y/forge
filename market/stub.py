"""Hardcoded deterministic market data for skeleton testing."""
import time

# Reference prices for each asset (close to real prices as of mid-2026)
_PRICES = {
    "BTC-PERP": 65_000.0,
    "ETH-PERP": 3_500.0,
    "SOL-PERP": 145.2,
    "BNB-PERP": 580.0,
    "XRP-PERP": 0.52,
    "DOGE-PERP": 0.12,
    "AVAX-PERP": 38.0,
    "LINK-PERP": 14.5,
    "ARB-PERP": 1.05,
    "OP-PERP": 2.40,
    "SUI-PERP": 1.80,
    "TON-PERP": 7.20,
    "PEPE-PERP": 0.0000142,
    "WIF-PERP": 2.10,
    "TRUMP-PERP": 12.50,
}

_FUNDING = {
    "BTC-PERP": 0.0001,
    "ETH-PERP": 0.0002,
    "SOL-PERP": -0.0042,  # negative — short pressure
    "BNB-PERP": 0.0003,
    "XRP-PERP": -0.0015,
    "DOGE-PERP": 0.0005,
    "AVAX-PERP": 0.0001,
    "LINK-PERP": 0.0002,
    "ARB-PERP": 0.0003,
    "OP-PERP": 0.0001,
    "SUI-PERP": 0.0008,
    "TON-PERP": 0.0002,
    "PEPE-PERP": 0.0010,
    "WIF-PERP": 0.0015,
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
        lo = o * 0.997
        c = o + price * 0.001
        v = price * 500
        candles.append(
            [ts, round(o, 6), round(h, 6), round(lo, 6), round(c, 6), round(v, 2)]
        )
    return candles


def _interval_to_seconds(interval: str) -> int:
    """Match HyperliquidClient._interval_to_ms but return seconds for candle gen."""
    if not interval or len(interval) < 2:
        raise ValueError(
            f"Invalid interval {interval!r}; expected format '<N>m', '<N>h', or '<N>d'"
        )
    units = {"m": 60, "h": 3600, "d": 86400}
    suffix = interval[-1]
    if suffix not in units:
        raise ValueError(
            f"Unknown interval suffix {suffix!r} in {interval!r}; valid: m, h, d"
        )
    try:
        n = int(interval[:-1])
    except ValueError:
        raise ValueError(
            f"Invalid interval number in {interval!r}; expected '<N>m', '<N>h', or '<N>d'"
        )
    if n <= 0:
        raise ValueError(f"Interval number must be positive, got {n} in {interval!r}")
    return n * units[suffix]


class StubMarket:
    """Async stub implementing the MarketProvider interface with deterministic data."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    async def get_ohlcv(
        self, asset: str, interval: str, lookback_candles: int
    ) -> list[list]:
        interval_sec = _interval_to_seconds(interval)
        price = _PRICES.get(asset, 100.0)
        return _make_candles(price, lookback_candles, interval_sec)

    async def get_funding_rate(self, asset: str) -> dict:
        rate = _FUNDING.get(asset, 0.0001)
        price = _PRICES.get(asset, 100.0)
        return {
            "fundingRate": rate,
            "openInterest": 420_000_000.0,
            "prevDayPx": price * 0.998,
        }

    async def get_open_interest(self, asset: str) -> dict:
        return {"openInterest": 420_000_000.0}

    async def get_liquidations(self, asset: str, hours: int = 4) -> list[dict]:
        price = _PRICES.get(asset, 100.0)
        now_ms = int(time.time() * 1000)
        cutoff_ms = now_ms - hours * 3600 * 1000
        entry_ts = now_ms
        if entry_ts >= cutoff_ms:
            return [
                {
                    "side": "long",
                    "price": price,
                    "size": 8_500_000.0,
                    "ts": entry_ts,
                    "_proxy": "recentTrades",
                },
            ]
        return []

    async def get_orderbook(self, asset: str, depth: int = 5) -> dict:
        price = _PRICES.get(asset, 100.0)
        spread = price * 0.0001
        bids = [
            [round(price - spread * (i + 1), 6), round(10.0 / (i + 1), 4)]
            for i in range(depth)
        ]
        asks = [
            [round(price + spread * (i + 1), 6), round(10.0 / (i + 1), 4)]
            for i in range(depth)
        ]
        return {"bids": bids, "asks": asks}

    async def get_mid_price(self, asset: str) -> float:
        return _PRICES.get(asset, 100.0)

    async def get_all_mids(self) -> dict[str, float]:
        # Return bare coin names (no -PERP suffix) matching HyperliquidClient
        return {k.replace("-PERP", ""): v for k, v in _PRICES.items()}


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
