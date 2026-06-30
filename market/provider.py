"""Unified market data interface. config['data_source'] selects the backend."""
from __future__ import annotations
import asyncio
import logging

from market.regime import classify_regime

logger = logging.getLogger(__name__)


class MarketProvider:
    def __init__(self, config: dict):
        source = config.get("data_source", "stub")
        if source == "hyperliquid":
            from market.hyperliquid import HyperliquidClient
            self._backend = HyperliquidClient()
        elif source == "stub":
            from market.stub import StubMarket
            self._backend = StubMarket()
        else:
            raise ValueError(
                f"Unknown data_source {source!r}; valid options: 'stub', 'hyperliquid'"
            )
        self._sem = asyncio.Semaphore(5)

    async def __aenter__(self):
        await self._backend.__aenter__()
        return self

    async def __aexit__(self, *args):
        await self._backend.__aexit__(*args)

    async def get_ohlcv(
        self, asset: str, interval: str, lookback_candles: int
    ) -> list[list]:
        return await self._backend.get_ohlcv(asset, interval, lookback_candles)

    async def get_funding_rate(self, asset: str) -> dict:
        return await self._backend.get_funding_rate(asset)

    async def get_open_interest(self, asset: str) -> dict:
        return await self._backend.get_open_interest(asset)

    async def get_liquidations(self, asset: str, hours: int = 4) -> list[dict]:
        return await self._backend.get_liquidations(asset, hours)

    async def get_orderbook(self, asset: str, depth: int = 5) -> dict:
        return await self._backend.get_orderbook(asset, depth)

    async def get_mid_price(self, asset: str) -> float:
        return await self._backend.get_mid_price(asset)

    async def get_all_mids(self) -> dict[str, float]:
        return await self._backend.get_all_mids()

    async def get_market_state(self, assets: list[str]) -> dict:
        """Aggregated market snapshot enriched with regime (parallel per asset)."""
        all_mids = await self._backend.get_all_mids()

        async def _fetch_one(asset: str) -> tuple[str, dict] | None:
            coin = asset.replace("-PERP", "")
            mid = all_mids.get(coin)
            if mid is None:
                logger.warning("get_market_state: skipping %s (no mid price)", asset)
                return None

            async def _safe(coro, default):
                try:
                    return await coro
                except Exception:
                    return default

            async with self._sem:
                funding, ohlcv_15m, ohlcv_1h, ohlcv_4h, oi, liq, book = await asyncio.gather(
                    _safe(self._backend.get_funding_rate(asset), {"fundingRate": 0, "prevDayPx": 0}),
                    _safe(self._backend.get_ohlcv(asset, "15m", 40), []),
                    _safe(self._backend.get_ohlcv(asset, "1h", 20), []),
                    _safe(self._backend.get_ohlcv(asset, "4h", 10), []),
                    _safe(self._backend.get_open_interest(asset), {"openInterest": 0}),
                    _safe(self._backend.get_liquidations(asset, hours=4), []),
                    _safe(self._backend.get_orderbook(asset, depth=1), None),
                    return_exceptions=False,
                )

            bid = book["bids"][0][0] if book and book.get("bids") else mid
            ask = book["asks"][0][0] if book and book.get("asks") else mid

            liq_vol_1h = sum(x["size"] for x in liq)
            long_vol = sum(x["size"] for x in liq if x.get("side") == "long")
            short_vol = sum(x["size"] for x in liq if x.get("side") == "short")
            liq_dir = "short" if short_vol > long_vol else "long"
            prev = funding.get("prevDayPx", 0)
            oi_24h_chg = (mid - prev) / prev * 100 if prev else 0.0

            return asset, {
                "ohlcv_15m": ohlcv_15m,
                "ohlcv_1h": ohlcv_1h,
                "ohlcv_4h": ohlcv_4h,
                "mid_price": mid,
                "bid": bid,
                "ask": ask,
                "funding_rate_current": funding.get("fundingRate", 0),
                "funding_rate_8h_history": [],
                "open_interest_usd": oi.get("openInterest", 0),
                "open_interest_24h_change_pct": oi_24h_chg,
                "liquidation_volume_1h_usd": liq_vol_1h,
                "liquidation_direction_dominant": liq_dir,
            }

        results = await asyncio.gather(*[_fetch_one(a) for a in assets])
        state = {}
        for r in results:
            if r is not None:
                k, v = r
                state[k] = v

        try:
            btc_1d = await self._backend.get_ohlcv("BTC-PERP", "1d", 30)
            state["_regime"] = classify_regime(btc_1d)
        except Exception:
            state["_regime"] = "range_low_vol"

        return state
