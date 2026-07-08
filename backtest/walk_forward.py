"""backtest/walk_forward.py -- train/validate/test split, deflated Sharpe,
and a parameter-sensitivity sweep.

Single 70/15/15 split, not rolling -- real history depth varies too much by
feature (12mo candles/funding vs. days of OI/liquidations) to justify a
rolling harness yet. See
docs/superpowers/specs/2026-07-07-strategy-spec-dsl-backtester-design.md
section 3.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from backtest.dsl import Spec
from backtest.engine import BacktestResult, run_backtest

TRAIN_FRACTION = 0.70
VALIDATE_FRACTION = 0.15
# remaining 0.15 is the test window

PERTURBATION_PCT = 0.20


@dataclass
class WalkForwardReport:
    train: BacktestResult
    validate: BacktestResult
    test: BacktestResult
    deflated_sharpe: float = 0.0
    parameter_sensitivity: dict = field(default_factory=dict)


def _ledger_date_range(ledger_dir: Path, spec: Spec) -> tuple[datetime, datetime]:
    """Full available candles_1h date range across the spec's universe."""
    kind_dir = ledger_dir / "candles_1h"
    all_ts = []
    for path in sorted(kind_dir.glob("*.jsonl")) + sorted(kind_dir.glob("*.parquet")):
        df = pd.read_json(path, lines=True) if path.suffix == ".jsonl" else pd.read_parquet(path)
        if "asset" in df.columns:
            df = df[df["asset"].isin(spec.universe_include)]
        if not df.empty:
            all_ts.extend(pd.to_datetime(df["ts"], utc=True).tolist())
    if not all_ts:
        now = datetime.now(timezone.utc)
        return now, now
    return min(all_ts).to_pydatetime(), max(all_ts).to_pydatetime()


def _deflated_sharpe(sharpe: float, n_trials: int, n_returns: int) -> float:
    """Simplified deflated Sharpe: penalizes the raw Sharpe for the number
    of parameter combinations effectively searched (n_trials, here the
    parameter-sensitivity sweep's trial count) and the sample size backing
    it. A conservative approximation, not the full Bailey-Lopez-de-Prado
    formula -- adequate for flagging "this edge is likely noise" without
    requiring a probability-distribution library dependency."""
    if n_returns < 2:
        return 0.0
    import math

    trial_penalty = math.sqrt(2 * math.log(max(n_trials, 1))) / math.sqrt(n_returns)
    return sharpe - trial_penalty


def run_walk_forward(spec: Spec, ledger_dir: Path, taker_fee: float) -> WalkForwardReport:
    full_start, full_end = _ledger_date_range(ledger_dir, spec)
    total_seconds = (full_end - full_start).total_seconds()

    train_end = full_start + (full_end - full_start) * TRAIN_FRACTION
    validate_end = train_end + (full_end - full_start) * VALIDATE_FRACTION

    train_result = run_backtest(spec, ledger_dir, full_start, train_end, taker_fee)
    validate_result = run_backtest(spec, ledger_dir, train_end, validate_end, taker_fee)
    test_result = run_backtest(spec, ledger_dir, validate_end, full_end, taker_fee)

    sensitivity = {}
    perturbable = ("confidence_threshold", "scale_threshold", "stop_loss_pct", "take_profit_pct")
    for field_name in perturbable:
        base_value = getattr(spec, field_name)
        perturbed_spec = dataclasses.replace(spec, **{field_name: base_value * (1 + PERTURBATION_PCT)})
        perturbed_result = run_backtest(perturbed_spec, ledger_dir, validate_end, full_end, taker_fee)
        sensitivity[field_name] = perturbed_result.sharpe - test_result.sharpe

    deflated = _deflated_sharpe(test_result.sharpe, n_trials=len(perturbable) + 1, n_returns=len(test_result.trades))

    return WalkForwardReport(
        train=train_result, validate=validate_result, test=test_result,
        deflated_sharpe=deflated, parameter_sensitivity=sensitivity,
    )
