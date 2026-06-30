"""
Hyperliquid REST API client.

Base URL: https://api.hyperliquid.xyz/info
All requests are HTTP POST with Content-Type: application/json.

Endpoints (all POST to BASE_URL):
  allMids
    Body: {"type": "allMids"}
    Returns: {"ASSET": "midPrice", ...}  — dict of asset name to mid price string

  candleSnapshot
    Body: {"type": "candleSnapshot", "req": {"coin": asset, "interval": interval,
           "startTime": unix_ms, "endTime": unix_ms}}
    Returns: list of {T, o, h, l, c, v} objects

  metaAndAssetCtxs
    Body: {"type": "metaAndAssetCtxs"}
    Returns: [meta, [assetCtx, ...]]
    Each assetCtx has: fundingRate, openInterest, prevDayPx, markPx, ...

  l2Book
    Body: {"type": "l2Book", "coin": asset}
    Returns: {"levels": [[{px, sz, n}, ...], [{px, sz, n}, ...]]}
    levels[0] = bids (descending price), levels[1] = asks (ascending price)

  recentTrades
    Body: {"type": "recentTrades", "coin": asset}
    Returns: list of {coin, side, px, sz, time, hash, tid}
    Note: No public liquidation endpoint exists; recentTrades is used as a proxy.

  fundingHistory
    Body: {"type": "fundingHistory", "coin": asset, "startTime": unix_ms}
    Returns: list of {coin, fundingRate, premium, time}
"""

import asyncio
import logging
import time

import httpx

logger = logging.getLogger(__name__)


