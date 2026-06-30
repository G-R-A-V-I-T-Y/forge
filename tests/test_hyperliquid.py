"""Tests for market/hyperliquid.py — HyperliquidClient with mocked HTTP."""
import time
import pytest
import httpx
import respx

from market.hyperliquid import HyperliquidClient

BASE = "https://api.hyperliquid.xyz/info"


@pytest.mark.asyncio
@respx.mock
async def test_get_all_mids_returns_dict():
    respx.post(BASE).mock(
        return_value=httpx.Response(200, json={"BTC": "65000.5", "ETH": "3500.0"})
    )
    async with HyperliquidClient() as client:
        result = await client.get_all_mids()
    assert isinstance(result, dict)
    assert result["BTC"] == pytest.approx(65000.5)
    assert result["ETH"] == pytest.approx(3500.0)


@pytest.mark.asyncio
@respx.mock
async def test_circuit_breaker_opens_after_5_failures():
    respx.post(BASE).mock(return_value=httpx.Response(500, json={"error": "server error"}))
    async with HyperliquidClient() as client:
        for _ in range(5):
            try:
                await client.get_all_mids()
            except Exception:
                pass
    assert client.available is False


@pytest.mark.asyncio
@respx.mock
async def test_rate_limit_retries():
    """First call returns 429, second returns 200 — should succeed after one retry."""
    call_count = 0

    def side_effect(request):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, json={})
        return httpx.Response(200, json={"BTC": "65000.0"})

    respx.post(BASE).mock(side_effect=side_effect)
    async with HyperliquidClient() as client:
        result = await client.get_all_mids()
    assert result["BTC"] == pytest.approx(65000.0)
    assert call_count == 2


@pytest.mark.asyncio
@respx.mock
async def test_get_orderbook_returns_bids_asks():
    respx.post(BASE).mock(
        return_value=httpx.Response(200, json={
            "levels": [
                [{"px": "64990.0", "sz": "1.5", "n": 1}, {"px": "64980.0", "sz": "2.0", "n": 1}],
                [{"px": "65010.0", "sz": "0.8", "n": 1}, {"px": "65020.0", "sz": "1.2", "n": 1}],
            ]
        })
    )
    async with HyperliquidClient() as client:
        book = await client.get_orderbook("BTC", depth=2)
    assert "bids" in book
    assert "asks" in book
    assert len(book["bids"]) == 2
    assert book["bids"][0][0] == pytest.approx(64990.0)


@pytest.mark.asyncio
@respx.mock
async def test_get_mid_price_returns_float():
    respx.post(BASE).mock(
        return_value=httpx.Response(200, json={
            "levels": [
                [{"px": "65000.0", "sz": "1.0", "n": 1}],
                [{"px": "65010.0", "sz": "1.0", "n": 1}],
            ]
        })
    )
    async with HyperliquidClient() as client:
        mid = await client.get_mid_price("BTC")
    assert mid == pytest.approx(65005.0)
