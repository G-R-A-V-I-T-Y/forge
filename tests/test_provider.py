"""Tests for market/provider.py — MarketProvider abstraction layer."""
import pytest
from market.provider import MarketProvider


@pytest.mark.asyncio
async def test_stub_provider_returns_ohlcv():
    provider = MarketProvider({})
    async with provider:
        candles = await provider.get_ohlcv("BTC-PERP", "15m", 10)
    assert isinstance(candles, list)
    assert len(candles) == 10
    # Each candle: [timestamp, open, high, low, close, volume]
    assert len(candles[0]) == 6
    assert candles[0][2] >= candles[0][3]  # high >= low


@pytest.mark.asyncio
async def test_stub_provider_returns_funding():
    provider = MarketProvider({})
    async with provider:
        result = await provider.get_funding_rate("ETH-PERP")
    assert "fundingRate" in result
    assert isinstance(result["fundingRate"], float)


@pytest.mark.asyncio
async def test_stub_provider_get_mid_price():
    provider = MarketProvider({})
    async with provider:
        price = await provider.get_mid_price("BTC-PERP")
    assert isinstance(price, float)
    assert price > 0.0


@pytest.mark.asyncio
async def test_provider_selects_stub_by_default():
    from market.stub import StubMarket
    provider = MarketProvider({})
    assert isinstance(provider._backend, StubMarket)


@pytest.mark.asyncio
async def test_provider_selects_hyperliquid_when_configured():
    from market.hyperliquid import HyperliquidClient
    provider = MarketProvider({"data_source": "hyperliquid"})
    assert isinstance(provider._backend, HyperliquidClient)


@pytest.mark.asyncio
async def test_stub_provider_get_all_mids():
    provider = MarketProvider({})
    async with provider:
        mids = await provider.get_all_mids()
    assert isinstance(mids, dict)
    assert "BTC-PERP" in mids
    assert mids["BTC-PERP"] > 0.0


@pytest.mark.asyncio
async def test_stub_provider_get_orderbook():
    provider = MarketProvider({})
    async with provider:
        book = await provider.get_orderbook("BTC-PERP", depth=3)
    assert "bids" in book
    assert "asks" in book
    assert len(book["bids"]) >= 1
    assert len(book["asks"]) >= 1
