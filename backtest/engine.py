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


def _to_candle_list(df: pd.DataFrame) -> list[list]:
    return [
        [int(row["_ts"].timestamp() * 1000), row["o"], row["h"], row["l"], row["c"], row["v"]]
        for _, row in df.iterrows()
    ]


def _to_funding_history(df: pd.DataFrame) -> list[dict]:
    return [{"time": int(row["_ts"].timestamp() * 1000), "fundingRate": row["rate"]} for _, row in df.iterrows()]


def run_backtest(
    spec: Spec, ledger_dir: Path, start: datetime, end: datetime, taker_fee: float,
) -> BacktestResult:
    result = BacktestResult()
    balance = 10_000.0  # notional backtest starting balance; only relative return matters
    peak = balance
    open_position: dict | None = None
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

        for idx in range(len(in_window)):
            bar_ts = in_window.iloc[idx]["_ts"]
            history_df = candles_df[candles_df["_ts"] <= bar_ts].tail(300)
            if len(history_df) < MIN_CANDLES_FOR_FEATURES:
                continue

            candles = _to_candle_list(history_df)
            funding_window = funding_df[funding_df["_ts"] <= bar_ts]
            funding_history = _to_funding_history(funding_window)
            funding_val = funding_history[-1]["fundingRate"] if funding_history else None

            oi_window = oi_df[oi_df["_ts"] <= bar_ts]["oi"].tolist() if not oi_df.empty else []
            oi_val = oi_window[-1] if oi_window else None
            prior_oi_history = oi_window[:-1] if len(oi_window) > 1 else []

            feature_row = compute_replayable_fields(
                candles, funding_history, oi_val, funding_val, prior_oi_history,
            )
            price = feature_row["price"]

            if open_position is not None and open_position["asset"] == asset:
                entry = open_position["entry_price"]
                direction = open_position["direction"]
                pct_move = (price - entry) / entry if direction == "long" else (entry - price) / entry
                hit_sl = pct_move <= -spec.stop_loss_pct
                hit_tp = pct_move >= spec.take_profit_pct
                held_hours = (bar_ts - open_position["opened_at"]).total_seconds() / 3600
                timed_out = held_hours >= spec.max_hold_hours
                if hit_sl or hit_tp or timed_out:
                    exit_price = price * (1 - BACKTEST_SLIPPAGE_PCT if direction == "long" else 1 + BACKTEST_SLIPPAGE_PCT)
                    gross_pct = pct_move * spec.leverage
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
                    open_position = None
                continue

            if open_position is None:
                decision = evaluate(spec, feature_row)
                if decision["action"] == "enter":
                    open_position = {
                        "asset": asset, "direction": decision["direction"],
                        "entry_price": price, "opened_at": bar_ts,
                    }

    result.total_return_pct = (balance - 10_000.0) / 10_000.0
    if len(returns_per_bar) >= 2:
        mean_r = statistics.mean(returns_per_bar)
        std_r = statistics.stdev(returns_per_bar)
        result.sharpe = (mean_r / std_r) * (len(returns_per_bar) ** 0.5) if std_r > 0 else 0.0
    return result
