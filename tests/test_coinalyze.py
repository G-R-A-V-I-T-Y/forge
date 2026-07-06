"""Tests for market/coinalyze.py — CoinalyzeClient with mocked HTTP."""
import pytest
import httpx
import respx

from market.coinalyze import (
    CoinalyzeClient,
    project_to_coinalyze_symbol,
)

BASE = "https://api.coinalyze.net/v1/"


# ---------------------------------------------------------------------------
# Symbol mapping
# ---------------------------------------------------------------------------

def test_project_to_coinalyze_symbol_btc():
    assert project_to_coinalyze_symbol("BTC-PERP") == "BTCUSDT_PERP.A"


def test_project_to_coinalyze_symbol_eth():
    assert project_to_coinalyze_symbol("ETH-PERP") == "ETHUSDT_PERP.A"


def test_project_to_coinalyze_symbol_sol():
    assert project_to_coinalyze_symbol("SOL-PERP") == "SOLUSDT_PERP.A"


def test_project_to_coinalyze_symbol_preserves_case():
    """Asset names are already uppercase in the project universe, but the
    function should handle lowercase gracefully."""
    assert project_to_coinalyze_symbol("btc-perp") == "BTCUSDT_PERP.A"


def test_project_to_coinalyze_symbol_no_perp_suffix():
    """If the asset happens lacks a -PERP suffix (shouldn't happen with
    our universe but the function should handle it), it still works."""
    assert project_to_coinalyze_symbol("LINK") == "LINKUSDT_PERP.A"


# ---------------------------------------------------------------------------
# Liquidation fetch — successful response
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@respx.mock
async def test_fetch_liquidations_returns_parsed_data():
    """A successful 200 response yields per-symbol history with l/s fields."""
    now = 1700000000
    respx.get(f"{BASE}liquidation-history").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "symbol": "BTCUSDT_PERP.A",
                    "history": [
                        {"t": now - 120, "l": 50000.0, "s": 30000.0},
                        {"t": now - 60, "l": 20000.0, "s": 10000.0},
                    ],
                },
                {
                    "symbol": "ETHUSDT_PERP.A",
                    "history": [
                        {"t": now - 120, "l": 10000.0, "s": 8000.0},
                    ],
                },
            ],
        )
    )
    async with CoinalyzeClient(api_key="test-key") as client:
        result = await client.fetch_liquidations(
            ["BTCUSDT_PERP.A", "ETHUSDT_PERP.A"],
            lookback_minutes=15,
        )
    assert result is not None
    assert "BTCUSDT_PERP.A" in result
    assert len(result["BTCUSDT_PERP.A"]) == 2
    assert result["BTCUSDT_PERP.A"][0]["l"] == 50000.0
    assert result["BTCUSDT_PERP.A"][0]["s"] == 30000.0
    assert "ETHUSDT_PERP.A" in result
    assert result["ETHUSDT_PERP.A"][0]["l"] == 10000.0


# ---------------------------------------------------------------------------
# Liquidation fetch — no API key
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fetch_liquidations_no_api_key_returns_none():
    """Without an API key the client logs a warning and returns None."""
    async with CoinalyzeClient(api_key=None) as client:
        result = await client.fetch_liquidations(["BTCUSDT_PERP.A"])
    assert result is None


# ---------------------------------------------------------------------------
# Liquidation fetch — HTTP error
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@respx.mock
async def test_fetch_liquidations_http_error_returns_none():
    """A 500 response triggers the circuit breaker after 5 failures."""
    respx.get(f"{BASE}liquidation-history").mock(
        return_value=httpx.Response(500, json={"error": "server error"}),
    )
    async with CoinalyzeClient(api_key="test-key") as client:
        for _ in range(5):
            result = await client.fetch_liquidations(["BTCUSDT_PERP.A"])
        assert result is None
    # Circuit breaker should be open after 5 failures
    assert client.available is False


