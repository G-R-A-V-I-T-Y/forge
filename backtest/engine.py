"""backtest/engine.py -- replay historical ledger data through the
interpreter, using the exact same feature-computation core the live
heartbeat uses (market.heartbeat.compute_replayable_fields).

Fee model matches the paper bridge's taker_fee. Slippage is a fixed,
conservative assumption (not execute_close's live slippage_estimate,
which needs order-book depth the ledger never captures) -- see
docs/superpowers/specs/2026-07-07-strategy-spec-dsl-backtester-design.md
section 3 for why this gap is real and stays documented, not hidden.
"""
from __future__ import annotations

import bisect
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from backtest.dsl import Spec
from backtest.interpreter import evaluate
from market.heartbeat import compute_replayable_fields

# Fixed backtest slippage assumption (pct of price), applied against the
# entry direction. Conservative relative to typical observed spread+impact
# on this universe's liquid assets; revisit once live paper-vs-backtest
# divergence data exists to calibrate against.
BACKTEST_SLIPPAGE_PCT = 0.0005

MIN_CANDLES_FOR_FEATURES = 20  # compute_replayable_fields needs enough history for ATR/RSI/EMA


@dataclass
class BacktestResult:
    trades: list[dict] = field(default_factory=list)
    equity_curve: list[tuple[datetime, float]] = field(default_factory=list)
    total_return_pct: float = 0.0
    sharpe: float = 0.0
    data_window: dict = field(default_factory=dict)


def _read_partitions(ledger_dir: Path, kind: str, asset: str) -> pd.DataFrame:
    kind_dir = ledger_dir / kind
    if not kind_dir.exists():
        return pd.DataFrame()
    frames = [pd.read_parquet(p) for p in sorted(kind_dir.glob("*.parquet"))]
    frames += [pd.read_json(p, lines=True) for p in sorted(kind_dir.glob("*.jsonl"))]
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    if "asset" in df.columns:
        df = df[df["asset"] == asset]
    if df.empty:
        return df
    df["_ts"] = pd.to_datetime(df["ts"], utc=True)
    return df.sort_values("_ts").reset_index(drop=True)


def _candles_to_plain_list(df: pd.DataFrame) -> tuple[list, list]:
    """Convert a sorted candles DataFrame into a plain Python list once (no
    per-bar re-filtering/iterrows). Returns (timestamps, candle_rows), both
    in ascending time order and index-aligned, so a per-bar cutoff can be
    found via bisect on `timestamps` and sliced directly out of
    `candle_rows` -- O(log n) + O(1) instead of re-scanning + re-converting
    the whole DataFrame on every bar."""
    if df.empty:
        return [], []
    timestamps = df["_ts"].tolist()
    candle_rows = [
        [int(ts.timestamp() * 1000), o, h, l, c, v]
        for ts, o, h, l, c, v in zip(
            timestamps, df["o"], df["h"], df["l"], df["c"], df["v"],
        )
    ]
    return timestamps, candle_rows


def _funding_to_plain_list(df: pd.DataFrame) -> tuple[list, list]:
    """Same idea as _candles_to_plain_list for the funding series."""
    if df.empty:
        return [], []
    timestamps = df["_ts"].tolist()
    funding_rows = [
        {"time": int(ts.timestamp() * 1000), "fundingRate": rate}
        for ts, rate in zip(timestamps, df["rate"])
    ]
    return timestamps, funding_rows


def _oi_to_plain_list(df: pd.DataFrame) -> tuple[list, list]:
    if df.empty:
        return [], []
    return df["_ts"].tolist(), df["oi"].tolist()


