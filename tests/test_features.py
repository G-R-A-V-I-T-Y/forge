"""tests/test_features.py -- regression tests for the perf fixes applied to
market/features.py's atr_percentile and bb_width_percentile.

Both functions originally recomputed their entire trailing indicator series
from scratch on every single call (atr_percentile: genuinely O(n^2) per
call, since Wilder's ATR recursion was re-walked from index 1 for every i;
bb_width_percentile: O(n) per call already, but with heavy stdlib
`statistics.mean`/`stdev` constant-factor overhead per fixed-size window).
These tests pin a slow, verbatim-from-the-original-code reference
implementation against the optimized one so a future edit can't silently
change output while chasing more speed.
"""
from __future__ import annotations

import random
import statistics

import pytest

from market.features import atr_percentile, bb_width_percentile, _percentile_rank


# ---------------------------------------------------------------------------
# Reference (slow, original-shape) implementations -- copied verbatim from
# the pre-optimization code, kept ONLY here as a correctness oracle.
# ---------------------------------------------------------------------------

def _reference_atr(highs, lows, closes, period=14):
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


def _reference_atr_percentile(closes, highs, lows, current_atr):
    if current_atr is None or len(closes) < 15:
        return None
    atr_values = []
    for i in range(14, len(closes)):
        atr_i = _reference_atr(highs[:i + 1], lows[:i + 1], closes[:i + 1], 14)
        if atr_i is not None:
            atr_values.append(atr_i)
    if not atr_values:
        return None
    return _percentile_rank(current_atr, atr_values)


def _reference_bb_width_percentile(closes, current_width):
    if len(closes) < 21 or current_width is None:
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


def _synthetic_series(n=300, seed=42):
    rng = random.Random(seed)
    closes = [100.0]
    for _ in range(n - 1):
        closes.append(closes[-1] * (1 + rng.uniform(-0.01, 0.01)))
    highs = [c * 1.002 for c in closes]
    lows = [c * 0.998 for c in closes]
    volumes = [rng.uniform(100, 1000) for _ in closes]
    return closes, highs, lows, volumes


@pytest.mark.parametrize("n", [15, 20, 50, 300])
def test_atr_percentile_matches_reference_exactly(n):
    closes, highs, lows, volumes = _synthetic_series(n=n)
    current_atr = _reference_atr(highs, lows, closes, 14)
    fields = {"atr": current_atr}

    expected = _reference_atr_percentile(closes, highs, lows, current_atr)
    actual = atr_percentile(None, closes, highs, lows, volumes, fields, {})

    assert actual == expected, "optimized atr_percentile must be bit-for-bit identical to the original per-i recomputation"


@pytest.mark.parametrize("n", [21, 25, 50, 300])
def test_bb_width_percentile_matches_reference_within_float_tolerance(n):
    closes, highs, lows, volumes = _synthetic_series(n=n)
    window = closes[-20:]
    sma = statistics.mean(window)
    std = statistics.stdev(window)
    current_width = (4.0 * std) / sma if sma else None
    fields = {}

    expected = _reference_bb_width_percentile(closes, current_width)
    actual = bb_width_percentile(None, closes, highs, lows, volumes, fields, {})

    # The optimized version uses an O(1)-per-step incremental rolling
    # sum/sum-of-squares instead of recomputing statistics.mean/stdev (which
    # use exact-fraction internals) from scratch per window -- this changes
    # the arithmetic path, so equality is to tight float tolerance, not
    # bit-for-bit. A percentile RANK is inherently a relative measure, so a
    # ~1e-9 relative difference in an individual window's width essentially
    # never flips the rank outcome for a realistic (non-degenerate) series.
    assert expected is not None and actual is not None
    assert actual == pytest.approx(expected, rel=1e-9, abs=1e-12)


def test_atr_percentile_none_below_minimum_history():
    closes, highs, lows, volumes = _synthetic_series(n=10)
    assert atr_percentile(None, closes, highs, lows, volumes, {"atr": 1.0}, {}) is None


def test_bb_width_percentile_none_below_minimum_history():
    closes, highs, lows, volumes = _synthetic_series(n=10)
    assert bb_width_percentile(None, closes, highs, lows, volumes, {}, {}) is None
