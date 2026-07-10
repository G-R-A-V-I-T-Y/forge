"""market/features.py — Approved derived feature library.

Each function is a pure computation from current heartbeat snapshot data.
No persistent state, no I/O. All features are computed from the 300x 5m
candles (25h lookback) and/or the already-computed per-asset fields.

To request a new feature: add a function here, register it in FEATURE_REGISTRY,
and wire it into heartbeat.py's per-asset computation.
"""

from __future__ import annotations

import statistics
from datetime import datetime, timezone

from market.event_calendar import EVENT_TYPE_TOKEN_UNLOCKS

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


def _atr_series(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> list[float]:
    """Wilder-smoothed ATR at every index i >= period, one-pass.

    Equivalent to calling `_atr(highs[:i+1], lows[:i+1], closes[:i+1], period)`
    for each i in range(period, len(closes)) -- Wilder's recurrence
    (`atr = (atr*(period-1) + tr) / period`) already only depends on the
    previous ATR value and the new true range, so recomputing it from
    scratch (as the original per-i _atr(...) calls did) was pure O(n^2)
    (each call itself re-walks the whole true-range series from index 1),
    made worse by the outer per-bar loop in backtest/engine.py calling this
    once per bar -- O(n^3) overall on a full backtest run. This computes
    the exact same sequence of values in one O(n) pass.
    """
    n = len(closes)
    if n < period + 1:
        return []
    trs = [
        max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        for i in range(1, n)
    ]
    if len(trs) < period:
        return []
    atr = statistics.mean(trs[:period])
    values = [atr]  # corresponds to i == period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
        values.append(atr)
    return values


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
    atr_values = _atr_series(highs, lows, closes, 14)
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


def _rolling_bb_widths(closes: list[float], window_size: int = 20) -> list[float]:
    """(4*stdev - SMA-normalized) Bollinger Band width at every fixed-size
    trailing window, one incremental pass.

    Equivalent to computing `statistics.mean`/`statistics.stdev` fresh over
    `closes[i-19:i+1]` for each `i in range(19, len(closes) - 1)` -- that
    original loop was already O(n) (each window is a fixed 20 elements, not
    growing), but paid real per-call overhead calling into the stdlib
    `statistics` module (~280 mean/stdev call pairs per invocation, each
    using exact-Fraction internals for precision). A sliding window's sum
    and sum-of-squares can be updated in O(1) per step instead, so the whole
    series is produced in one O(n) pass with plain float arithmetic and no
    per-step function-call overhead.

    Note: this uses a running-sum formula (sample variance = (sum_sq - n *
    mean^2) / (n - 1)), which is arithmetically equivalent to but NOT
    bit-for-bit identical to `statistics.stdev`'s exact-fraction computation
    -- results match to float precision (~1e-9 relative), not exactly.
    Acceptable here because bb_width_percentile's output is a percentile
    RANK, a relative ordering that a ~1e-9 relative float difference in one
    window's width practically never flips for a non-degenerate series.
    """
    n = len(closes)
    if n < window_size + 1:  # need range(window_size-1, n-1) to have at least one iteration
        return []
    widths = []
    window_sum = sum(closes[0:window_size])
    window_sumsq = sum(c * c for c in closes[0:window_size])
    for i in range(window_size - 1, n - 1):
        if i > window_size - 1:
            # Slide the window forward by one: drop closes[i-window_size],
            # add closes[i] -- matches window = closes[i-19:i+1].
            dropped = closes[i - window_size]
            added = closes[i]
            window_sum += added - dropped
            window_sumsq += added * added - dropped * dropped
        w_sma = window_sum / window_size
        variance = (window_sumsq - window_size * w_sma * w_sma) / (window_size - 1)
        w_std = variance ** 0.5 if variance > 0 else 0.0
        if w_sma == 0:
            continue
        widths.append((4.0 * w_std) / w_sma)
    return widths


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
    widths = _rolling_bb_widths(closes, 20)
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


@register("statistical_forecast")
def statistical_forecast(
    candles: list[list], closes: list[float], highs: list[float],
    lows: list[float], volumes: list[float], fields: dict,
    raw_data: dict,
) -> dict | None:
    """Regime-conditioned empirical return distribution statistics.

    Computes the mean return, standard deviation, and probability of an up
    move over a trailing window of N periods (default 48 x 5m = 4 hours).
    The window size is configurable via ``raw_data.get("forecast_periods", 48)``
    so it can be tuned per asset or per market regime without changing code.

    Returns a dict with three keys:
      - ``statistical_forecast_return`` — mean of the per-period returns
      - ``statistical_forecast_vol`` — standard deviation of the per-period returns
      - ``statistical_forecast_up_prob`` — fraction of periods with a positive return

    Returns ``None`` if insufficient candle history exists for the requested
    window (requires at least ``periods + 1`` closes).
    """
    periods = raw_data.get("forecast_periods", 48)
    if len(closes) < periods + 1:
        return None

    # Compute per-period simple returns over the trailing window.
    # closes[-periods-1] is the price at the start of the window;
    # closes[-periods:] are the prices at each period boundary, giving
    # us `periods` returns in total.
    start_idx = len(closes) - periods - 1
    returns = [
        (closes[i] - closes[i - 1]) / closes[i - 1]
        for i in range(start_idx + 1, len(closes))
        if closes[i - 1] > 0
    ]

    if len(returns) < 2:
        return None

    mean_ret = statistics.mean(returns)
    std_ret = statistics.stdev(returns)
    up_prob = sum(1 for r in returns if r > 0) / len(returns)

    return {
        "statistical_forecast_return": mean_ret,
        "statistical_forecast_vol": std_ret,
        "statistical_forecast_up_prob": up_prob,
    }


# ---------------------------------------------------------------------------
# Event-calendar-driven features (M8 sage_turtle) -- computed from per-asset
# raw event records threaded into raw_data as "asset_events" (a list of raw
# event dicts, each carrying a parsed "_scheduled_dt" UTC datetime alongside
# the original schema fields such as "type"/"asset_specific") and an
# optional "event_as_of" datetime (the point in time "now" is evaluated
# from -- wall-clock time live, the historical bar timestamp in a backtest,
# so no event is ever treated as known before its own recorded schedule).
# Both are wired in by market/heartbeat.py (live) and backtest/engine.py
# (replay) -- see compute_replayable_fields's asset_events/event_as_of
# parameters.
# ---------------------------------------------------------------------------

def _event_as_of(raw_data: dict) -> datetime:
    return raw_data.get("event_as_of") or datetime.now(timezone.utc)


def _upcoming_events(raw_data: dict, as_of: datetime) -> list[dict]:
    events = raw_data.get("asset_events") or []
    return [e for e in events if e.get("_scheduled_dt") and e["_scheduled_dt"] >= as_of]


def _extract_unlock_pct(event: dict) -> float | None:
    """Pull the unlock-size-as-pct-of-circulating-supply magnitude out of an
    event record. The design doc's example payload nests this under an
    "asset_specific" (or, inconsistently, "asset-specific") dict as
    "unlock_percentage"; also accept a flat top-level "unlock_percentage"
    in case a producer stores it there instead. Returns None (never raises)
    if the field is absent or unparseable -- callers treat that the same as
    no upcoming unlock."""
    for key in ("asset_specific", "asset-specific"):
        nested = event.get(key)
        if isinstance(nested, dict) and nested.get("unlock_percentage") is not None:
            try:
                return float(nested["unlock_percentage"])
            except (TypeError, ValueError):
                return None
    flat = event.get("unlock_percentage")
    if flat is not None:
        try:
            return float(flat)
        except (TypeError, ValueError):
            return None
    return None


@register("days_to_event")
def days_to_event(
    candles: list[list], closes: list[float], highs: list[float],
    lows: list[float], volumes: list[float], fields: dict,
    raw_data: dict,
) -> float | None:
    """Days until this asset's next scheduled event (any type), or None if
    the event calendar has no upcoming event for this asset (or event data is
    unavailable)."""
    as_of = _event_as_of(raw_data)
    upcoming = _upcoming_events(raw_data, as_of)
    if not upcoming:
        return None
    nearest = min(e["_scheduled_dt"] for e in upcoming)
    return (nearest - as_of).total_seconds() / 86400.0


@register("unlock_size_pct")
def unlock_size_pct(
    candles: list[list], closes: list[float], highs: list[float],
    lows: list[float], volumes: list[float], fields: dict,
    raw_data: dict,
) -> float | None:
    """Size of this asset's next upcoming token-unlock event as a percentage
    of circulating supply, or None if there is no upcoming unlock event (or
    its size is unavailable)."""
    as_of = _event_as_of(raw_data)
    unlock_events = [
        e for e in _upcoming_events(raw_data, as_of)
        if e.get("type") == EVENT_TYPE_TOKEN_UNLOCKS
    ]
    if not unlock_events:
        return None
    nearest = min(unlock_events, key=lambda e: e["_scheduled_dt"])
    return _extract_unlock_pct(nearest)
