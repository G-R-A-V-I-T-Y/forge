#!/usr/bin/env python
"""scripts/compact_ledger.py -- Monthly ledger compaction.

Converts closed-month JSONL ledger partitions to Parquet (smaller,
columnar) and, for the highest-volume fine-grained streams, downsamples
data older than DECAY_WINDOW_MONTHS to hourly resolution. Never touches
the current month's hot JSONL file -- only fully-closed months are
eligible. Idempotent: re-running against an already-compacted month is a
no-op since the source .jsonl no longer exists. See
docs/superpowers/specs/2026-07-07-git-native-data-ledger-design.md.
"""
from __future__ import annotations

import argparse
import logging
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)

LEDGER_DIR = Path(__file__).resolve().parent.parent / "ledger"
DECAY_WINDOW_MONTHS = 12
DECAY_ELIGIBLE_KINDS = {"candles_5m", "oi"}


def _current_month() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def _closed_month_files(ledger_dir: Path) -> list[Path]:
    current = _current_month()
    return sorted(p for p in ledger_dir.glob("*/*.jsonl") if p.stem < current)


def _months_ago(month_str: str) -> int:
    year, month = (int(x) for x in month_str.split("-"))
    now = datetime.now(timezone.utc)
    return (now.year - year) * 12 + (now.month - month)


def compact_file(path: Path, decay_window_months: int = DECAY_WINDOW_MONTHS) -> Path:
    """Convert one closed-month .jsonl file to .parquet, deleting the
    source. If the file's kind is decay-eligible and its month is older
    than `decay_window_months`, downsample to hourly (first sample of each
    UTC hour per asset) before writing."""
    kind = path.parent.name
    month = path.stem

    df = pd.read_json(path, lines=True)
    if (
        kind in DECAY_ELIGIBLE_KINDS
        and _months_ago(month) > decay_window_months
        and not df.empty
        and "asset" in df.columns
    ):
        ts = pd.to_datetime(df["ts"], utc=True)
        df = (
            df.assign(_hour=ts.dt.floor("h"))
            .sort_values("ts")
            .groupby(["_hour", "asset"], as_index=False)
            .first()
            .drop(columns=["_hour"])
        )

    out_path = path.with_suffix(".parquet")
    df.to_parquet(out_path, engine="pyarrow", index=False)
    path.unlink()
    return out_path


def compact_ledger(
    ledger_dir: Path = LEDGER_DIR, decay_window_months: int = DECAY_WINDOW_MONTHS
) -> list[Path]:
    written = []
    for path in _closed_month_files(ledger_dir):
        try:
            out = compact_file(path, decay_window_months)
        except Exception:
            logger.warning(
                "Failed to compact %s -- skipping, source left in place for next run",
                path, exc_info=True,
            )
            continue
        written.append(out)
        print(f"Compacted {path} -> {out}")
    return written


def main() -> None:
    parser = argparse.ArgumentParser(description="Compact closed-month ledger JSONL to Parquet")
    parser.add_argument("--ledger-dir", type=Path, default=LEDGER_DIR)
    parser.add_argument("--decay-window-months", type=int, default=DECAY_WINDOW_MONTHS)
    args = parser.parse_args()
    compact_ledger(args.ledger_dir, args.decay_window_months)


if __name__ == "__main__":
    main()
