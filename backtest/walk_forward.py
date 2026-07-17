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
import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
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
    """Full available candles_5m date range across the spec's universe --
    matches backtest/engine.py's run_backtest, which now drives its per-bar
    loop off candles_5m (not candles_1h) for live/backtest feature parity."""
    kind_dir = ledger_dir / "candles_5m"
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


def _spec_hash(spec: Spec) -> str:
    """Deterministic SHA-256 of the spec's effective parameters for trial dedup."""
    import dataclasses as _dc
    fields_to_hash = [
        spec.agent_id, spec.direction,
        spec.confidence_threshold, spec.scale_threshold,
        spec.stop_loss_pct, spec.take_profit_pct,
        spec.max_hold_hours, spec.leverage, spec.position_size_pct,
        tuple(spec.universe_include),
        tuple(spec.regime_exclude),
    ]
    raw = "|".join(str(f) for f in fields_to_hash)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _count_overlapping_trials(
    conn, spec: Spec, window_start: datetime, window_end: datetime,
    lookback_days: int = 90,
) -> int:
    """Count prior backtest_trials for the same agent with overlapping data windows.

    Windows overlap when: existing.start < new.end AND existing.end > new.start.
    Only trials within the trailing *lookback_days* are counted, preventing
    ancient history from permanently inflating the deflation penalty.
    """
    cutoff = window_end - timedelta(days=lookback_days)
    row = conn.execute(
        """SELECT COUNT(*) FROM backtest_trials
           WHERE agent_id = ?
             AND data_window_end >= ?
             AND data_window_start <= ?
             AND data_window_end > ?""",
        (spec.agent_id, cutoff.isoformat(), window_end.isoformat(),
         window_start.isoformat()),
    ).fetchone()
    return row[0] if row else 0


def record_trial(conn, spec: Spec, data_window_start: datetime, data_window_end: datetime,
                 deflated_sharpe: float, outcome: str | None = None) -> None:
    """Insert a row into backtest_trials after a walk-forward run."""
    conn.execute(
        """INSERT INTO backtest_trials
               (spec_hash, agent_id, data_window_start, data_window_end,
                deflated_sharpe, outcome)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            _spec_hash(spec),
            spec.agent_id,
            data_window_start.isoformat(),
            data_window_end.isoformat(),
            deflated_sharpe,
            outcome,
        ),
    )
    conn.commit()


def run_walk_forward(spec: Spec, ledger_dir: Path, taker_fee: float,
                     conn=None) -> WalkForwardReport:
    full_start, full_end = _ledger_date_range(ledger_dir, spec)

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

    base_trials = len(perturbable) + 1
    if conn is not None:
        overlapping = _count_overlapping_trials(conn, spec, full_start, full_end)
        total_trials = base_trials + overlapping
    else:
        total_trials = base_trials

    deflated = _deflated_sharpe(test_result.sharpe, n_trials=total_trials, n_returns=len(test_result.trades))

    outcome = "pass" if deflated > 0 else "fail"

    if conn is not None:
        record_trial(conn, spec, full_start, full_end, deflated, outcome)

    return WalkForwardReport(
        train=train_result, validate=validate_result, test=test_result,
        deflated_sharpe=deflated, parameter_sensitivity=sensitivity,
    )
