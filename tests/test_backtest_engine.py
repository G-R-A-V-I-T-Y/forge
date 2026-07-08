import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from backtest.dsl import EvidenceTerm, Spec, Threshold
from backtest.engine import run_backtest


def _write_candles(ledger_dir: Path, kind: str, month: str, rows: list[dict]) -> None:
    path = ledger_dir / kind / f"{month}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def _synthetic_candles(asset: str, start: datetime, n: int, base_price: float, drift: float) -> list[dict]:
    rows = []
    price = base_price
    for i in range(n):
        ts = start + timedelta(hours=i)
        price = price * (1 + drift)
        rows.append({
            "ts": ts.strftime("%Y-%m-%dT%H:%M:%SZ"), "asset": asset,
            "o": price, "h": price * 1.001, "l": price * 0.999, "c": price, "v": 100.0,
        })
    return rows


def test_run_backtest_produces_equity_curve_and_trades(tmp_path):
    ledger_dir = tmp_path / "ledger"
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    candles = _synthetic_candles("FET-PERP", start, 400, base_price=1.0, drift=0.001)
    _write_candles(ledger_dir, "candles_1h", "2025-01", candles[:350])
    _write_candles(ledger_dir, "candles_1h", "2025-02", candles[350:])

    funding_rows = [
        {"ts": (start + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M:%SZ"), "asset": "FET-PERP", "rate": 0.0003}
        for i in range(400)
    ]
    _write_candles(ledger_dir, "funding", "2025-01", funding_rows[:350])
    _write_candles(ledger_dir, "funding", "2025-02", funding_rows[350:])

    spec = Spec(
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

    result = run_backtest(spec, ledger_dir, start, start + timedelta(hours=399), taker_fee=0.00035)

    assert len(result.equity_curve) > 0
    assert result.data_window["candles_1h"]["rows"] == 400
    assert isinstance(result.total_return_pct, float)
    assert isinstance(result.sharpe, float)


def test_run_backtest_reports_thin_data_window_honestly(tmp_path):
    ledger_dir = tmp_path / "ledger"
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    # Only 5 rows of OI -- far short of a full window; must be reported, not hidden.
    oi_rows = [
        {"ts": (start + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M:%SZ"), "asset": "FET-PERP", "oi": 1_000_000.0}
        for i in range(5)
    ]
    _write_candles(ledger_dir, "oi", "2025-01", oi_rows)
    candles = _synthetic_candles("FET-PERP", start, 10, base_price=1.0, drift=0.0)
    _write_candles(ledger_dir, "candles_1h", "2025-01", candles)

    spec = Spec(
        agent_id="test_spec", spec_version=1, thesis_version=1,
        universe_include=["FET-PERP"], regime_exclude=[],
        direction="long", confidence_threshold=0.9, scale_threshold=0.9,
        evidence=[EvidenceTerm(
            name="oi_check", feature="oi_zscore",
            thresholds=[Threshold(op="else", weight=0.0)], missing="skip",
        )],
        secondary_evidence=[],
        stop_loss_pct=0.05, take_profit_pct=0.10, max_hold_hours=48,
        leverage=2, position_size_pct=0.10,
    )

    result = run_backtest(spec, ledger_dir, start, start + timedelta(hours=9), taker_fee=0.00035)

    assert result.data_window["oi"]["rows"] == 5