# ---------------------------------------------------------------------------
# fetch_liquidations_for_assets — successful
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@respx.mock
async def test_fetch_liquidations_for_assets_computes_totals():
    """fetch_liquidations_for_assets maps project assets → Coinalyze symbols,
    fetches data, and returns per-asset feature dicts."""
    now = 1700000000
    respx.get(f"{BASE}liquidation-history").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "symbol": "BTCUSDT_PERP.A",
                    "history": [
                        {"t": now - 60, "l": 100000.0, "s": 50000.0},
                    ],
                },
                {
                    "symbol": "ETHUSDT_PERP.A",
                    "history": [
                        {"t": now - 60, "l": 20000.0, "s": 10000.0},
                    ],
                },
            ],
        )
    )
    async with CoinalyzeClient(api_key="test-key") as client:
        result = await client.fetch_liquidations_for_assets(
            ["BTC-PERP", "ETH-PERP"],
        )
    assert result is not None
    assert result["BTC-PERP"] is not None
    assert result["BTC-PERP"]["liq_total_usd"] == 150000.0
    assert result["BTC-PERP"]["liq_long_usd"] == 100000.0
    assert result["BTC-PERP"]["liq_short_usd"] == 50000.0
    assert result["ETH-PERP"]["liq_total_usd"] == 30000.0
    assert result["ETH-PERP"]["liq_long_usd"] == 20000.0
    assert result["ETH-PERP"]["liq_short_usd"] == 10000.0


# ---------------------------------------------------------------------------
# fetch_liquidations_for_assets — empty history
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@respx.mock
async def test_fetch_liquidations_for_assets_empty_history():
    """If Coinalyze returns empty history for a symbol the asset gets None."""
    respx.get(f"{BASE}liquidation-history").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"symbol": "BTCUSDT_PERP.A", "history": []},
            ],
        )
    )
    async with CoinalyzeClient(api_key="test-key") as client:
        result = await client.fetch_liquidations_for_assets(["BTC-PERP"])
    assert result["BTC-PERP"] is None


# ---------------------------------------------------------------------------
# fetch_liquidations_for_assets — fetch failure
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@respx.mock
async def test_fetch_liquidations_for_assets_fetch_failure():
    """If the HTTP fetch fails entirely, all assets get None."""
    respx.get(f"{BASE}liquidation-history").mock(
        return_value=httpx.Response(502, json={"error": "bad gateway"}),
    )
    async with CoinalyzeClient(api_key="test-key") as client:
        result = await client.fetch_liquidations_for_assets(["BTC-PERP", "ETH-PERP"])
    assert result["BTC-PERP"] is None
    assert result["ETH-PERP"] is None


# ---------------------------------------------------------------------------
# Heartbeat integration — liquidation fields in asset dict
# ---------------------------------------------------------------------------

def test_compute_asset_fields_includes_liquidation_fields():
    """_compute_asset_fields with liq_data produces the 3 liquidation fields."""
    from market.heartbeat import _compute_asset_fields

    # Need non-empty candles so the function doesn't early-return all-None
    raw = {
        "candles": [[1000, 65000, 66000, 64000, 65500, 100.0]],
        "funding_history": [],
        "oi": {},
        "funding": {},
        "book": {"bids": [], "asks": []},
        "trades": [],
    }
    liq_data = {
        "liq_total_usd": 123456.0,
        "liq_long_usd": 80000.0,
        "liq_short_usd": 43456.0,
    }
    result = _compute_asset_fields(raw, [], liq_data)
    assert result["liq_total_usd"] == 123456.0
    assert result["liq_long_usd"] == 80000.0
    assert result["liq_short_usd"] == 43456.0


def test_compute_asset_fields_no_liq_data():
    """Without liq_data the liquidation fields are None."""
    from market.heartbeat import _compute_asset_fields

    raw = {"candles": [], "funding_history": [], "oi": {}, "funding": {}, "book": {}, "trades": []}
    result = _compute_asset_fields(raw, [], None)
    assert result["liq_total_usd"] is None
    assert result["liq_long_usd"] is None
    assert result["liq_short_usd"] is None


def test_compute_asset_fields_empty_liq_data():
    """Empty dict liq_data also produces None (missing keys)."""
    from market.heartbeat import _compute_asset_fields

    raw = {"candles": [], "funding_history": [], "oi": {}, "funding": {}, "book": {}, "trades": []}
    result = _compute_asset_fields(raw, [], {})
    assert result["liq_total_usd"] is None