class HyperliquidClient:
    BASE_URL = "https://api.hyperliquid.xyz/info"
    _MAX_RETRIES = 3
    _FAILURE_THRESHOLD = 5
    _CIRCUIT_COOLDOWN = 60.0  # seconds before auto-recovery attempt

    def __init__(self):
        self._client: httpx.AsyncClient | None = None
        self._consecutive_failures = 0
        self._circuit_open_until: float = 0.0
        self.available = True
        self._sem = asyncio.Semaphore(10)

    async def __aenter__(self):
        self._client = httpx.AsyncClient(timeout=15.0)
        return self

    async def __aexit__(self, *_):
        if self._client:
            await self._client.aclose()
            self._client = None

    async def _post(self, body: dict) -> dict:
        if not self.available:
            if time.monotonic() < self._circuit_open_until:
                raise RuntimeError(
                    "HyperliquidClient circuit breaker open — skipping request"
                )
            # Auto-recovery: reset and try again
            logger.info("HyperliquidClient: circuit breaker reset, retrying")
            self.available = True
            self._consecutive_failures = 0

        last_exc: Exception | None = None
        wait: float = 0.0
        for attempt in range(self._MAX_RETRIES):
            if wait:
                await asyncio.sleep(wait)
                wait = 0.0
            async with self._sem:
                try:
                    resp = await self._client.post(self.BASE_URL, json=body)
                    if resp.status_code == 429:
                        try:
                            wait = float(resp.headers.get("Retry-After", "1"))
                        except ValueError:
                            wait = 1.0
                        logger.warning(
                            "Rate limited by Hyperliquid, waiting %.1fs", wait
                        )
                        last_exc = httpx.HTTPStatusError(
                            "429 rate limit", request=resp.request, response=resp
                        )
                        continue
                    resp.raise_for_status()
                    self._consecutive_failures = 0
                    return resp.json()
                except httpx.HTTPStatusError:
                    self._record_failure()
                    raise
                except Exception:
                    self._record_failure()
                    raise
        # Exhausted retries
        self._record_failure()
        raise last_exc or RuntimeError("HyperliquidClient: max retries exceeded")

    def _record_failure(self):
        self._consecutive_failures += 1
        if self._consecutive_failures >= self._FAILURE_THRESHOLD:
            self.available = False
            self._circuit_open_until = time.monotonic() + self._CIRCUIT_COOLDOWN
            logger.error(
                "HyperliquidClient: %d consecutive failures — circuit breaker opened",
                self._consecutive_failures,
            )

    async def get_all_mids(self) -> dict[str, float]:
        data = await self._post({"type": "allMids"})
        return {k: float(v) for k, v in data.items()}

    async def get_ohlcv(
        self, asset: str, interval: str, lookback_candles: int
    ) -> list[list]:
        now_ms = int(time.time() * 1000)
        interval_ms = _interval_to_ms(interval)
        start_ms = now_ms - lookback_candles * interval_ms
        data = await self._post(
            {
                "type": "candleSnapshot",
                "req": {
                    "coin": asset,
                    "interval": interval,
                    "startTime": start_ms,
                    "endTime": now_ms,
                },
            }
        )
        return [
            [
                c["T"],
                float(c["o"]),
                float(c["h"]),
                float(c["l"]),
                float(c["c"]),
                float(c["v"]),
            ]
            for c in data
        ]

    async def get_funding_rate(self, asset: str) -> dict:
        meta, asset_ctxs = await self._post({"type": "metaAndAssetCtxs"})
        idx = _asset_index(meta, asset)
        ctx = asset_ctxs[idx]
        return {
            "fundingRate": float(ctx["fundingRate"]),
            "openInterest": float(ctx["openInterest"]),
            "prevDayPx": float(ctx["prevDayPx"]),
        }

    async def get_open_interest(self, asset: str) -> dict:
        meta, asset_ctxs = await self._post({"type": "metaAndAssetCtxs"})
        idx = _asset_index(meta, asset)
        ctx = asset_ctxs[idx]
        oi = float(ctx["openInterest"])
        return {"openInterest": oi}

    async def get_liquidations(self, asset: str, hours: int = 4) -> list[dict]:
        # NOTE: Hyperliquid has no public liquidation history endpoint.
        # recentTrades is used as a proxy; filtered to the requested time window.
        now_ms = int(time.time() * 1000)
        cutoff_ms = now_ms - hours * 3600 * 1000
        data = await self._post({"type": "recentTrades", "coin": asset})
        return [
            {
                "side": t["side"],
                "price": float(t["px"]),
                "size": float(t["sz"]),
                "ts": t["time"],
            }
            for t in data
            if t["time"] >= cutoff_ms
        ]

    async def get_orderbook(self, asset: str, depth: int = 5) -> dict:
        data = await self._post({"type": "l2Book", "coin": asset})
        levels = data["levels"]
        bids = [[float(lv["px"]), float(lv["sz"])] for lv in levels[0][:depth]]
        asks = [[float(lv["px"]), float(lv["sz"])] for lv in levels[1][:depth]]
        return {"bids": bids, "asks": asks}

    async def get_mid_price(self, asset: str) -> float:
        book = await self.get_orderbook(asset, depth=1)
        if not book["bids"] or not book["asks"]:
            raise ValueError(
                f"Empty order book for {asset!r}: cannot compute mid price"
            )
        best_bid = book["bids"][0][0]
        best_ask = book["asks"][0][0]
        return (best_bid + best_ask) / 2.0


def _interval_to_ms(interval: str) -> int:
    units = {"m": 60_000, "h": 3_600_000, "d": 86_400_000}
    suffix = interval[-1]
    if suffix not in units:
        raise ValueError(
            f"Unknown interval suffix {suffix!r} in {interval!r}; valid: m, h, d"
        )
    n = int(interval[:-1])
    return n * units[suffix]


def _asset_index(meta: dict, asset: str) -> int:
    # meta["universe"] is a list of {"name": ..., ...}
    # Strip "-PERP" suffix to get the coin name Hyperliquid uses
    coin = asset.replace("-PERP", "")
    for i, info in enumerate(meta.get("universe", [])):
        if info.get("name") == coin:
            return i
    raise KeyError(f"Asset {asset!r} not found in Hyperliquid universe")
