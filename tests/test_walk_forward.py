from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from backtest.dsl import EvidenceTerm, Spec, Threshold
from backtest.walk_forward import run_walk_forward
from tests.test_backtest_engine import _synthetic_candles, _write_candles


def _spec():
    return Spec(
        agent_id="test_spec", spec_version=1, thesis_version=1,
        universe_include=["FET-PERP"], regime_exclude=[],
        direction="long", confidence_threshold=0.5, scale_threshold=0.3,
        evidence=[EvidenceTerm(
            name="funding_positive", feature="funding_zscore",
            thresholds=[Threshold(op=">", value=-100.0, weight=0.6), Threshold(op="else", weight=0.0)],
            missing="veto",
        )],
        secondary_evidence=[],
        stop_loss_pct=0.05, take_profit_pct=0.10, max_hold_hours=48,
        leverage=2, position_size_pct=0.10,
    )


def _seed_ledger(tmp_path: Path) -> Path:
    ledger_dir = tmp_path / "ledger"
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    candles = _synthetic_candles("FET-PERP", start, 1000, base_price=1.0, drift=0.0005)
    for month, chunk_start in (("2025-01", 0), ("2025-02", 350), ("2025-03", 700)):
        _write_candles(ledger_dir, "candles_1h", month, candles[chunk_start:chunk_start + 350])
    funding_rows = [
        {"ts": (start + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M:%SZ"), "asset": "FET-PERP", "rate": 0.0003}
        for i in range(1000)
    ]
    for month, chunk_start in (("2025-01", 0), ("2025-02", 350), ("2025-03", 700)):
        _write_candles(ledger_dir, "funding", month, funding_rows[chunk_start:chunk_start + 350])
    return ledger_dir


def test_walk_forward_splits_into_three_windows(tmp_path):
    ledger_dir = _seed_ledger(tmp_path)
    report = run_walk_forward(_spec(), ledger_dir, taker_fee=0.00035)

    assert report.train is not None
    assert report.validate is not None
    assert report.test is not None
    assert isinstance(report.deflated_sharpe, float)


def test_walk_forward_reports_parameter_sensitivity(tmp_path):
    ledger_dir = _seed_ledger(tmp_path)
    report = run_walk_forward(_spec(), ledger_dir, taker_fee=0.00035)

    assert "confidence_threshold" in report.parameter_sensitivity
    assert "stop_loss_pct" in report.parameter_sensitivity
    assert all(isinstance(v, float) for v in report.parameter_sensitivity.values())
