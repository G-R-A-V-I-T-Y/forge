#!/usr/bin/env python
"""
build_training_dataset.py

Offline, read-only post-processing of the heartbeat ledger
(``ledger/{candles_5m,funding,oi,liquidations}/{YYYY-MM}.jsonl`` or
``data/historical_data/YYYY-MM-DD.jsonl`` for backward compatibility)
into a flat feature/label table for model training.

Reads:   ledger/{candles_5m,funding,oi,liquidations}/{YYYY-MM}.jsonl|parquet
         (primary, when ``--ledger-dir`` is provided or ledger files exist)
         OR
         data/historical_data/YYYY-MM-DD.jsonl  (legacy fallback)
Writes:  data/historical_data/training_dataset.parquet  (one row per asset per
         heartbeat timestamp)

This script has no import dependency on ``market/heartbeat.py`` and is not
part of the live heartbeat cycle -- it is meant to be run by hand or from a
scheduled batch job.

Ledger data source (``--ledger-dir``):
  Candles_5m, funding, OI, and (optionally) liquidation records are read
  from their monthly partitions, then merged per asset on timestamp using
  backward-fill (asof join) so that each candle row carries the most recent
  funding rate and OI value known at that moment.

Legacy data source (no ``--ledger-dir``):
  Reads heartbeat JSONL files from ``data/historical_data/``. Each file
  contains one heartbeat packet per line with the shape:
      {"timestamp": "...Z", "assets": {"<ASSET>-PERP": {...fields...}, ...},
       "cross_asset": {...}, "regime": {...}}
  Per-asset scalar fields are flattened dynamically.

Forward-looking labels at each configured horizon:
  - fwd_return_<h>          forward pct return of price
  - fwd_vol_<h>             realized volatility (stdev of pct changes) over the window
  - fwd_maxdd_<h>           max drawdown (most negative cumulative return) over the window
  - fwd_maxrunup_<h>        max run-up (most positive cumulative return) over the window
  - fwd_funding_accrued_<h> sum of the `funding` field over the window
  - fwd_stop_hit_<h>        "sl" / "tp" / "none" -- whichever of the illustrative
                            +/-2%% stop-loss / +/-5%% take-profit levels
                            (DEFAULT_SL_PCT / DEFAULT_TP_PCT below) is crossed
                            first, if either

Gap handling: if the heartbeat timeline has a gap larger than
STALENESS_THRESHOLD (2x the expected 5-minute cadence) anywhere between a
sample and its forward horizon, that (sample, horizon) combination is
excluded (all its label columns are left null) rather than computed over
incomplete data. The sample's own row and its other, unaffected horizons
are unaffected.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import json

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "historical_data"
DEFAULT_OUTPUT = DATA_DIR / "training_dataset.parquet"

EXPECTED_INTERVAL = timedelta(minutes=5)
STALENESS_THRESHOLD = (
    EXPECTED_INTERVAL * 2
)  # 10 min; mirrors heartbeat_max_age_seconds()

# Horizons in minutes. Configurable via build_dataset()/--horizons; this is
# just the default set, not hardcoded into the label logic below.
DEFAULT_HORIZONS_MINUTES = [30, 120, 240, 1440]  # 30m, 2h, 4h, 24h

# Illustrative stop-loss / take-profit levels used for the fwd_stop_hit_*
# label. Purely a labeling convenience -- not a real risk-gate rule (see
# risk/gate.py for the actual mandatory stop-loss policy) -- and easily
# swapped for a different pair via build_dataset(sl_pct=..., tp_pct=...).
DEFAULT_SL_PCT = 0.02
DEFAULT_TP_PCT = 0.05

# Per-asset fields whose value is a nested structure (OHLCV candle arrays)
# rather than a scalar -- excluded from the flattened feature columns since
# they don't fit a flat row/column table.
_NON_SCALAR_ASSET_FIELDS = {"candles_5m", "candles_30m", "candles_4h"}

# Default ledger directory (same as store/ledger.LEDGER_DIR but independent
# so this script has no import-time coupling to the live system).
LEDGER_DIR = Path(__file__).resolve().parent.parent / "ledger"


def horizon_label(minutes: int) -> str:
    """Render a horizon in minutes as a short label: 30 -> "30m", 120 -> "2h"."""
    if minutes % 60 == 0:
        return f"{minutes // 60}h"
    return f"{minutes}m"


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def _all_jsonl_files(
    data_dir: Path, start_date: date | None, end_date: date | None
) -> list[Path]:
    """Sorted *.jsonl files in data_dir, optionally restricted to a date
    range (inclusive) based on the YYYY-MM-DD filename stem."""
    files = sorted(data_dir.glob("*.jsonl"))
    if start_date is None and end_date is None:
        return files
    selected = []
    for f in files:
        try:
            file_date = datetime.strptime(f.stem, "%Y-%m-%d").date()
        except ValueError:
            continue
        if start_date is not None and file_date < start_date:
            continue
        if end_date is not None and file_date > end_date:
            continue
        selected.append(f)
    return selected


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    """Parse every JSON line from *path*, skipping and warning on malformed lines."""
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, 1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                records.append(json.loads(raw))
            except json.JSONDecodeError as exc:
                print(f"WARNING: skipping malformed JSON at {path}:{lineno}: {exc}")
    return records


# ---------------------------------------------------------------------------
# Row construction
# ---------------------------------------------------------------------------


def _flatten_asset(asset: dict[str, Any]) -> dict[str, Any]:
    """Flatten one per-asset sub-record's scalar fields into `asset.<field>`
    columns, skipping nested OHLCV candle arrays."""
    return {
        f"asset.{field}": value
        for field, value in asset.items()
        if field not in _NON_SCALAR_ASSET_FIELDS
    }


def _build_row(
    packet: dict[str, Any], asset_key: str, asset: dict[str, Any]
) -> dict[str, Any]:
    """Flatten one packet's (asset_key, asset) pair into a single flat row."""
    row: dict[str, Any] = {
        "timestamp": packet.get("timestamp"),
        "asset_key": asset_key,
        "regime_tag": (packet.get("regime") or {}).get("regime_tag"),
    }
    row.update(_flatten_asset(asset))
    return row


