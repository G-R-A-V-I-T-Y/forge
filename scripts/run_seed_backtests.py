#!/usr/bin/env python
"""scripts/run_seed_backtests.py -- backtest the 3 hand-compiled seed specs.

Prints a report per spec: train/validate/test Sharpe, deflated Sharpe,
parameter sensitivity, and the actual data window used per feature stream
(honest about OI/liquidation-dependent specs having far less real history
than funding/price-driven ones). Does NOT run the backfill itself --
run scripts/backfill_history.py first.
"""
from __future__ import annotations

import glob
from pathlib import Path

import yaml

from backtest.dsl import load_spec
from backtest.walk_forward import run_walk_forward

LEDGER_DIR = Path("ledger")


def main() -> None:
    with open("config.yaml", "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)
    taker_fee = config["desk"]["taker_fee"]

    for spec_path in sorted(glob.glob("agents/specs/*.yaml")):
        spec = load_spec(spec_path)
        report = run_walk_forward(spec, LEDGER_DIR, taker_fee)

        print(f"\n=== {spec.agent_id} ===")
        print(f"  data window: {report.test.data_window}")
        print(f"  train: {len(report.train.trades)} trades, {report.train.total_return_pct:+.2%} return, Sharpe {report.train.sharpe:.2f}")
        print(f"  validate: {len(report.validate.trades)} trades, {report.validate.total_return_pct:+.2%} return, Sharpe {report.validate.sharpe:.2f}")
        print(f"  test: {len(report.test.trades)} trades, {report.test.total_return_pct:+.2%} return, Sharpe {report.test.sharpe:.2f}")
        print(f"  deflated Sharpe: {report.deflated_sharpe:.2f}")
        print(f"  parameter sensitivity: {report.parameter_sensitivity}")


if __name__ == "__main__":
    main()
