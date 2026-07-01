"""Tests for market/heartbeat.py — the heartbeat market-data generator."""
import json
import math

import httpx
import pytest
import respx

from market import heartbeat
from market.heartbeat import (
    PER_ASSET_FIELDS,
    SECTORS,
    _atr,
    _ema,
    _log_returns,
    _realized_vol,
    _rsi,
    _vwap,
    _zscore,
    compute_pca,
    correlation_matrix,
    generate_heartbeat,
    heartbeat_max_age_seconds,
    read_heartbeat,
    read_heartbeat_or_none,
    sector_strength,
    write_heartbeat,
)
from market.provider import MarketProvider


# ---------------------------------------------------------------------------
# Indicator unit tests
# ---------------------------------------------------------------------------

def test_ema_matches_hand_computed():
    # EMA(3) of [1, 2, 3, 4, 5]: k = 0.5
    # ema0=1, ema1=2*0.5+1*0.5=1.5, ema2=3*0.5+1.5*0.5=2.25,
    # ema3=4*0.5+2.25*0.5=3.125, ema4=5*0.5+3.125*0.5=4.0625
    values = [1, 2, 3, 4, 5]
    assert _ema(values, 3) == pytest.approx(4.0625)


def test_ema_none_when_insufficient_data():
    assert _ema([1, 2], 5) is None


def test_rsi_all_gains_is_100():
    closes = [float(i) for i in range(1, 20)]  # strictly increasing
    assert _rsi(closes, 14) == pytest.approx(100.0)


def test_rsi_all_losses_is_0():
    closes = [float(i) for i in range(20, 1, -1)]  # strictly decreasing
    assert _rsi(closes, 14) == pytest.approx(0.0)


def test_rsi_flat_series_is_none_or_100():
    # No losses at all in a flat series -> avg_loss == 0 -> RSI defined as 100
    closes = [100.0] * 20
    assert _rsi(closes, 14) == pytest.approx(100.0)


def test_atr_hand_computed():
    # Constant true range of 2.0 for every candle after the first
    highs = [10 + i * 0 + 1 for i in range(16)]
    lows = [10 - 1 for _ in range(16)]
    closes = [10.0 for _ in range(16)]
    atr = _atr(highs, lows, closes, period=14)
    assert atr == pytest.approx(2.0)


def test_atr_none_when_insufficient_data():
    assert _atr([1, 2], [1, 2], [1, 2], period=14) is None


def test_realized_vol_zero_for_constant_series():
    closes = [100.0] * 50
    vol = _realized_vol(closes, periods_per_year=252)
    assert vol == pytest.approx(0.0)


def test_realized_vol_none_for_too_short_series():
    assert _realized_vol([100.0], periods_per_year=252) is None


def test_zscore_hand_computed():
    baseline = [1.0, 2.0, 3.0, 4.0, 5.0]
    # mean=3, stdev=sqrt(2.5)=1.5811
    z = _zscore(6.0, baseline)
    assert z == pytest.approx((6.0 - 3.0) / 1.5811388300841898, rel=1e-4)


def test_zscore_none_with_insufficient_baseline():
    assert _zscore(5.0, [1.0]) is None
    assert _zscore(None, [1.0, 2.0, 3.0]) is None


def test_zscore_zero_stdev_returns_zero():
    assert _zscore(5.0, [5.0, 5.0, 5.0]) == pytest.approx(0.0)


def test_vwap_hand_computed():
    # Two candles: [ts, o, h, l, c, v]
    # candle1: typical = (11+9+10)/3 = 10, vol=100 -> weighted 1000
    # candle2: typical = (21+19+20)/3 = 20, vol=200 -> weighted 4000
    # vwap = (1000+4000)/(100+200) = 5000/300 = 16.667
    candles = [
        [0, 10, 11, 9, 10, 100],
        [1, 20, 21, 19, 20, 200],
    ]
    assert _vwap(candles) == pytest.approx(5000 / 300)


def test_vwap_none_for_empty_candles():
    assert _vwap([]) is None


def test_log_returns_basic():
    closes = [100.0, 110.0, 121.0]
    returns = _log_returns(closes)
    assert returns[0] == pytest.approx(math.log(1.1))
    assert returns[1] == pytest.approx(math.log(1.1))


# ---------------------------------------------------------------------------
# Correlation / PCA
# ---------------------------------------------------------------------------

def test_correlation_matrix_perfectly_correlated_series():
    series = [0.01, -0.02, 0.03, 0.015, -0.005, 0.02]
    asset_returns = {"A-PERP": series, "B-PERP": series}
    corr = correlation_matrix(asset_returns)
    assert corr["A-PERP"]["B-PERP"] == pytest.approx(1.0, abs=1e-6)
    assert corr["A-PERP"]["A-PERP"] == pytest.approx(1.0, abs=1e-6)