def _rows_from_packets(packets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for pkt in packets:
        for asset_key, asset in (pkt.get("assets") or {}).items():
            rows.append(_build_row(pkt, asset_key, asset))
    return rows


# ---------------------------------------------------------------------------
# Ledger reading (alternative to JSONL path)
# ---------------------------------------------------------------------------


def _read_ledger_partitions(
    ledger_dir: Path, kind: str,
    start_date: date | None, end_date: date | None,
) -> pd.DataFrame:
    """Read all monthly partitions for a ledger *kind*, filtered by date.

    Reads both ``.parquet`` (preferred) and ``.jsonl`` files in
    ``ledger_dir / kind /``, sorts by timestamp, and returns a DataFrame
    with a parsed ``_ts`` column. Returns empty DataFrame if no files
    exist.
    """
    kind_dir = ledger_dir / kind
    if not kind_dir.is_dir():
        return pd.DataFrame()
    frames: list[pd.DataFrame] = []
    for p in sorted(kind_dir.glob("*.parquet")):
        frames.append(pd.read_parquet(p))
    for p in sorted(kind_dir.glob("*.jsonl")):
        frames.append(pd.read_json(p, lines=True))
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    df["_ts"] = pd.to_datetime(df["ts"], utc=True)
    if start_date is not None or end_date is not None:
        mask = pd.Series([True] * len(df))
        if start_date is not None:
            mask &= df["_ts"] >= pd.Timestamp(start_date, tz="UTC")
        if end_date is not None:
            mask &= df["_ts"] < pd.Timestamp(end_date, tz="UTC") + pd.Timedelta(days=1)
        df = df[mask]
    return df.sort_values("_ts").reset_index(drop=True)


def _rows_from_ledger(
    ledger_dir: Path,
    start_date: date | None,
    end_date: date | None,
) -> list[dict[str, Any]]:
    """Reconstruct per-asset-per-timestamp rows from ledger partitions.

    Candles_5m is the primary clock; funding and OI are backward-filled
    (asof-merged) so each candle row carries the most recent funding rate
    and OI value known at that moment. Liquidation fields are joined the
    same way when available.
    """
    candles = _read_ledger_partitions(ledger_dir, "candles_5m", start_date, end_date)
    if candles.empty:
        return []

    funding = _read_ledger_partitions(ledger_dir, "funding", start_date, end_date)
    oi = _read_ledger_partitions(ledger_dir, "oi", start_date, end_date)
    liq = _read_ledger_partitions(ledger_dir, "liquidations", start_date, end_date)

    rows: list[dict[str, Any]] = []
    # Process one asset at a time so merge_asof runs per-asset (avoiding
    # cross-asset contamination during the backward-fill).
    for asset_key in sorted(candles["asset"].unique()):
        asset_c = candles[candles["asset"] == asset_key].sort_values("_ts").copy()

        if not funding.empty:
            asset_f = funding[funding["asset"] == asset_key].sort_values("_ts")
            if not asset_f.empty:
                asset_c = pd.merge_asof(
                    asset_c, asset_f[["_ts", "asset", "rate"]],
                    on="_ts", by="asset", direction="backward",
                )
                asset_c.rename(columns={"rate": "funding"}, inplace=True)
            else:
                asset_c["funding"] = None
        else:
            asset_c["funding"] = None

        if not oi.empty:
            asset_o = oi[oi["asset"] == asset_key].sort_values("_ts")
            if not asset_o.empty:
                asset_c = pd.merge_asof(
                    asset_c, asset_o[["_ts", "asset", "oi"]],
                    on="_ts", by="asset", direction="backward",
                )
                asset_c.rename(columns={"oi": "open_interest"}, inplace=True)
            else:
                asset_c["open_interest"] = None
        else:
            asset_c["open_interest"] = None

        if not liq.empty:
            asset_l = liq[liq["asset"] == asset_key].sort_values("_ts")
            if not asset_l.empty:
                asset_c = pd.merge_asof(
                    asset_c, asset_l[["_ts", "asset", "total_usd", "long_usd", "short_usd"]],
                    on="_ts", by="asset", direction="backward",
                )
                asset_c.rename(
                    columns={
                        "total_usd": "liq_total_usd",
                        "long_usd": "liq_long_usd",
                        "short_usd": "liq_short_usd",
                    },
                    inplace=True,
                )

        for _, r in asset_c.iterrows():
            row: dict[str, Any] = {
                "timestamp": r["ts"],
                "asset_key": r["asset"],
                "asset.price": r["c"],
            }
            if pd.notna(r.get("funding")):
                row["asset.funding"] = float(r["funding"])
            if pd.notna(r.get("volume")) and r.get("volume") is not None:
                row["asset.volume"] = float(r["v"])
            if pd.notna(r.get("open_interest")):
                row["asset.open_interest"] = float(r["open_interest"])
            if pd.notna(r.get("liq_total_usd")):
                row["asset.liq_total_usd"] = float(r["liq_total_usd"])
            if pd.notna(r.get("liq_long_usd")):
                row["asset.liq_long_usd"] = float(r["liq_long_usd"])
            if pd.notna(r.get("liq_short_usd")):
                row["asset.liq_short_usd"] = float(r["liq_short_usd"])
            rows.append(row)

    return rows


# ---------------------------------------------------------------------------
# Labels
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LabelWindow:
    """Result of computing one horizon's forward labels for one base sample.
    All fields are None when the sample+horizon combination is excluded
    (insufficient or gappy forward data)."""

    fwd_return: float | None = None
    fwd_vol: float | None = None
    fwd_maxdd: float | None = None
    fwd_maxrunup: float | None = None
    fwd_funding_accrued: float | None = None
    fwd_stop_hit: str | None = None


def _label_one(
    asset_df: pd.DataFrame,
    base_idx: int,
    horizon: timedelta,
    sl_pct: float,
    tp_pct: float,
) -> LabelWindow:
    """Compute forward labels for one (row, horizon) pair. `asset_df` must be
    sorted by timestamp ascending with a default RangeIndex, containing
    columns "timestamp", "price", "funding" for a single asset."""
    base_ts = asset_df.at[base_idx, "timestamp"]
    base_price = asset_df.at[base_idx, "price"]
    if pd.isna(base_price) or base_price == 0:
        return LabelWindow()

    target_end = base_ts + horizon
    future = asset_df[
        (asset_df["timestamp"] > base_ts) & (asset_df["timestamp"] <= target_end)
    ]
    if future.empty:
        return LabelWindow()

    # Gap check: no consecutive-sample gap within [base_ts, ...future] may
    # exceed the staleness threshold.
    window_ts = pd.concat(
        [pd.Series([base_ts]), future["timestamp"]], ignore_index=True
    )
    if (window_ts.diff().dropna() > STALENESS_THRESHOLD).any():
        return LabelWindow()

    # The window must actually reach close to the horizon end -- otherwise
    # the dataset simply doesn't extend far enough yet and the label would
    # be computed over an incomplete window.
    last_future_ts = future["timestamp"].iloc[-1]
    if (target_end - last_future_ts) > STALENESS_THRESHOLD:
        return LabelWindow()

    end_price = future["price"].iloc[-1]
    if pd.isna(end_price) or end_price == 0:
        return LabelWindow()

    fwd_return = (end_price - base_price) / base_price

    window_prices = pd.concat(
        [pd.Series([base_price]), future["price"]], ignore_index=True
    ).astype(float)
    pct_changes = window_prices.pct_change().dropna()
    fwd_vol = float(pct_changes.std()) if len(pct_changes) >= 2 else 0.0

    cum_returns = (window_prices - base_price) / base_price
    fwd_maxdd = float(cum_returns.min())
    fwd_maxrunup = float(cum_returns.max())

    funding_series = future["funding"].dropna()
    fwd_funding_accrued = (
        float(funding_series.sum()) if not funding_series.empty else None
    )

    fwd_stop_hit = "none"
    for future_price in future["price"]:
        if pd.isna(future_price):
            continue
        cum_ret = (future_price - base_price) / base_price
        if cum_ret <= -sl_pct:
            fwd_stop_hit = "sl"
            break
        if cum_ret >= tp_pct:
            fwd_stop_hit = "tp"
            break

    return LabelWindow(
        fwd_return=float(fwd_return),
        fwd_vol=fwd_vol,
        fwd_maxdd=fwd_maxdd,
        fwd_maxrunup=fwd_maxrunup,
        fwd_funding_accrued=fwd_funding_accrued,
        fwd_stop_hit=fwd_stop_hit,
    )


def _compute_labels(
    df: pd.DataFrame,
    horizons_minutes: list[int],
    sl_pct: float = DEFAULT_SL_PCT,
    tp_pct: float = DEFAULT_TP_PCT,
) -> pd.DataFrame:
    """Return a copy of df with forward-label columns added for every
    configured horizon, computed per asset_key group."""
    if not df.index.equals(pd.RangeIndex(len(df))):
        raise ValueError(
            "_compute_labels requires df to have a contiguous 0..len(df)-1 "
            "RangeIndex; call df.reset_index(drop=True) before invoking it"
        )

    for required_col in ("asset.price", "asset.funding"):
        if required_col not in df.columns:
            df[required_col] = None

    label_cols: dict[str, list[Any]] = {}
    horizons = [(m, horizon_label(m), timedelta(minutes=m)) for m in horizons_minutes]
    for _minutes, label, _td in horizons:
        for suffix in (
            "return",
            "vol",
            "maxdd",
            "maxrunup",
            "funding_accrued",
            "stop_hit",
        ):
            label_cols[f"fwd_{suffix}_{label}"] = [None] * len(df)

    for _asset_key, group in df.groupby("asset_key", sort=False):
        asset_df = (
            group[["timestamp", "asset.price", "asset.funding"]]
            .rename(columns={"asset.price": "price", "asset.funding": "funding"})
            .sort_values("timestamp")
        )
        # `positions` maps the reset 0..n-1 index used inside _label_one
        # back to df's original row index, so results can be written back
        # to the correct row.
        positions = list(asset_df.index)
        asset_df = asset_df.reset_index(drop=True)

        for pos, original_idx in enumerate(positions):
            for minutes, label, td in horizons:
                result = _label_one(asset_df, pos, td, sl_pct, tp_pct)
                label_cols[f"fwd_return_{label}"][original_idx] = result.fwd_return
                label_cols[f"fwd_vol_{label}"][original_idx] = result.fwd_vol
                label_cols[f"fwd_maxdd_{label}"][original_idx] = result.fwd_maxdd
                label_cols[f"fwd_maxrunup_{label}"][original_idx] = result.fwd_maxrunup
                label_cols[f"fwd_funding_accrued_{label}"][
                    original_idx
                ] = result.fwd_funding_accrued
                label_cols[f"fwd_stop_hit_{label}"][original_idx] = result.fwd_stop_hit

    out = df.copy()
    for col, values in label_cols.items():
        out[col] = values
    return out


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_dataset(
    data_dir: Path | None = None,
    output_path: Path | None = None,
    horizon_minutes: list[int] | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    sl_pct: float = DEFAULT_SL_PCT,
    tp_pct: float = DEFAULT_TP_PCT,
    ledger_dir: Path | None = None,
) -> pd.DataFrame:
    """Build the full training dataset and write it to Parquet.

    Two data-source modes (mutually exclusive):
      1. **Ledger** — set *ledger_dir* (or leave as ``None`` and the default
         ``LEDGER_DIR`` will be used if its ``candles_5m`` subdirectory
         exists). Reads from ``ledger/{candles_5m,funding,oi}/{YYYY-MM}.{jsonl,parquet}``.
      2. **Legacy JSONL** — set *data_dir* to a directory of heartbeat JSONL
         files each containing one heartbeat packet per line.

    Returns the resulting DataFrame.
    """
    output_path = output_path if output_path is not None else DEFAULT_OUTPUT
    horizons = (
        horizon_minutes if horizon_minutes is not None else DEFAULT_HORIZONS_MINUTES
    )

    # ----- Decide data source -----
    use_ledger = ledger_dir is not None  # explicit --ledger-dir
    if not use_ledger and data_dir is None:
        # Auto-detect when neither source is explicitly given: default ledger
        # takes priority if it has a candles_5m subdirectory.
        if (LEDGER_DIR / "candles_5m").is_dir():
            use_ledger = True
            ledger_dir = LEDGER_DIR

    if use_ledger:
        effective_ledger = ledger_dir if ledger_dir is not None else LEDGER_DIR
        all_rows = _rows_from_ledger(effective_ledger, start_date, end_date)
        source_desc = str(effective_ledger)
        if not all_rows:
            print(f"No candle rows found in ledger at {effective_ledger} for the date range")
            return pd.DataFrame()
    else:
        # Legacy JSONL fallback
        data_dir = data_dir if data_dir is not None else DATA_DIR
        jsonl_files = _all_jsonl_files(data_dir, start_date, end_date)
        if not jsonl_files:
            print(f"No *.jsonl files found in {data_dir} for the requested date range")
            return pd.DataFrame()
        all_rows = []
        for jsonl_path in jsonl_files:
            all_rows.extend(_rows_from_packets(_load_jsonl(jsonl_path)))
        source_desc = str(data_dir)
        if not all_rows:
            print("No rows extracted from JSONL files")
            return pd.DataFrame()

    df = pd.DataFrame(all_rows)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    df = df.sort_values(["asset_key", "timestamp"]).reset_index(drop=True)

    df = _compute_labels(df, horizons, sl_pct=sl_pct, tp_pct=tp_pct)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(output_path, engine="pyarrow", index=False)
    print(f"Wrote {len(df)} rows, {len(df.columns)} columns to {output_path} "
          f"(source: {source_desc})")

    return df


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build training dataset from heartbeat ledger or legacy JSONL"
    )
    parser.add_argument(
        "-l",
        "--ledger-dir",
        type=Path,
        default=None,
        help="Ledger directory with candles_5m/funding/oi partitions "
             "(default: auto-detect ledger/; fall back to --data-dir JSONL)",
    )
    parser.add_argument(
        "-d",
        "--data-dir",
        type=Path,
        default=None,
        help="Directory with legacy *.jsonl files (default: %(default)s)",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help=f"Output Parquet path (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--start-date",
        type=_parse_date,
        default=None,
        help="Earliest date (YYYY-MM-DD) of data to include, inclusive",
    )
    parser.add_argument(
        "--end-date",
        type=_parse_date,
        default=None,
        help="Latest date (YYYY-MM-DD) of data to include, inclusive",
    )
    parser.add_argument(
        "-H",
        "--horizons",
        type=int,
        nargs="+",
        default=None,
        help=f"Horizons in minutes (default: {DEFAULT_HORIZONS_MINUTES})",
    )
    parser.add_argument(
        "--sl-pct",
        type=float,
        default=DEFAULT_SL_PCT,
        help=f"Illustrative stop-loss threshold as a fraction (default: {DEFAULT_SL_PCT})",
    )
    parser.add_argument(
        "--tp-pct",
        type=float,
        default=DEFAULT_TP_PCT,
        help=f"Illustrative take-profit threshold as a fraction (default: {DEFAULT_TP_PCT})",
    )
    args = parser.parse_args()

    build_dataset(
        data_dir=args.data_dir,
        output_path=args.output,
        horizon_minutes=args.horizons,
        start_date=args.start_date,
        end_date=args.end_date,
        sl_pct=args.sl_pct,
        tp_pct=args.tp_pct,
        ledger_dir=args.ledger_dir,
    )


if __name__ == "__main__":
    main()
