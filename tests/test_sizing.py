"""Tests for execution/sizing.py — the ONE confidence-sizing formula shared
by the live decision loop and the backtest engine (R4 AC#4: no third state).

The formula is the thesis rule every seed thesis states:
  confidence >= confidence_threshold (default 0.70) → full size
  scale_threshold <= confidence < confidence_threshold → base × (0.5 + 0.5·s)
    where s = (confidence − scale) / (confidence_threshold − scale)
  confidence < scale_threshold → full base size (the interpreter/gate is the
    entry decision; sizing never silently blocks an entry it was handed)
"""
import pytest

from execution.sizing import scale_position_size


class TestScalePositionSize:
    def test_full_size_at_threshold(self):
        assert scale_position_size(0.10, 0.70) == pytest.approx(0.10)

    def test_full_size_above_threshold(self):
        assert scale_position_size(0.10, 0.95) == pytest.approx(0.10)

    def test_half_size_at_scale_threshold(self):
        # s = 0 → factor 0.5
        assert scale_position_size(0.10, 0.50) == pytest.approx(0.05)

    def test_linear_between_thresholds(self):
        # conf 0.60 → s = 0.5 → factor 0.75
        assert scale_position_size(0.10, 0.60) == pytest.approx(0.075)

    def test_below_scale_threshold_full_base(self):
        # Matches backtest/engine.py's else-branch: sizing never vetoes an
        # entry the strategy already decided to take.
        assert scale_position_size(0.10, 0.20) == pytest.approx(0.10)

    def test_custom_spec_thresholds(self):
        # Spec thresholds override the defaults (compiled agents).
        assert scale_position_size(
            0.12, 0.40, confidence_threshold=0.50, scale_threshold=0.30
        ) == pytest.approx(0.12 * (0.5 + 0.5 * 0.5))

    def test_missing_confidence_full_size(self):
        assert scale_position_size(0.10, None) == pytest.approx(0.10)

    def test_matches_backtest_engine_formula(self):
        # Exact expression from backtest/engine.py so live and backtest
        # cannot drift: base × (0.5 + 0.5 × (conf − scale)/(confT − scale)).
        base, conf, conf_t, scale_t = 0.10, 0.55, 0.70, 0.50
        s = (conf - scale_t) / (conf_t - scale_t)
        assert scale_position_size(
            base, conf, confidence_threshold=conf_t, scale_threshold=scale_t
        ) == pytest.approx(base * (0.5 + 0.5 * s))

    def test_degenerate_equal_thresholds(self):
        # confidence_threshold == scale_threshold must not divide by zero.
        assert scale_position_size(
            0.10, 0.60, confidence_threshold=0.5, scale_threshold=0.5
        ) == pytest.approx(0.10)
