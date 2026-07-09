"""Heartbeat market-data generator.

One shared market-data snapshot for the full universe, computed once per
heartbeat cycle (default every 5 minutes — `desk.heartbeat_interval_seconds`
in config.yaml) and written atomically to `desk.heartbeat_path` (default
`data/heartbeat.json`). This replaces every agent independently hitting the
Hyperliquid API on its own wake cycle.

This module owns: fetching raw data for all universe assets, computing every
derived per-asset / cross-asset / regime field, the atomic file write/read,
and the git-native ledger export (`export_heartbeat_to_ledger()`), which
decomposes each packet into lean per-asset-per-type records appended to
`ledger/{kind}/{YYYY-MM}.jsonl` and is failure-isolated from the primary
write. See docs/superpowers/specs/2026-07-01-heartbeat-market-data-design.md
for the full field list and the documented approximations (OI-history
sampling, BTC-dominance-within-tracked-universe, Fear & Greed third-party
fetch).

Task B wires `agents/decision_loop.py`, `execution/paper_bridge.py`, and the
`/api/prices` web ticker to read this file (via `read_heartbeat_or_none()`
below) instead of calling the provider directly, applies
`wake_interval_seconds` to the agent scheduler, and schedules
`generate_heartbeat()` itself in `forge.py`. See
docs/superpowers/specs/2026-07-01-heartbeat-wiring-design.md.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import statistics
import time
from datetime import datetime, timezone

import httpx
import numpy as np
import pandas as pd

from market.event_calendar import read_events_for_heartbeat
from market.features import FEATURE_REGISTRY
from market.regime import classify_regime
from store.ledger import append_ledger_record

logger = logging.getLogger(__name__)

# Reference notional (USD) used to estimate market-order slippage.
REFERENCE_NOTIONAL_USD = 10_000.0

# 300 x 5m candles = 25h lookback. One fetch covers EMA200, ATR(14), RSI(14),
# realized vol, and all Z-score baselines.
LOOKBACK_CANDLES = 300
LOOKBACK_HOURS = 25

# Funding z-score baseline: every thesis (silver_basin's "z-score vs 14-day
# history" is the clearest example) assumes 14 days, independent of the 25h
# candle/EMA200 lookback above -- these were incorrectly sharing one window.
FUNDING_LOOKBACK_HOURS = 14 * 24

# Trade-tape window for buy/sell volume + aggressor fields. A reasonable
# window for trade-tape aggregates given the 5-minute heartbeat cadence.
TRADE_TAPE_HOURS = 1

# 5m candles per year, used to annualize realized volatility.
PERIODS_PER_YEAR_5M = 365 * 24 * 12  # 105,120

# Third-party (non-Hyperliquid) fear/greed index. Must never block or crash
# the heartbeat cycle if unreachable.
FEAR_GREED_URL = "https://api.alternative.me/fng/?limit=1"
FEAR_GREED_TIMEOUT_SECONDS = 5.0

# Rolling OI-history sample file. Hyperliquid has no OI history endpoint, so
# this is a deliberate workaround: sample current OI once per heartbeat
# cycle and keep the last 100 samples per asset here, used as a trailing
# baseline for oi_zscore and average_oi_growth. NOT a substitute for a real
# OI history API — see the design doc for the caveat this implies.
OI_HISTORY_PATH = "data/heartbeat_oi_history.json"
OI_HISTORY_MAX_SAMPLES = 100

DEFAULT_HEARTBEAT_PATH = "data/heartbeat.json"
DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 300

# Daily-candle lookback used only for the categorical regime tag
# (classify_regime() needs ~30 days of BTC daily candles; the 5m/25h
# universe fetch above is unrelated and not reused here).
REGIME_LOOKBACK_DAYS = 30

BTC_ASSET = "BTC-PERP"

# Exact sector grouping per spec — every universe asset appears exactly once.
SECTORS = {
    "L1": ["BTC-PERP", "ETH-PERP", "SOL-PERP", "SUI-PERP", "AVAX-PERP", "ADA-PERP", "BNB-PERP"],
    "L2": ["ARB-PERP", "OP-PERP"],
    "Modular_DA": ["TIA-PERP"],
    "DeFi_Oracle": ["AAVE-PERP", "LINK-PERP"],
    "AI": ["FET-PERP", "RENDER-PERP", "TAO-PERP"],
    "Exchange": ["HYPE-PERP"],
    "Legacy_Payments": ["XRP-PERP", "XLM-PERP", "LTC-PERP", "BCH-PERP"],
}

PER_ASSET_FIELDS = [
    "price", "return_5m", "return_30m", "return_4h", "return_24h", "volume",
    "open_interest", "funding", "spread", "atr", "realized_vol", "rsi",
    "ema20", "ema50", "ema200", "vwap_distance", "volume_zscore",
    "funding_zscore", "oi_zscore", "bid_depth", "ask_depth",
    "depth_imbalance", "top5_imbalance", "slippage_estimate", "buy_volume",
    "sell_volume", "aggressor_ratio",     "avg_trade_size", "largest_trade",
    "momentum_acceleration", "atr_percentile", "bb_width",
    "bb_width_percentile", "volume_percentile_14d", "funding_acceleration",
    "oi_drawdown_pct", "large_trade_volume_usd", "liquidation_cascade_flag",
    "candles_5m", "candles_30m", "candles_4h",
    # Liquidation fields (Coinalyze REST, 15-min lookback)
    "liq_total_usd", "liq_long_usd", "liq_short_usd",
    # Statistical forecast features (trailing return distribution)
    "statistical_forecast_return", "statistical_forecast_vol",
    "statistical_forecast_up_prob",
]

# Resampling factors (in units of 5m candles) for the longer-horizon
# aggregates carried alongside the raw 5m series in each asset's packet
# entry — see _resample_candles().
RESAMPLE_FACTOR_30M = 6
RESAMPLE_FACTOR_4H = 48


# ---------------------------------------------------------------------------
# Pure indicator helpers — each independently testable against hand-computed
# values.
# ---------------------------------------------------------------------------

def _log_returns(closes: list[float]) -> list[float]:
    return [
        math.log(closes[i] / closes[i - 1])
        for i in range(1, len(closes))
        if closes[i - 1] > 0 and closes[i] > 0
    ]


def _ema_series(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    k = 2.0 / (period + 1)
    ema = [values[0]]
    for v in values[1:]:
        ema.append(v * k + ema[-1] * (1 - k))
    return ema


def _ema(values: list[float], period: int) -> float | None:
    """Current EMA(period) value. None if fewer than `period` samples exist."""
    if len(values) < period:
        return None
    return _ema_series(values, period)[-1]


def _rsi(closes: list[float], period: int = 14) -> float | None:
    """Wilder's RSI(period) on a closes series."""
    if len(closes) < period + 1:
        return None
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(d, 0.0) for d in deltas]
    losses = [max(-d, 0.0) for d in deltas]
    avg_gain = statistics.mean(gains[:period])
    avg_loss = statistics.mean(losses[:period])
    for gain, loss in zip(gains[period:], losses[period:]):
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _atr(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> float | None:
    """Wilder-smoothed Average True Range."""
    if len(closes) < period + 1:
        return None
    trs = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        trs.append(tr)
    if len(trs) < period:
        return None
    atr = statistics.mean(trs[:period])
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr


def _realized_vol(closes: list[float], periods_per_year: float = PERIODS_PER_YEAR_5M) -> float | None:
    """Stdev of log returns, annualized."""
    returns = _log_returns(closes)
    if len(returns) < 2:
        return None
    stdev = statistics.stdev(returns)
    return stdev * math.sqrt(periods_per_year)


def _zscore(current: float | None, baseline: list[float]) -> float | None:
    """(current - mean(baseline)) / stdev(baseline). None if too little baseline data."""
    if current is None:
        return None
    baseline = [b for b in baseline if b is not None]
    if len(baseline) < 2:
        return None
    mean = statistics.mean(baseline)
    stdev = statistics.stdev(baseline)
    if stdev == 0:
        return 0.0
    return (current - mean) / stdev


def _vwap(candles: list[list]) -> float | None:
    """Cumulative typical-price * volume VWAP over the given candle window."""
    if not candles:
        return None
    num = 0.0
    den = 0.0
    for c in candles:
        _, _o, h, lo, close, vol = c
        typical = (h + lo + close) / 3.0
        num += typical * vol
        den += vol
    if den == 0:
        return None
    return num / den


def _resample_candles(candles_5m: list[list], factor: int) -> list[list]:
    """Aggregate consecutive, non-overlapping groups of `factor` 5m candles
    into fewer, longer-horizon OHLCV candles: open = the group's first
    candle's open, high = max high in the group, low = min low in the
    group, close = the group's last candle's close, volume = sum of the
    group's volumes. Each input candle is `[ts, open, high, low, close,
    volume]`. A trailing partial group (fewer than `factor` candles left)
    is dropped rather than emitted as an incomplete candle."""
    if not candles_5m or factor <= 0:
        return []
    out = []
    n = len(candles_5m)
    for start in range(0, n - factor + 1, factor):
        group = candles_5m[start:start + factor]
        ts = group[0][0]
        o = group[0][1]
        h = max(c[2] for c in group)
        lo = min(c[3] for c in group)
        close = group[-1][4]
        vol = sum(c[5] for c in group)
        out.append([ts, o, h, lo, close, vol])
    return out


def _pct_return(closes: list[float], periods_back: int) -> float | None:
    if len(closes) < periods_back + 1:
        return None
    prev = closes[-(periods_back + 1)]
    if prev == 0:
        return None
    return (closes[-1] - prev) / prev


def _is_buy(side) -> bool:
    """Classify a recentTrades `side` value as a buy/aggressor-buy.

    Real Hyperliquid data uses "B"/"A" (buy/ask-side aggressor); the stub
    backend's get_liquidations() historically used "long"/"short". Accept
    all of these so both real and stub data classify correctly without
    assuming one fixed vocabulary (see design doc).
    """
    return str(side).strip().upper() in ("B", "BUY", "LONG")


def _slippage_estimate(levels: list[list[float]], mid: float | None, notional: float) -> float | None:
    """Estimated pct price impact of a `notional`-sized market buy, walking
    the ask-side book levels and computing size-weighted avg fill price vs
    mid. If the fetched depth (top 5 levels) can't fully absorb `notional`,
    the estimate reflects whatever the available levels could fill."""
    if not levels or not mid:
        return None
    remaining = notional
    qty_total = 0.0
    for px, sz in levels:
        if remaining <= 0:
            break
        level_notional = px * sz
        take = min(remaining, level_notional)
        if px <= 0:
            continue
        qty_total += take / px
        remaining -= take
    if qty_total == 0:
        return None
    filled_notional = notional - remaining
    avg_price = filled_notional / qty_total
    return (avg_price - mid) / mid


# ---------------------------------------------------------------------------
# Per-asset fetch + compute
# ---------------------------------------------------------------------------

async def _safe(factory, default, retries=2, delay=1.0):
    """Await the coroutine from factory(), retrying up to `retries` times
    with `delay` seconds between attempts before falling back to default.

    Retries are additive on top of the HTTP-level retries inside the
    HyperliquidClient, so a transient blip lasting a few seconds gets
    absorbed rather than silently producing null fields in the packet.
    """
    for attempt in range(retries + 1):
        try:
            return await factory()
        except Exception:
            if attempt < retries:
                logger.debug(
                    "heartbeat: fetch failed (attempt %d/%d), retrying in %.1fs",
                    attempt + 1, retries, delay,
                )
                await asyncio.sleep(delay)
            else:
                logger.warning(
                    "heartbeat: fetch failed after %d retries, using default",
                    retries, exc_info=True,
                )
                return default


async def _fetch_asset_snapshot(provider, asset: str) -> dict:
    """All I/O for one asset. Each sub-fetch is wrapped in _safe() so a
    single failed call degrades that asset's fields to None rather than
    crashing the whole cycle or dropping the asset from the packet."""
    now_ms = int(time.time() * 1000)
    candle_start_ms = now_ms - LOOKBACK_HOURS * 3600 * 1000
    funding_start_ms = now_ms - FUNDING_LOOKBACK_HOURS * 3600 * 1000

    candles, funding_history, oi, funding, book, trades = await asyncio.gather(
        _safe(lambda: provider.get_ohlcv(asset, "5m", LOOKBACK_CANDLES), []),
        _safe(lambda: provider.get_funding_history(asset, funding_start_ms), []),
        _safe(lambda: provider.get_open_interest(asset), {}),
        _safe(lambda: provider.get_funding_rate(asset), {}),
        _safe(lambda: provider.get_orderbook(asset, depth=5), {"bids": [], "asks": []}),
        _safe(lambda: provider.get_recent_trades(asset, hours=TRADE_TAPE_HOURS), []),
    )
    return {
        "candles": candles or [],
        "funding_history": funding_history or [],
        "oi": oi or {},
        "funding": funding or {},
        "book": book or {"bids": [], "asks": []},
        "trades": trades or [],
    }


def compute_replayable_fields(
    candles: list[list],
    funding_history: list[dict],
    oi_val: float | None,
    funding_val: float | None,
    prior_oi_history: list[float],
    liq_data: dict[str, float | None] | None = None,
) -> dict:
    """Every field derivable purely from candles/funding/OI/liquidations --
    exactly the subset store/ledger.py's ledger stores (candles_5m, funding,
    oi, liquidations). This is the ONLY set of fields a historical backtest
    can ever compute, since the ledger deliberately never captures order-book
    depth or the trade tape (the retired microstructure paradigm -- see
    docs/STRATEGIC_ASSESSMENT_2026-07-04.md). Used both by the live heartbeat
    (via _compute_asset_fields below) and by backtest/engine.py.
    """
    if not candles:
        return {}

    closes = [c[4] for c in candles]
    highs = [c[2] for c in candles]
    lows = [c[3] for c in candles]
    volumes = [c[5] for c in candles]

    price = closes[-1]
    volume = volumes[-1]

    return_5m = _pct_return(closes, 1)
    return_30m = _pct_return(closes, 6)
    return_4h = _pct_return(closes, 48)
    return_24h = _pct_return(closes, 288)

    atr = _atr(highs, lows, closes, 14)
    rsi = _rsi(closes, 14)
    ema20 = _ema(closes, 20)
    ema50 = _ema(closes, 50)
    ema200 = _ema(closes, 200)
    realized_vol = _realized_vol(closes)
    vwap = _vwap(candles)
    vwap_distance = (price - vwap) / vwap if vwap else None

    volume_zscore = _zscore(volume, volumes[:-1]) if len(volumes) > 1 else None

    funding_history_vals = [
        f.get("fundingRate") for f in funding_history if f.get("fundingRate") is not None
    ]
    funding_zscore = _zscore(funding_val, funding_history_vals)

    oi_zscore = _zscore(oi_val, prior_oi_history)

    # OI drawdown: percentage change from the prior OI sample. Negative means
    # OI dropped between cycles, which is a hallmark of forced liquidations
    # (positions removed mechanically). steel_crane uses this as its OI-drawdown
    # pillar; copper_vane and violet_lion also reference OI change magnitude.
    oi_prior = prior_oi_history[-1] if prior_oi_history else None
    oi_drawdown_pct = (
        (oi_val - oi_prior) / oi_prior
        if oi_val is not None and oi_prior is not None and oi_prior != 0
        else None
    )

    # Liquidation cascade flag: composite 0/1 signal that fires when OI drops
    # sharply (>3%) while volume is elevated (z-score > 1.5) and price moves
    # hard (>1.5% in 5m). All three conditions together are a strong proxy for
    # a forced-liquidation cascade rather than organic selling.
    liquidation_cascade_flag = (
        1
        if (oi_drawdown_pct is not None
            and oi_drawdown_pct < -0.03
            and volume_zscore is not None
            and volume_zscore > 1.5
            and abs(return_5m or 0) > 0.015)
        else 0
    )

    # Raw 5m series (already fetched for the indicators above — no new API
    # call) plus two longer-horizon resamples of that same series, carried
    # in the packet so a trade fingerprint recorded off this asset's fields
    # can capture real OHLCV context, not just derived indicators.
    candles_5m = candles
    candles_30m = _resample_candles(candles, RESAMPLE_FACTOR_30M)
    candles_4h = _resample_candles(candles, RESAMPLE_FACTOR_4H)

    result = {
        "price": price,
        "return_5m": return_5m,
        "return_30m": return_30m,
        "return_4h": return_4h,
        "return_24h": return_24h,
        "volume": volume,
        "open_interest": oi_val,
        "funding": funding_val,
        "atr": atr,
        "realized_vol": realized_vol,
        "rsi": rsi,
        "ema20": ema20,
        "ema50": ema50,
        "ema200": ema200,
        "vwap_distance": vwap_distance,
        "volume_zscore": volume_zscore,
        "funding_zscore": funding_zscore,
        "oi_zscore": oi_zscore,
        "oi_drawdown_pct": oi_drawdown_pct,
        "liquidation_cascade_flag": liquidation_cascade_flag,
        "candles_5m": candles_5m,
        "candles_30m": candles_30m,
        "candles_4h": candles_4h,
        # Liquidation data from Coinalyze (15-min lookback)
        "liq_total_usd": liq_data.get("liq_total_usd") if liq_data else None,
        "liq_long_usd": liq_data.get("liq_long_usd") if liq_data else None,
        "liq_short_usd": liq_data.get("liq_short_usd") if liq_data else None,
    }

    # Pre-initialise multi-key feature outputs so they are always present
    # in the result dict even when the registered function fails (the
    # exception handler only sets the single registered key to None).
    for _k in (
        "statistical_forecast_return", "statistical_forecast_vol",
        "statistical_forecast_up_prob",
    ):
        result[_k] = None

    # raw_data shape FEATURE_REGISTRY functions expect: only the replayable
    # inputs they were ever documented to need (funding_history for
    # funding_acceleration; nothing here needs book/trades).
    raw_data_for_features = {"funding_history": funding_history}
    for feature_name, feature_fn in FEATURE_REGISTRY.items():
        try:
            val = feature_fn(
                candles=candles, closes=closes, highs=highs,
                lows=lows, volumes=volumes, fields=result,
                raw_data=raw_data_for_features,
            )
            if isinstance(val, dict):
                # Dict-returning features (e.g. statistical_forecast)
                # spread their keys directly into the output.
                result.update(val)
            else:
                result[feature_name] = val
        except Exception:
            result[feature_name] = None

    return result


def _compute_live_only_fields(raw: dict, price: float) -> dict:
    """Order-book and trade-tape fields -- never available to a backtest,
    since the ledger deliberately never captures this data (retired
    microstructure paradigm). Only the live heartbeat calls this."""
    book = raw["book"]
    bids = (book.get("bids") or [])[:5]
    asks = (book.get("asks") or [])[:5]
    bid_depth = sum(sz for _px, sz in bids) if bids else 0.0
    ask_depth = sum(sz for _px, sz in asks) if asks else 0.0
    best_bid = bids[0][0] if bids else None
    best_ask = asks[0][0] if asks else None
    mid = (best_bid + best_ask) / 2.0 if best_bid is not None and best_ask is not None else price
    spread = (
        (best_ask - best_bid) / mid
        if best_bid is not None and best_ask is not None and mid
        else None
    )
    depth_imbalance = (
        (bid_depth - ask_depth) / (bid_depth + ask_depth)
        if (bid_depth + ask_depth) > 0
        else None
    )
    # top5_imbalance uses the exact same top-5 levels as bid_depth/ask_depth
    # above, so it is identical by construction — kept as a separate field
    # per spec for Task B's consumers.
    top5_imbalance = depth_imbalance

    slippage_estimate = _slippage_estimate(asks, mid, REFERENCE_NOTIONAL_USD)

    trades = raw["trades"]
    buy_volume = sum(t["size"] for t in trades if _is_buy(t.get("side")))
    sell_volume = sum(t["size"] for t in trades if not _is_buy(t.get("side")))
    total_vol = buy_volume + sell_volume
    aggressor_ratio = buy_volume / total_vol if total_vol > 0 else 0.5
    avg_trade_size = statistics.mean([t["size"] for t in trades]) if trades else None
    largest_trade = max((t["size"] * t["price"] for t in trades), default=None)

    # Large-trade volume: sum of notional (size * price) for trades that are
    # > 3x the average trade size. On Hyperliquid the trade tape includes all
    # fills; outsized trades during volatile periods are statistically likely
    # to be liquidations (forced market orders). This gives steel_crane its
    # primary "$10M / $5M / $2M" magnitude proxy without a dedicated API.
    large_trade_threshold = (avg_trade_size or 0) * 3
    large_trade_volume_usd = (
        sum(t["size"] * t["price"] for t in trades if t["size"] > large_trade_threshold)
        if trades and avg_trade_size and large_trade_threshold > 0
        else None
    )

    return {
        "spread": spread,
        "bid_depth": bid_depth,
        "ask_depth": ask_depth,
        "depth_imbalance": depth_imbalance,
        "top5_imbalance": top5_imbalance,
        "slippage_estimate": slippage_estimate,
        "buy_volume": buy_volume,
        "sell_volume": sell_volume,
        "aggressor_ratio": aggressor_ratio,
        "avg_trade_size": avg_trade_size,
        "largest_trade": largest_trade,
        "large_trade_volume_usd": large_trade_volume_usd,
    }


def _compute_asset_fields(
    raw: dict,
    prior_oi_history: list[float],
    liq_data: dict[str, float | None] | None = None,
) -> dict:
    """Live entry point: replayable + live-only fields merged. Output shape
    is unchanged from before this function was split -- see
    test_compute_asset_fields_unchanged_after_refactor."""
    candles = raw["candles"]
    if not candles:
        return {k: None for k in PER_ASSET_FIELDS}

    oi_val = raw["oi"].get("openInterest")
    funding_val = raw["funding"].get("fundingRate")

    replayable = compute_replayable_fields(
        candles, raw["funding_history"], oi_val, funding_val, prior_oi_history, liq_data,
    )
    live_only = _compute_live_only_fields(raw, replayable["price"])

    return {**replayable, **live_only}


# ---------------------------------------------------------------------------
# Cross-asset
# ---------------------------------------------------------------------------

def correlation_matrix(asset_returns: dict[str, list[float]]) -> dict[str, dict[str, float | None]]:
    """Pairwise Pearson correlation of 5m log returns, as a nested dict."""
    assets = list(asset_returns.keys())
    valid = {a: r for a, r in asset_returns.items() if r}
    if len(valid) < 2:
        return {a: {b: None for b in assets} for a in assets}
    min_len = min(len(r) for r in valid.values())
    if min_len < 2:
        return {a: {b: None for b in assets} for a in assets}
    trimmed = {a: r[-min_len:] for a, r in valid.items()}
    df = pd.DataFrame(trimmed)
    corr = df.corr()
    result = {}
    for a in assets:
        row = {}
        for b in assets:
            if a in corr.index and b in corr.columns and pd.notna(corr.loc[a, b]):
                row[b] = float(corr.loc[a, b])
            else:
                row[b] = None
        result[a] = row
    return result


def compute_pca(asset_returns: dict[str, list[float]], n_components: int = 3) -> dict:
    """PCA of the 5m-return matrix via numpy eigendecomposition of the
    covariance matrix (no scikit-learn dependency)."""
    valid = {a: r for a, r in asset_returns.items() if r}
    assets = list(valid.keys())
    empty = {"explained_variance_ratio": [], "first_component_loadings": {}}
    if len(assets) < 2:
        return empty
    min_len = min(len(r) for r in valid.values())
    if min_len < 2:
        return empty

    matrix = np.array([valid[a][-min_len:] for a in assets])  # (n_assets, n_periods)
    cov = np.cov(matrix)
    eigvals, eigvecs = np.linalg.eigh(cov)
    order = np.argsort(eigvals)[::-1]
    eigvals = eigvals[order]
    eigvecs = eigvecs[:, order]

    total = eigvals.sum()
    k = min(n_components, len(eigvals))
    explained = [float(eigvals[i] / total) if total > 0 else 0.0 for i in range(k)]
    first_component_loadings = {assets[i]: float(eigvecs[i, 0]) for i in range(len(assets))}
    return {
        "explained_variance_ratio": explained,
        "first_component_loadings": first_component_loadings,
    }


def sector_strength(assets_fields: dict[str, dict]) -> dict[str, float | None]:
    """Mean return_24h per sector, using the exact SECTORS grouping."""
    result = {}
    for sector, members in SECTORS.items():
        vals = [
            assets_fields[a]["return_24h"]
            for a in members
            if a in assets_fields and assets_fields[a].get("return_24h") is not None
        ]
        result[sector] = statistics.mean(vals) if vals else None
    return result


def _compute_cross_asset(assets_fields: dict[str, dict], asset_returns: dict[str, list[float]]) -> dict:
    returns_24h = {
        a: f["return_24h"] for a, f in assets_fields.items() if f.get("return_24h") is not None
    }
    values = list(returns_24h.values())

    market_breadth = (
        sum(1 for v in values if v > 0) / len(assets_fields) if assets_fields else 0.0
    )
    average_return = statistics.mean(values) if values else 0.0
    median_return = statistics.median(values) if values else 0.0
    leader = max(returns_24h, key=returns_24h.get) if returns_24h else None
    laggard = min(returns_24h, key=returns_24h.get) if returns_24h else None

    btc_return = returns_24h.get(BTC_ASSET, 0.0)
    relative_strength = {a: (v - btc_return) for a, v in returns_24h.items()}
    momentum_rankings = sorted(returns_24h, key=returns_24h.get, reverse=True)

    return {
        "market_breadth": market_breadth,
        "average_return": average_return,
        "median_return": median_return,
        "leader": leader,
        "laggard": laggard,
        "correlation_matrix": correlation_matrix(asset_returns),
        "pca": compute_pca(asset_returns),
        "sector_strength": sector_strength(assets_fields),
        "momentum_rankings": momentum_rankings,
        "relative_strength": relative_strength,
    }


# ---------------------------------------------------------------------------
# Regime
# ---------------------------------------------------------------------------

async def _fetch_fear_greed() -> int | None:
    """Fetch the crypto Fear & Greed index from alternative.me. Third-party
    dependency outside Hyperliquid — must never block or crash the cycle."""
    try:
        async with httpx.AsyncClient(timeout=FEAR_GREED_TIMEOUT_SECONDS) as client:
            resp = await client.get(FEAR_GREED_URL)
            resp.raise_for_status()
            data = resp.json()
            return int(data["data"][0]["value"])
    except Exception:
        logger.warning("crypto_fear_index fetch failed, using None", exc_info=True)
        return None


def _compute_regime(
    assets_fields: dict[str, dict],
    cross_asset: dict,
    oi_history_after: dict[str, list[float]],
    fear_index: int | None,
    regime_tag: str,
) -> dict:
    # btc_dominance: Hyperliquid has no market-wide dominance endpoint. This
    # approximates dominance WITHIN THE TRACKED UNIVERSE only (BTC OI / sum
    # of OI across the 20 tracked assets) — not true market-wide dominance.
    oi_values = {
        a: f["open_interest"] for a, f in assets_fields.items() if f.get("open_interest") is not None
    }
    total_oi = sum(oi_values.values())
    btc_oi = oi_values.get(BTC_ASSET, 0.0)
    btc_dominance = btc_oi / total_oi if total_oi > 0 else None

    vols = [f["realized_vol"] for f in assets_fields.values() if f.get("realized_vol") is not None]
    average_volatility = statistics.mean(vols) if vols else 0.0

    fundings = [f["funding"] for f in assets_fields.values() if f.get("funding") is not None]
    average_funding = statistics.mean(fundings) if fundings else 0.0

    # average_oi_growth: pct change of each asset's OI relative to the
    # OLDEST sample in its rolling OI-history window (see OI_HISTORY_PATH
    # caveat above). Assets with fewer than 2 samples are excluded rather
    # than crashing.
    oi_growth = []
    for history in oi_history_after.values():
        if len(history) >= 2 and history[0]:
            oi_growth.append((history[-1] - history[0]) / history[0])
    average_oi_growth = statistics.mean(oi_growth) if oi_growth else None

    market_breadth = cross_asset["market_breadth"]

    # risk_on_score / trend_score: simple heuristic composites, not
    # authoritative quant theory — a reasonable starting point the captain
    # can retune later. average_volatility_reference (1.0 == 100%
    # annualized vol) is a fixed constant used only to normalize into 0-1.
    average_volatility_reference = 1.0
    risk_on_score = (
        market_breadth * 0.5
        + (1.0 if average_funding > 0 else 0.0) * 0.25
        + (1.0 - min(average_volatility / average_volatility_reference, 1.0)) * 0.25
    )
    trend_score = (
        cross_asset["average_return"] / average_volatility if average_volatility > 0 else 0.0
    )

    return {
        "crypto_fear_index": fear_index,
        "btc_dominance": btc_dominance,
        "average_volatility": average_volatility,
        "average_funding": average_funding,
        "average_oi_growth": average_oi_growth,
        "market_breadth": market_breadth,
        "risk_on_score": risk_on_score,
        "trend_score": trend_score,
        "regime_tag": regime_tag,
    }


async def _fetch_regime_tag(provider) -> str:
    """Categorical regime tag (trending_bull/trending_bear/range_low_vol/
    range_high_vol/crisis) from classify_regime() on BTC daily candles.
    Fetched once per heartbeat cycle, not per-agent, so downstream consumers
    (fingerprints, trade-bank queries) keep the categorical tag they depend
    on without any consumer calling the provider directly. Never raises —
    degrades to the classifier's own low-data default."""
    try:
        btc_1d = await provider.get_ohlcv(BTC_ASSET, "1d", REGIME_LOOKBACK_DAYS)
        return classify_regime(btc_1d or [])
    except Exception:
        logger.warning("heartbeat: BTC daily OHLCV fetch failed for regime_tag", exc_info=True)
        return classify_regime([])


async def _fetch_liquidations_batch(
    universe: list[str],
    coinalyze_api_key: str | None = None,
) -> dict[str, dict[str, float | None] | None]:
    """Fetch liquidation data for all universe assets via Coinalyze.

    Returns a dict mapping each project asset to its liquidation feature
    dict (``{"liq_total_usd", "liq_long_usd", "liq_short_usd"}``) or
    ``None`` if Coinalyze is unreachable or the asset is not covered.
    """
    try:
        from market.coinalyze import CoinalyzeClient
    except ImportError:
        logger.warning("heartbeat: CoinalyzeClient not available", exc_info=True)
        return {a: None for a in universe}

    try:
        async with CoinalyzeClient(api_key=coinalyze_api_key) as client:
            return await client.fetch_liquidations_for_assets(universe)
    except Exception:
        logger.warning(
            "heartbeat: Coinalyze liquidations fetch failed, using None for all assets",
            exc_info=True,
        )
        return {a: None for a in universe}


# ---------------------------------------------------------------------------
# OI-history rolling state (deliberate workaround for missing OI-history API)
# ---------------------------------------------------------------------------

def _load_oi_history(path: str) -> dict[str, list[float]]:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        logger.warning("failed to read OI history at %s", path, exc_info=True)
        return {}


def _save_oi_history(path: str, history: dict[str, list[float]]) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w") as f:
        json.dump(history, f)
    os.replace(tmp_path, path)


def _update_oi_history(history: dict[str, list[float]], asset: str, oi_value: float | None) -> list[float]:
    """Append this cycle's OI sample (capped at OI_HISTORY_MAX_SAMPLES),
    mutating `history` in place. Returns the PRIOR (pre-append) series, used
    as the z-score baseline so the current sample doesn't dilute its own
    baseline."""
    prior = list(history.get(asset, []))
    if oi_value is not None:
        series = history.get(asset, [])
        series = series + [oi_value]
        history[asset] = series[-OI_HISTORY_MAX_SAMPLES:]
    return prior


# ---------------------------------------------------------------------------
# Git-native ledger export -- replaces the old verbose full-packet mirror.
# Only the lean, backtest-relevant raw fields are exported per asset, not
# every derived indicator the heartbeat computes -- derived fields are
# recomputed from these raw inputs at read time, never trusted as frozen
# historical fact. See
# docs/superpowers/specs/2026-07-07-git-native-data-ledger-design.md.
# ---------------------------------------------------------------------------

def export_heartbeat_to_ledger(
    packet: dict, when: datetime | None = None, ledger_dir: str | None = None,
) -> None:
    """Decompose one heartbeat packet into lean per-type ledger records.

    `ledger_dir` defaults to None so it resolves store.ledger.LEDGER_DIR at
    call time via append_ledger_record's own None-sentinel handling --
    binding it to `= LEDGER_DIR` here would silently defeat test isolation
    the same way store/ledger.py's own docstring warns against.

    Never raises: a malformed timestamp, or one asset's malformed data,
    must not stop export for the rest of the universe or propagate into
    generate_heartbeat()'s hot path -- each asset is isolated so a single
    bad entry degrades only that asset's records, not the whole cycle.
    """
    try:
        ts = packet.get("timestamp")
        if not ts:
            return
        moment = when or datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except Exception:
        logger.warning(
            "export_heartbeat_to_ledger: bad timestamp %r", packet.get("timestamp"),
            exc_info=True,
        )
        return

    for asset, fields in (packet.get("assets") or {}).items():
        try:
            candle = (fields.get("candles_5m") or [None])[-1]
            if candle is not None:
                append_ledger_record(
                    "candles_5m",
                    {"ts": ts, "asset": asset, "o": candle[1], "h": candle[2],
                     "l": candle[3], "c": candle[4], "v": candle[5]},
                    moment, ledger_dir,
                )

            if fields.get("funding") is not None:
                append_ledger_record(
                    "funding", {"ts": ts, "asset": asset, "rate": fields["funding"]},
                    moment, ledger_dir,
                )

            if fields.get("open_interest") is not None:
                append_ledger_record(
                    "oi", {"ts": ts, "asset": asset, "oi": fields["open_interest"]},
                    moment, ledger_dir,
                )

            liq_total = fields.get("liq_total_usd")
            if liq_total is not None:
                append_ledger_record(
                    "liquidations",
                    {
                        "ts": ts, "asset": asset, "total_usd": liq_total,
                        "long_usd": fields.get("liq_long_usd"),
                        "short_usd": fields.get("liq_short_usd"),
                    },
                    moment, ledger_dir,
                )
        except Exception:
            logger.warning(
                "export_heartbeat_to_ledger: failed for asset %s", asset, exc_info=True,
            )


# ---------------------------------------------------------------------------
# Atomic write / read contract
# ---------------------------------------------------------------------------

def write_heartbeat(path: str, packet: dict) -> None:
    """Atomic write: write to f"{path}.tmp", then os.replace(tmp, path).
    os.replace is atomic on both POSIX and Windows, so no reader ever
    observes a half-written file."""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w") as f:
        json.dump(packet, f, indent=2)
    os.replace(tmp_path, path)


def read_heartbeat(path: str) -> dict | None:
    """Read + JSON-parse the heartbeat file. Returns None (after logging a
    warning) if the file doesn't exist or fails to parse — never raises."""
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        logger.warning("failed to read heartbeat at %s", path, exc_info=True)
        return None


def read_heartbeat_or_none(path: str, max_age_seconds: float) -> dict | None:
    """Staleness-aware wrapper around read_heartbeat(). Returns None (never
    raises) if the file is missing, unparseable, has no/garbled `timestamp`,
    or is older than `max_age_seconds`. Task B's consumers (decision loop,
    paper bridge, /api/prices) all use this instead of read_heartbeat()
    directly so a stale packet is treated the same as a missing one."""
    packet = read_heartbeat(path)
    if packet is None:
        return None
    timestamp = packet.get("timestamp")
    if not timestamp:
        logger.warning("heartbeat at %s has no timestamp field", path)
        return None
    try:
        written_at = datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except (ValueError, TypeError):
        logger.warning("heartbeat at %s has unparseable timestamp %r", path, timestamp)
        return None
    age_seconds = (datetime.now(timezone.utc) - written_at).total_seconds()
    if age_seconds > max_age_seconds:
        logger.warning(
            "heartbeat at %s is stale (%.0fs old, max %.0fs)", path, age_seconds, max_age_seconds
        )
        return None
    return packet


def heartbeat_max_age_seconds(config: dict) -> float:
    """Shared staleness-cutoff policy: tolerate one missed cycle before
    calling the heartbeat stale."""
    desk_cfg = config.get("desk", {})
    interval = desk_cfg.get("heartbeat_interval_seconds", DEFAULT_HEARTBEAT_INTERVAL_SECONDS)
    return 2 * interval


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

async def generate_heartbeat(provider, config: dict) -> dict:
    """Fetch + compute + atomically write one heartbeat packet. Returns the
    packet dict that was written.

    Not wired into forge.py's scheduler here — Task B's concern. Useful
    directly for tests and for APScheduler wiring in that follow-up PR.
    """
    desk_cfg = config.get("desk", {})
    heartbeat_path = desk_cfg.get("heartbeat_path", DEFAULT_HEARTBEAT_PATH)
    universe = config.get("universe", [])
    coinalyze_key = config.get("coinalyze", {}).get("api_key")

    oi_history = _load_oi_history(OI_HISTORY_PATH)
    sem = asyncio.Semaphore(5)

    async def _one(asset: str):
        async with sem:
            return asset, await _fetch_asset_snapshot(provider, asset)

    # Fire concurrent I/O tasks: Fear & Greed, regime tag, Coinalyze
    fear_index_task = asyncio.create_task(_fetch_fear_greed())
    regime_tag_task = asyncio.create_task(_fetch_regime_tag(provider))
    liq_task = _fetch_liquidations_batch(universe, coinalyze_key)

    raw_results = await asyncio.gather(*[_one(a) for a in universe])
    liq_data_by_asset = await liq_task
    # Coinalyze returns {asset: {field: value}}; heartbeat expects {field: value}
    # unwrap the nested structure before passing to _compute_asset_fields
    unwrapped_liq_data = {}
    for asset, fields in liq_data_by_asset.items():
        if isinstance(fields, dict) and "liq_total_usd" in fields:
            unwrapped_liq_data[asset] = fields

    assets_fields: dict[str, dict] = {}
    asset_returns: dict[str, list[float]] = {}

    for asset, raw in raw_results:
        raw_oi = raw["oi"].get("openInterest")
        prior_history = _update_oi_history(oi_history, asset, raw_oi)
        liq_for_asset = unwrapped_liq_data.get(asset)
        assets_fields[asset] = _compute_asset_fields(
            raw, prior_history, liq_for_asset,
        )
        closes = [c[4] for c in raw["candles"]]
        asset_returns[asset] = _log_returns(closes) if len(closes) > 1 else []

    _save_oi_history(OI_HISTORY_PATH, oi_history)

    cross_asset = _compute_cross_asset(assets_fields, asset_returns)
    fear_index = await fear_index_task
    regime_tag = await regime_tag_task
    regime = _compute_regime(assets_fields, cross_asset, oi_history, fear_index, regime_tag)

    # Event calendar — macro releases, token unlocks, scheduled events.
    # Failure is isolated: errors return empty event dict, never crash.
    try:
        events = read_events_for_heartbeat(universe)
    except Exception:
        logger.warning("event calendar read failed, using empty events", exc_info=True)
        events = {}

    packet = {
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "assets": assets_fields,
        "cross_asset": cross_asset,
        "regime": regime,
        "events": events,
    }
    write_heartbeat(heartbeat_path, packet)
    export_heartbeat_to_ledger(packet)
    return packet