def run_backtest(
    spec: Spec, ledger_dir: Path, start: datetime, end: datetime, taker_fee: float,
) -> BacktestResult:
    result = BacktestResult()
    balance = 10_000.0  # notional backtest starting balance; only relative return matters
    peak = balance
    # Keyed per-asset so one asset's still-open position (never hit SL/TP/
    # max-hold before its own data runs out) can't block entry evaluation
    # for any other asset in the universe.
    open_positions: dict[str, dict | None] = {}
    returns_per_bar: list[float] = []

    for asset in spec.universe_include:
        candles_df = _read_partitions(ledger_dir, "candles_1h", asset)
        funding_df = _read_partitions(ledger_dir, "funding", asset)
        oi_df = _read_partitions(ledger_dir, "oi", asset)

        result.data_window.setdefault("candles_1h", {"rows": 0})
        result.data_window.setdefault("funding", {"rows": 0})
        result.data_window.setdefault("oi", {"rows": 0})
        result.data_window["candles_1h"]["rows"] += len(candles_df)
        result.data_window["funding"]["rows"] += len(funding_df)
        result.data_window["oi"]["rows"] += len(oi_df)

        if candles_df.empty:
            continue

        start_ts = pd.Timestamp(start) if start.tzinfo else pd.Timestamp(start, tz="UTC")
        end_ts = pd.Timestamp(end) if end.tzinfo else pd.Timestamp(end, tz="UTC")
        in_window = candles_df[(candles_df["_ts"] >= start_ts) & (candles_df["_ts"] <= end_ts)]
        bar_timestamps = in_window["_ts"].tolist()

        # Precompute each asset's full history as plain Python lists ONCE
        # (not per bar). A per-bar cutoff index is then found via bisect on
        # the parallel timestamp list and the plain list is sliced directly
        # -- this replaces a fresh pandas boolean-filter + iterrows() (or
        # tolist()) pass over the whole DataFrame on every single bar, which
        # was the dominant cost of run_backtest (see
        # docs/superpowers/reports/2026-07-07-seed-backtest-results.md for
        # the profile that identified this). Semantics are unchanged:
        # candles still cap at the trailing 300 (matching the old
        # `.tail(300)`), funding/OI windows stay unbounded up to bar_ts
        # (matching the old behavior exactly), so results are bit-for-bit
        # identical to the previous implementation, just far fewer
        # per-bar pandas operations.
        candle_ts_list, candle_rows = _candles_to_plain_list(candles_df)
        funding_ts_list, funding_rows = _funding_to_plain_list(funding_df)
        oi_ts_list, oi_val_list = _oi_to_plain_list(oi_df)

        for bar_ts in bar_timestamps:
            candle_cutoff = bisect.bisect_right(candle_ts_list, bar_ts)
            candles = candle_rows[max(0, candle_cutoff - 300):candle_cutoff]
            if len(candles) < MIN_CANDLES_FOR_FEATURES:
                continue

            funding_cutoff = bisect.bisect_right(funding_ts_list, bar_ts)
            funding_history = funding_rows[:funding_cutoff]
            funding_val = funding_history[-1]["fundingRate"] if funding_history else None

            oi_cutoff = bisect.bisect_right(oi_ts_list, bar_ts)
            oi_window = oi_val_list[:oi_cutoff]
            oi_val = oi_window[-1] if oi_window else None
            prior_oi_history = oi_window[:-1] if len(oi_window) > 1 else []

            feature_row = compute_replayable_fields(
                candles, funding_history, oi_val, funding_val, prior_oi_history,
            )
            price = feature_row["price"]
            open_position = open_positions.get(asset)

            if open_position is not None:
                entry = open_position["entry_price"]
                direction = open_position["direction"]
                pct_move = (price - entry) / entry if direction == "long" else (entry - price) / entry
                hit_sl = pct_move <= -spec.stop_loss_pct
                hit_tp = pct_move >= spec.take_profit_pct
                held_hours = (bar_ts - open_position["opened_at"]).total_seconds() / 3600
                timed_out = held_hours >= spec.max_hold_hours
                if hit_sl or hit_tp or timed_out:
                    exit_price = price * (1 - BACKTEST_SLIPPAGE_PCT if direction == "long" else 1 + BACKTEST_SLIPPAGE_PCT)
                    realized_pct_move = (
                        (exit_price - entry) / entry if direction == "long" else (entry - exit_price) / entry
                    )
                    gross_pct = realized_pct_move * spec.leverage
                    net_pct = gross_pct - 2 * taker_fee * spec.leverage
                    pnl_usd = balance * spec.position_size_pct * net_pct
                    balance += pnl_usd
                    peak = max(peak, balance)
                    returns_per_bar.append(net_pct)
                    result.trades.append({
                        "asset": asset, "direction": direction,
                        "entry_price": entry, "exit_price": exit_price,
                        "opened_at": open_position["opened_at"], "closed_at": bar_ts,
                        "pnl_pct": net_pct, "pnl_usd": pnl_usd,
                        "reason": "stop_loss" if hit_sl else ("take_profit" if hit_tp else "max_hold"),
                    })
                    result.equity_curve.append((bar_ts.to_pydatetime(), balance))
                    open_positions[asset] = None
                continue

            decision = evaluate(spec, feature_row)
            if decision["action"] == "enter":
                open_positions[asset] = {
                    "asset": asset, "direction": decision["direction"],
                    "entry_price": price, "opened_at": bar_ts,
                }

    result.total_return_pct = (balance - 10_000.0) / 10_000.0
    if len(returns_per_bar) >= 2:
        mean_r = statistics.mean(returns_per_bar)
        std_r = statistics.stdev(returns_per_bar)
        result.sharpe = (mean_r / std_r) * (len(returns_per_bar) ** 0.5) if std_r > 0 else 0.0
    return result