def test_correlation_matrix_anti_correlated_series():
    series_a = [0.01, -0.02, 0.03, 0.015, -0.005, 0.02]
    series_b = [-x for x in series_a]
    corr = correlation_matrix({"A-PERP": series_a, "B-PERP": series_b})
    assert corr["A-PERP"]["B-PERP"] == pytest.approx(-1.0, abs=1e-6)


def test_correlation_matrix_insufficient_data_returns_none():
    corr = correlation_matrix({"A-PERP": [], "B-PERP": []})
    assert corr["A-PERP"]["B-PERP"] is None


def test_pca_returns_expected_structure():
    series = [0.01, -0.02, 0.03, 0.015, -0.005, 0.02, 0.01, -0.01]
    asset_returns = {"A-PERP": series, "B-PERP": series, "C-PERP": [x * 2 for x in series]}
    pca = compute_pca(asset_returns)
    assert "explained_variance_ratio" in pca
    assert "first_component_loadings" in pca
    # Perfectly collinear assets -> first component should explain ~all variance
    assert pca["explained_variance_ratio"][0] == pytest.approx(1.0, abs=1e-6)
    assert set(pca["first_component_loadings"].keys()) == {"A-PERP", "B-PERP", "C-PERP"}


def test_pca_insufficient_data_returns_empty():
    pca = compute_pca({"A-PERP": []})
    assert pca == {"explained_variance_ratio": [], "first_component_loadings": {}}


# ---------------------------------------------------------------------------
# Sector strength
# ---------------------------------------------------------------------------

def test_sector_strength_has_exactly_seven_sectors_no_overlap():
    assert set(SECTORS.keys()) == {
        "L1", "L2", "Modular_DA", "DeFi_Oracle", "AI", "Exchange", "Legacy_Payments",
    }
    all_assets = [a for members in SECTORS.values() for a in members]
    assert len(all_assets) == len(set(all_assets)), "no asset should be double-counted"
    assert len(all_assets) == 20, "all 20 universe assets should be covered exactly once"


def test_sector_strength_computes_mean_return():
    assets_fields = {a: {"return_24h": 0.0} for members in SECTORS.values() for a in members}
    assets_fields["BTC-PERP"]["return_24h"] = 0.10
    assets_fields["ETH-PERP"]["return_24h"] = 0.20
    result = sector_strength(assets_fields)
    assert set(result.keys()) == set(SECTORS.keys())
    # L1 = BTC, ETH, SOL, SUI, AVAX, ADA, BNB -> (0.10+0.20+0+0+0+0+0)/7
    assert result["L1"] == pytest.approx((0.10 + 0.20) / 7)


def test_sector_strength_none_when_no_data():
    assets_fields = {}
    result = sector_strength(assets_fields)
    assert all(v is None for v in result.values())


# ---------------------------------------------------------------------------
# Atomic write / read
# ---------------------------------------------------------------------------

def test_write_and_read_heartbeat_roundtrip(tmp_path):
    path = str(tmp_path / "heartbeat.json")
    packet = {"timestamp": "2026-07-01T00:00:00Z", "assets": {}, "cross_asset": {}, "regime": {}}
    write_heartbeat(path, packet)
    with open(path) as f:
        on_disk = json.load(f)
    assert on_disk == packet
    assert read_heartbeat(path) == packet


def test_read_heartbeat_missing_path_returns_none(tmp_path):
    path = str(tmp_path / "does_not_exist.json")
    assert read_heartbeat(path) is None


