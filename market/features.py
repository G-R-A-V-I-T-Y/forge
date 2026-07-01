"""market/features.py — Approved derived feature library.

Each function is a pure computation from current heartbeat snapshot data.
No persistent state, no I/O. All features are computed from the 300x 5m
candles (25h lookback) and/or the already-computed per-asset fields.

To request a new feature: add a function here, register it in FEATURE_REGISTRY,
and wire it into heartbeat.py's per-asset computation.
"""

from __future__ import annotations

import statistics

FEATURE_REGISTRY: dict[str, callable] = {}


def register(name):
    """Decorator to register a feature in the approved library."""
    def decorator(fn):
        FEATURE_REGISTRY[name] = fn
        return fn
    return decorator


# ---------------------------------------------------------------------------
# Local indicator helpers (avoids circular import from heartbeat.py)
# ---------------------------------------------------------------------------

def _atr(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> float | None:
    """Wilder-smoothed Average True Range (same impl as heartbeat.py)."""
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


def _percentile_rank(value: float, population: list[float]) -> float:
    """Empirical percentile: fraction of population values <= value."""
    if not population:
        return 0.0
    count = sum(1 for v in population if v <= value)
    return count / len(population)


# ---------------------------------------------------------------------------
# Approved derived features
# ---------------------------------------------------------------------------

@register("momentum_acceleration")
def momentum_acceleration(
    candles: list[list], closes: list[float], highs: list[float],
    lows: list[float], volumes: list[float], fields: dict,
    raw_data: dict,
) -> float | None:
    """Measures whether momentum is accelerating.

    Positive = recent 5m return per-period is outperforming the average of
    30m, 4h, 24h returns (each normalised to a per-period basis).
    """
    r5 = fields.get("return_5m")
    r30 = fields.get("return_30m")
    r4h = fields.get("return_4h")
    r24 = fields.get("return_24h")
    if any(v is None for v in (r5, r30, r4h, r24)):
        return None
    per_period_5m = r5 / 1.0
    per_period_30m = r30 / 6.0
    per_period_4h = r4h / 48.0
    per_period_24h = r24 / 288.0
    longer_avg = (per_period_30m + per_period_4h + per_period_24h) / 3.0
    return per_period_5m - longer_avg


@register("atr_percentile")
def atr_percentile(
    candles: list[list], closes: list[float], highs: list[float],
    lows: list[float], volumes: list[float], fields: dict,
    raw_data: dict,
) -> float | None:
    """Rank current ATR(14) vs all trailing ATR(14) values in the candle window."""
    current_atr = fields.get("atr")
    if current_atr is None or len(closes) < 15:
        return None
    atr_values = []
    for i in range(14, len(closes)):
        atr_i = _atr(highs[:i + 1], lows[:i + 1], closes[:i + 1], 14)
        if atr_i is not None:
            atr_values.append(atr_i)
    if not atr_values:
        return None
    return _percentile_rank(current_atr, atr_values)


@register("bb_width")
def bb_width(
    candles: list[list], closes: list[float], highs: list[float],
    lows: list[float], volumes: list[float], fields: dict,
    raw_data: dict,
) -> float | None:
    """Bollinger Band width: (upper - lower) / SMA(20)."""
    if len(closes) < 20:
        return None
    window = closes[-20:]
    sma = statistics.mean(window)
    std = statistics.stdev(window) if len(window) >= 2 else 0.0
    if sma == 0:
        return None
    return (4.0 * std) / sma


@register("bb_width_percentile")
def bb_width_percentile(
    candles: list[list], closes: list[float], highs: list[float],
    lows: list[float], volumes: list[float], fields: dict,
    raw_data: dict,
) -> float | None:
    """Percentile rank of current bb_width within the trailing distribution."""
    if len(closes) < 21:
        return None
    current_width = bb_width(candles, closes, highs, lows, volumes, fields, raw_data)
    if current_width is None:
        return None
    widths = []
    for i in range(19, len(closes) - 1):
        window = closes[i - 19:i + 1]
        w_sma = statistics.mean(window)
        w_std = statistics.stdev(window) if len(window) >= 2 else 0.0
        if w_sma == 0:
            continue
        widths.append((4.0 * w_std) / w_sma)
    if not widths:
        return None
    return _percentile_rank(current_width, widths)


@register("volume_percentile_14d")
def volume_percentile_14d(
    candles: list[list], closes: list[float], highs: list[float],
    lows: list[float], volumes: list[float], fields: dict,
    raw_data: dict,
) -> float | None:
    """Rank current volume vs all trailing volumes in the candle window."""
    if len(volumes) < 2:
        return None
    current_volume = volumes[-1]
    trailing = volumes[:-1]
    return _percentile_rank(current_volume, trailing)


@register("funding_acceleration")
def funding_acceleration(
    candles: list[list], closes: list[float], highs: list[float],
    lows: list[float], volumes: list[float], fields: dict,
    raw_data: dict,
) -> float | None:
    """Rate of change of funding rate — average change per period over the
    last three funding samples."""
    history = raw_data.get("funding_history", [])
    vals = [f.get("fundingRate") for f in history if f.get("fundingRate") is not None]
    if len(vals) < 3:
        return None
    return (vals[-1] - vals[-3]) / 2.0
