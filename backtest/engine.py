"""backtest/engine.py -- replay historical ledger data through the
interpreter, using the exact same feature-computation core the live
heartbeat uses (market.heartbeat.compute_replayable_fields).

Reads candles_5m (not candles_1h) for exactly this reason: every function in
market/features.py and market/heartbeat.py's replayable core is written and
documented against live's 300 x 5m-candle / 25h window (LOOKBACK_CANDLES,
LOOKBACK_HOURS in market/heartbeat.py) -- RSI/EMA/ATR periods, the
return_5m/30m/4h/24h candle-count offsets, momentum_acceleration's per-period
normalizers, and realized_vol's annualization constant (PERIODS_PER_YEAR_5M)
all assume 5-minute bars. Feeding this same code hourly candles doesn't
degrade gracefully -- it silently recomputes every one of those features over
a ~12x-longer, differently-labeled window than live ever produces (e.g.
"return_24h" becomes a 12-day return), which is a live/backtest parity bug,
not a preserved behavior. candles_1h remains in the ledger for other
consumers; the backtest engine no longer reads it.

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
from market.heartbeat import FUNDING_LOOKBACK_HOURS, LOOKBACK_CANDLES, compute_replayable_fields

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


def _read_events_partitions(ledger_dir: Path, asset: str) -> list[dict]:
    """Read every monthly events partition for one asset, sorted by
    scheduled_time. Unlike candles/funding/oi, events are keyed by
    scheduled_time, not ts -- so this can't reuse _read_partitions() above
    (which hard-codes the "ts" column name). Token unlocks and macro prints
    are scheduled well in advance and published to the ledger long before
    they occur, so loading the whole events history up front (rather than
    windowing to [start, end] like the candle/funding/OI readers) is not a
    lookahead-bias risk: the per-bar cutoff in run_backtest() only ever
    looks at events with scheduled_time >= the current bar_ts, exactly
    mirroring what a live heartbeat cycle at that same wall-clock moment
    would have seen."""
    events_dir = ledger_dir / "events"
    if not events_dir.exists():
        return []
    frames = [pd.read_parquet(p) for p in sorted(events_dir.glob("*.parquet"))]
    frames += [pd.read_json(p, lines=True) for p in sorted(events_dir.glob("*.jsonl"))]
    if not frames:
        return []
    df = pd.concat(frames, ignore_index=True)
    if "asset" in df.columns:
        df = df[df["asset"] == asset]
    if df.empty or "scheduled_time" not in df.columns:
        return []
    df = df.copy()
    df["_scheduled_dt"] = pd.to_datetime(df["scheduled_time"], utc=True)
    df = df.sort_values("_scheduled_dt").reset_index(drop=True)
    return df.to_dict("records")


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
        candles_df = _read_partitions(ledger_dir, "candles_5m", asset)
        funding_df = _read_partitions(ledger_dir, "funding", asset)
        oi_df = _read_partitions(ledger_dir, "oi", asset)

        result.data_window.setdefault("candles_5m", {"rows": 0})
        result.data_window.setdefault("funding", {"rows": 0})
        result.data_window.setdefault("oi", {"rows": 0})
        result.data_window["candles_5m"]["rows"] += len(candles_df)
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
        # the profile that identified this). Candles cap at the trailing
        # LOOKBACK_CANDLES (300), same constant live's _fetch_asset_snapshot
        # uses -- since this loop now reads candles_5m (not candles_1h), that
        # cap represents the same ~25h window live computes over, not just
        # the same bar count. The funding window is capped to
        # FUNDING_LOOKBACK_HOURS (see below) -- the earlier unbounded version
        # was a live/backtest parity bug, not a preserved behavior: live's
        # _fetch_asset_snapshot has fetched only a 14-day funding window
        # since the Task-1 fix, so an unbounded multi-month window here
        # silently fed compute_replayable_fields's funding_zscore a
        # different population than live ever computes against. OI stays
        # unbounded up to bar_ts since no equivalent live lookback constant
        # exists for it yet.
        candle_ts_list, candle_rows = _candles_to_plain_list(candles_df)
        funding_ts_list, funding_rows = _funding_to_plain_list(funding_df)
        oi_ts_list, oi_val_list = _oi_to_plain_list(oi_df)
        funding_lookback = pd.Timedelta(hours=FUNDING_LOOKBACK_HOURS)

        # Event calendar (days_to_event / unlock_size_pct -- market/features.py).
        # Loaded once per asset, outside the per-bar loop, same pattern as the
        # candle/funding/OI series above.
        asset_raw_events = _read_events_partitions(ledger_dir, asset)
        event_ts_list = [e["_scheduled_dt"] for e in asset_raw_events]

        for bar_ts in bar_timestamps:
            candle_cutoff = bisect.bisect_right(candle_ts_list, bar_ts)
            candles = candle_rows[max(0, candle_cutoff - LOOKBACK_CANDLES):candle_cutoff]
            if len(candles) < MIN_CANDLES_FOR_FEATURES:
                continue

            funding_cutoff = bisect.bisect_right(funding_ts_list, bar_ts)
            funding_window_start = bisect.bisect_left(funding_ts_list, bar_ts - funding_lookback)
            funding_history = funding_rows[funding_window_start:funding_cutoff]
            funding_val = funding_history[-1]["fundingRate"] if funding_history else None

            oi_cutoff = bisect.bisect_right(oi_ts_list, bar_ts)
            oi_window = oi_val_list[:oi_cutoff]
            oi_val = oi_window[-1] if oi_window else None
            prior_oi_history = oi_window[:-1] if len(oi_window) > 1 else []

            # Only events not-yet-occurred as of this bar are visible --
            # bisect_left finds the first event with scheduled_time >= bar_ts.
            event_cutoff = bisect.bisect_left(event_ts_list, bar_ts)
            upcoming_events = asset_raw_events[event_cutoff:]

            feature_row = compute_replayable_fields(
                candles, funding_history, oi_val, funding_val, prior_oi_history,
                asset_events=upcoming_events, event_as_of=bar_ts,
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
                # Deliberately deferred, not an oversight: the interpreter
                # distinguishes a "scaled" entry (confidence between
                # scale_threshold and confidence_threshold) from a full-size
                # one only via decision["reason"]'s text (backtest/interpreter.py),
                # not a structured field. Every backtest entry here opens at
                # full spec.position_size_pct regardless of that distinction,
                # so scaled-conviction sizing is not yet reflected in P&L or
                # Sharpe. Implementing it needs a structured signal from the
                # interpreter (e.g. a numeric size multiplier), which is out
                # of scope for M7b -- tracked as follow-up before any spec's
                # backtest results are used to justify scaled position sizing
                # in a live/paper deployment (M8+).
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