def test_read_heartbeat_malformed_json_returns_none(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not valid json")
    assert read_heartbeat(str(path)) is None


def test_write_heartbeat_creates_parent_dirs(tmp_path):
    path = str(tmp_path / "nested" / "dir" / "heartbeat.json")
    write_heartbeat(path, {"a": 1})
    assert read_heartbeat(path) == {"a": 1}


# ---------------------------------------------------------------------------
# End-to-end generate_heartbeat() against the stub backend
# ---------------------------------------------------------------------------

@pytest.fixture
def stub_config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    universe = [
        "BTC-PERP", "ETH-PERP", "SOL-PERP", "SUI-PERP", "AVAX-PERP", "LINK-PERP",
        "AAVE-PERP", "BNB-PERP", "ARB-PERP", "OP-PERP", "TAO-PERP", "FET-PERP",
        "RENDER-PERP", "XRP-PERP", "XLM-PERP", "TIA-PERP", "HYPE-PERP", "LTC-PERP",
        "BCH-PERP", "ADA-PERP",
    ]
    return {
        "data_source": "stub",
        "universe": universe,
        "desk": {"heartbeat_path": "data/heartbeat.json"},
    }


@pytest.mark.asyncio
@respx.mock
async def test_generate_heartbeat_end_to_end_structure(stub_config):
    # Let the real Fear & Greed call through respx as an unmocked passthrough
    # would raise (respx.mock intercepts all httpx traffic); mock it to a
    # deterministic value so the test is fully network-free.
    respx.get("https://api.alternative.me/fng/?limit=1").mock(
        return_value=httpx.Response(200, json={"data": [{"value": "42"}]})
    )
    provider = MarketProvider(stub_config)
    async with provider:
        packet = await generate_heartbeat(provider, stub_config)

    assert set(packet.keys()) == {"timestamp", "assets", "cross_asset", "regime"}
    assert len(packet["assets"]) == len(stub_config["universe"])
    for asset in stub_config["universe"]:
        assert asset in packet["assets"]
        fields = packet["assets"][asset]
        for field in PER_ASSET_FIELDS:
            assert field in fields, f"{field} missing for {asset}"

    cross_asset = packet["cross_asset"]
    for key in (
        "market_breadth", "average_return", "median_return", "leader", "laggard",
        "correlation_matrix", "pca", "sector_strength", "momentum_rankings",
        "relative_strength",
    ):
        assert key in cross_asset

    regime = packet["regime"]
    for key in (
        "crypto_fear_index", "btc_dominance", "average_volatility", "average_funding",
        "average_oi_growth", "market_breadth", "risk_on_score", "trend_score",
        "regime_tag",
    ):
        assert key in regime
    assert regime["crypto_fear_index"] == 42
    assert isinstance(regime["regime_tag"], str) and regime["regime_tag"]

    # Atomic file was written and is readable back
    on_disk = read_heartbeat(stub_config["desk"]["heartbeat_path"])
    assert on_disk == packet


@pytest.mark.asyncio
@respx.mock
async def test_generate_heartbeat_fear_greed_failure_is_graceful(stub_config):
    respx.get("https://api.alternative.me/fng/?limit=1").mock(
        side_effect=httpx.ConnectError("network down")
    )
    provider = MarketProvider(stub_config)
    async with provider:
        packet = await generate_heartbeat(provider, stub_config)

    assert packet["regime"]["crypto_fear_index"] is None
    # Cycle still completed fully despite the third-party failure
    assert len(packet["assets"]) == len(stub_config["universe"])


# ---------------------------------------------------------------------------
# Staleness-aware reader
# ---------------------------------------------------------------------------

def test_heartbeat_max_age_seconds_uses_config_interval():
    assert heartbeat_max_age_seconds({"desk": {"heartbeat_interval_seconds": 300}}) == 600
    assert heartbeat_max_age_seconds({}) == 600  # default 300s interval


def test_read_heartbeat_or_none_missing_file_returns_none(tmp_path):
    path = str(tmp_path / "does_not_exist.json")
    assert read_heartbeat_or_none(path, max_age_seconds=600) is None


def test_read_heartbeat_or_none_malformed_json_returns_none(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not valid json")
    assert read_heartbeat_or_none(str(path), max_age_seconds=600) is None


def test_read_heartbeat_or_none_missing_timestamp_returns_none(tmp_path):
    path = str(tmp_path / "heartbeat.json")
    write_heartbeat(path, {"assets": {}, "cross_asset": {}, "regime": {}})
    assert read_heartbeat_or_none(path, max_age_seconds=600) is None


def test_read_heartbeat_or_none_fresh_packet_returned(tmp_path):
    from datetime import datetime, timezone
    path = str(tmp_path / "heartbeat.json")
    packet = {
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "assets": {"BTC-PERP": {"price": 65000.0}},
        "cross_asset": {},
        "regime": {},
    }
    write_heartbeat(path, packet)
    result = read_heartbeat_or_none(path, max_age_seconds=600)
    assert result == packet


def test_read_heartbeat_or_none_stale_packet_returns_none(tmp_path):
    from datetime import datetime, timedelta, timezone
    path = str(tmp_path / "heartbeat.json")
    old_ts = (datetime.now(timezone.utc) - timedelta(seconds=700)).strftime("%Y-%m-%dT%H:%M:%SZ")
    write_heartbeat(path, {"timestamp": old_ts, "assets": {}, "cross_asset": {}, "regime": {}})
    assert read_heartbeat_or_none(path, max_age_seconds=600) is None
