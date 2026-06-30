"""Unified market data interface. config['data_source'] selects the backend."""
from __future__ import annotations


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
