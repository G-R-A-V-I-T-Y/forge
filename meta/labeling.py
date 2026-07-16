"""meta/labeling.py — Forward-label every decision against the ledger.

For every ``decisions`` row (enter, wait, close — all agents including
benchmarks) whose timestamp sits at least the longest horizon behind the
ledger head, compute from ``ledger/candles_5m/``:

  - Forward return at 1 h / 4 h / 24 h
  - Max run-up (MFE) and max drawdown (MAE) per horizon
  - Outcome of the chosen action
  - Best-available action (enter_long, enter_short, wait) and regret

The function is designed for nightly APScheduler invocation (cron-compatible
signature ``run_labeling_job(conn, ledger_dir)``) but is also immediately
callable from tests.

Public API
----------
run_labeling_job(conn, ledger_dir) -> dict
    Idempotently label all eligible unlabeled decisions.
    Returns ``{total_processed, total_labeled, errors}``.

get_labeling_coverage(conn) -> dict
    Percentage of labelable decisions that have been labeled.
"""
from __future__ import annotations

import ast
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from store.positions import find_first_cross

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────

#: Horizons to compute labels for.
HORIZONS_HOURS: dict[str, int] = {"1h": 1, "4h": 4, "24h": 24}

#: Longest horizon in hours — a decision must be this old to be labelable.
LONGEST_HOURS: int = max(HORIZONS_HOURS.values())

#: Default SL/TP percentages when decision details don't carry them.
#: Matches ``scripts/build_training_dataset.py``'s DEFAULT_SL_PCT / DEFAULT_TP_PCT.
DEFAULT_SL_PCT: float = 0.02
DEFAULT_TP_PCT: float = 0.05

#: Max allowed gap (ms) between a horizon's cutoff and the nearest available
#: candle before that horizon is left unlabeled rather than computed from a
#: stale candle across a ledger gap. Mirrors
#: ``scripts/build_training_dataset.py``'s STALENESS_THRESHOLD convention:
#: 2x the expected 5-minute candle cadence.
CANDLE_INTERVAL_MS: int = 5 * 60 * 1000
STALENESS_THRESHOLD_MS: int = CANDLE_INTERVAL_MS * 2


# ═════════════════════════════════════════════════════════════════════════
#  Public API
# ═════════════════════════════════════════════════════════════════════════


def run_labeling_job(
    conn,
    ledger_dir: str | Path | None = None,
) -> dict:
    """Forward-label all eligible unlabeled decisions.  Idempotent.

    A decision is *labelable* when its timestamp is at least
    ``LONGEST_HOURS`` behind the ledger head (the latest candle).  Each
    labelable decision that does not yet have rows in ``decision_labels``
    is processed; decisions that already have labels are skipped.

    Parameters
    ----------
    conn : sqlite3.Connection
        Database with ``decisions`` and ``decision_labels`` tables.
    ledger_dir : str | Path | None
        Ledger root.  Defaults to ``store.ledger.LEDGER_DIR``.

    Returns
    -------
    dict
        ``{total_processed, total_labeled, errors}``.
    """
    if ledger_dir is None:
        from store.ledger import LEDGER_DIR

        ledger_dir = LEDGER_DIR

    # 1. Find ledger head (latest candle timestamp across all assets).
    head = _find_ledger_head(ledger_dir)
    if head is None:
        logger.warning("No candle data in ledger — nothing to label")
        return {"total_processed": 0, "total_labeled": 0, "errors": 0}

    # 2. Cutoff = head − longest_horizon.
    cutoff = head - timedelta(hours=LONGEST_HOURS)
    cutoff_iso = cutoff.isoformat().replace("+00:00", "Z")

    # 3. Select all decisions ≤ cutoff that have NO label rows yet.
    #    LEFT JOIN + IS NULL is the idempotent gate.
    rows = conn.execute(
        """SELECT d.id, d.agent_id, d.timestamp, d.decision_action,
                  d.decision_reason, d.decision_details_json
           FROM decisions d
           LEFT JOIN decision_labels dl ON dl.decision_id = d.id
           WHERE d.timestamp <= ?
             AND dl.id IS NULL
           ORDER BY d.timestamp ASC""",
        (cutoff_iso,),
    ).fetchall()

    total_processed = 0
    total_labeled = 0
    errors = 0

    for row in rows:
        decision = dict(row)
        try:
            n = _label_one(conn, decision, ledger_dir, head)
            total_labeled += n
            total_processed += 1
        except Exception as exc:
            logger.warning(
                "Labeling error for decision %s: %s",
                decision["id"],
                exc,
                exc_info=True,
            )
            errors += 1

    return {
        "total_processed": total_processed,
        "total_labeled": total_labeled,
        "errors": errors,
    }


def get_labeling_coverage(conn) -> dict:
    """Compute labeling coverage for decisions ≥ ``LONGEST_HOURS`` old.

    Returns
    -------
    dict
        ``{eligible_decisions, labeled, coverage_pct}``.
    """
    cutoff = (
        datetime.now(timezone.utc) - timedelta(hours=LONGEST_HOURS)
    ).isoformat().replace("+00:00", "Z")

    eligible = conn.execute(
        "SELECT COUNT(*) FROM decisions WHERE timestamp <= ?",
        (cutoff,),
    ).fetchone()[0]

    labeled = conn.execute(
        "SELECT COUNT(DISTINCT decision_id) FROM decision_labels",
    ).fetchone()[0]

    coverage_pct = round(labeled / eligible * 100, 2) if eligible > 0 else 0.0

    return {
        "eligible_decisions": eligible,
        "labeled": labeled,
        "coverage_pct": coverage_pct,
    }


# ═════════════════════════════════════════════════════════════════════════
#  Ledger head
# ═════════════════════════════════════════════════════════════════════════


def _find_ledger_head(ledger_dir: str | Path) -> datetime | None:
    """Return the latest candle timestamp across all assets.

    Reads current and previous month partitions.  Returns ``None`` when
    no candle data exists.
    """
    from store.ledger import read_partition

    now = datetime.now(timezone.utc)
    best: datetime | None = None

    for delta in (0, -1):
        year = now.year + (now.month + delta - 1) // 12
        month = (now.month + delta - 1) % 12 + 1
        dt = datetime(year, month, 1, tzinfo=timezone.utc)

        df = read_partition("candles_5m", dt, ledger_dir)
        if df is None or df.empty:
            continue

        ts_col = _normalise_ts(df)
        if ts_col is None or ts_col.empty:
            continue

        max_ms = ts_col.max()
        if pd.isna(max_ms):
            continue
        candidate = datetime.fromtimestamp(max_ms / 1000, tz=timezone.utc)
        if best is None or candidate > best:
            best = candidate

    return best


def _normalise_ts(df: pd.DataFrame):
    """Normalise the ``ts`` column to numeric epoch-ms, handling ISO strings.

    Mirrors the identical logic in ``store/counterfactuals.py``.
    """
    if "ts" not in df.columns:
        return None
    ts_col = pd.to_numeric(df["ts"], errors="coerce")
    if ts_col.isna().any():
        ts_dt = pd.to_datetime(df["ts"], errors="coerce", utc=True)
        ts_ms = (ts_dt - pd.Timestamp(0, tz="utc")) // pd.Timedelta(milliseconds=1)
        ts_col = ts_col.fillna(ts_ms)
    return ts_col


# ═════════════════════════════════════════════════════════════════════════
#  Decision info extraction
# ═════════════════════════════════════════════════════════════════════════


def _extract_decision_info(
    decision: dict,
    conn,
) -> dict | None:
    """Extract asset, entry_price, sl, tp from a decision row.

    Returns ``{asset, direction, entry_price, sl, tp}`` or ``None``
    when extraction fails (insufficient details, no asset, etc.).
    """
    action = decision["decision_action"]
    details = None
    if decision["decision_details_json"]:
        try:
            details = json.loads(decision["decision_details_json"])
        except (json.JSONDecodeError, TypeError):
            pass

    if action == "enter":
        return _extract_enter_info(details)
    if action == "wait":
        return _extract_wait_info(details)
    if action == "close":
        return _extract_close_info(decision, conn)
    return None


def _extract_enter_info(details: dict | None) -> dict | None:
    """Extract from an enter decision's ``{"order": "<dict repr>", ...}``."""
    if not details:
        return None
    order_str = details.get("order")
    if not order_str:
        return None

    order = _safe_parse_dict(order_str)
    if not order:
        return None

    asset = order.get("asset")
    entry_price = order.get("entry_price")
    if not asset or not isinstance(entry_price, (int, float)) or entry_price <= 0:
        return None

    sl = order.get("stop_loss_price")
    tp = order.get("take_profit_price")

    return {
        "asset": asset,
        "direction": order.get("direction"),
        "entry_price": float(entry_price),
        "sl": float(sl) if isinstance(sl, (int, float)) else None,
        "tp": float(tp) if isinstance(tp, (int, float)) else None,
    }


def _extract_wait_info(details: dict | None) -> dict | None:
    """Extract from a wait decision's ``{"candidate": {...}}`` block."""
    if not details:
        return None
    candidate = details.get("candidate")
    if not candidate or not isinstance(candidate, dict):
        return None

    asset = candidate.get("asset")
    entry_price = candidate.get("entry_price")
    if not asset or not isinstance(entry_price, (int, float)) or entry_price <= 0:
        return None

    sl = candidate.get("stop_loss_price")
    tp = candidate.get("take_profit_price")

    return {
        "asset": asset,
        "direction": candidate.get("direction"),
        "entry_price": float(entry_price),
        "sl": float(sl) if isinstance(sl, (int, float)) else None,
        "tp": float(tp) if isinstance(tp, (int, float)) else None,
    }


def _extract_close_info(decision: dict, conn) -> dict | None:
    """Extract from a close decision by correlating it to its actual trade.

    ``agents/decision_loop.py``'s close branch logs
    ``{"position_id": ..., "fill": "<dict repr>"}`` where ``fill`` is
    ``execute_close()``'s return value and always carries the exact
    ``trade_id`` this decision closed. That is the only reliable
    correlation key — picking "the agent's most-recently-closed trade"
    is wrong whenever the agent has closed more than one trade since,
    which silently mislabels a historical close decision with a
    different trade's asset/direction/exit price. When the trade_id
    can't be recovered (missing/malformed details, or the trade no
    longer exists), the decision is left unlabeled rather than guessed.
    """
    details = None
    if decision["decision_details_json"]:
        try:
            details = json.loads(decision["decision_details_json"])
        except (json.JSONDecodeError, TypeError):
            pass
    if not details:
        return None

    fill = _safe_parse_dict(details.get("fill"))
    trade_id = fill.get("trade_id") if fill else None
    if not trade_id:
        return None

    row = conn.execute(
        """SELECT asset, direction, exit_price,
                  stop_loss_price, take_profit_price
           FROM trades
           WHERE id = ? AND agent_id = ?""",
        (trade_id, decision["agent_id"]),
    ).fetchone()
    if not row:
        return None

    trade = dict(row)
    asset = trade["asset"]
    # Reference price = the exit price (price at close decision point).
    entry_price = trade.get("exit_price")

    if not asset or not isinstance(entry_price, (int, float)) or entry_price <= 0:
        return None

    sl = trade.get("stop_loss_price")
    tp = trade.get("take_profit_price")

    return {
        "asset": asset,
        "direction": trade.get("direction"),
        "entry_price": float(entry_price),
        "sl": float(sl) if isinstance(sl, (int, float)) else None,
        "tp": float(tp) if isinstance(tp, (int, float)) else None,
    }


def _safe_parse_dict(s: str) -> dict | None:
    """Safely parse a Python string repr of a dict (``ast.literal_eval``)."""
    if not isinstance(s, str):
        return None
    try:
        parsed = ast.literal_eval(s)
        if isinstance(parsed, dict):
            return parsed
    except (ValueError, SyntaxError):
        pass
    return None


# ═════════════════════════════════════════════════════════════════════════
#  Candle reading
# ═════════════════════════════════════════════════════════════════════════


def _fetch_forward_candles(
    asset: str,
    start_ts: datetime,
    ledger_dir: str | Path,
) -> list[list] | None:
    """Read 5 m candles for *asset* from *start_ts* forward.

    Reads the start month's partition and the next month to cover the
    full ``LONGEST_HOURS`` window.  Returns ``[ts_ms, o, h, l, c, v]``
    rows sorted by time, or ``None`` when no candles are found.

    Follows the same normalisation + filtering pattern as
    ``store/counterfactuals._read_candle_ledger``.
    """
    from store.ledger import read_partition

    all_candles: list[list] = []

    # Read start month and next month (covers month-boundary crossings).
    months: set[tuple[int, int]] = set()
    months.add((start_ts.year, start_ts.month))
    next_m = start_ts.month % 12 + 1
    next_y = start_ts.year + (1 if start_ts.month == 12 else 0)
    months.add((next_y, next_m))

    start_ms = int(start_ts.timestamp() * 1000)

    for year, month in months:
        dt = datetime(year, month, 1, tzinfo=timezone.utc)
        df = read_partition("candles_5m", dt, ledger_dir)
        if df is None or df.empty:
            continue

        ts_col = _normalise_ts(df)
        if ts_col is None:
            continue

        mask = (df["asset"] == asset) & (ts_col >= start_ms)
        if not mask.any():
            continue

        ts_vals = ts_col[mask].values
        ohlc_df = df.loc[mask, ["o", "h", "l", "c", "v"]]
        for i in range(len(ts_vals)):
            all_candles.append([
                int(ts_vals[i]),
                float(ohlc_df.iloc[i]["o"]),
                float(ohlc_df.iloc[i]["h"]),
                float(ohlc_df.iloc[i]["l"]),
                float(ohlc_df.iloc[i]["c"]),
                float(ohlc_df.iloc[i]["v"]),
            ])

    if not all_candles:
        return None

    # Deduplicate by timestamp and sort.
    seen: set[int] = set()
    unique: list[list] = []
    for c in all_candles:
        ts_int = c[0]
        if ts_int not in seen:
            seen.add(ts_int)
            unique.append(c)
    unique.sort(key=lambda c: c[0])
    return unique


# ═════════════════════════════════════════════════════════════════════════
#  Per-decision labeling
# ═════════════════════════════════════════════════════════════════════════


def _label_one(
    conn,
    decision: dict,
    ledger_dir: str | Path,
    head: datetime,
) -> int:
    """Label one decision across all horizons.  Returns count of labels written."""
    info = _extract_decision_info(decision, conn)
    if info is None:
        return 0

    asset = info["asset"]
    entry_price = info["entry_price"]
    sl = info["sl"]
    tp = info["tp"]
    direction = info.get("direction")

    # Determine the *chosen* action string for outcome comparison.
    action = decision["decision_action"]
    if action == "enter" and direction == "long":
        chosen_action = "enter_long"
    elif action == "enter" and direction == "short":
        chosen_action = "enter_short"
    else:
        # wait, close, or enter with unknown direction → no exposure.
        chosen_action = "wait"

    # Parse decision timestamp.
    try:
        dec_ts = datetime.fromisoformat(
            decision["timestamp"].replace("Z", "+00:00"),
        )
    except (ValueError, TypeError):
        return 0

    # Fetch forward candles from ledger.
    candles = _fetch_forward_candles(asset, dec_ts, ledger_dir)
    if not candles:
        return 0

    entry_ts_unix = dec_ts.timestamp()
    count = 0

    for horizon_label, horizon_hours in HORIZONS_HOURS.items():
        horizon_ts = dec_ts + timedelta(hours=horizon_hours)
        if horizon_ts > head:
            # Not enough forward data — leave this horizon null.
            continue

        label = _compute_horizon_label(
            candles,
            entry_ts_unix,
            entry_price,
            sl,
            tp,
            horizon_hours,
            chosen_action,
        )
        if label is None:
            continue

        now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        conn.execute(
            """INSERT INTO decision_labels
               (decision_id, horizon, fwd_return_pct, max_runup_pct,
                max_drawdown_pct, chosen_outcome_pct, best_action,
                best_outcome_pct, regret_pct, labeled_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                decision["id"],
                horizon_label,
                label["fwd_return_pct"],
                label["max_runup_pct"],
                label["max_drawdown_pct"],
                label["chosen_outcome_pct"],
                label["best_action"],
                label["best_outcome_pct"],
                label["regret_pct"],
                now_iso,
            ),
        )
        count += 1

    if count > 0:
        conn.commit()

    return count


# ═════════════════════════════════════════════════════════════════════════
#  Horizon label computation
# ═════════════════════════════════════════════════════════════════════════


def _compute_horizon_label(
    candles: list[list],
    entry_ts_unix: float,
    entry_price: float,
    sl: float | None,
    tp: float | None,
    horizon_hours: int,
    chosen_action: str,
) -> dict | None:
    """Compute all label fields for one horizon.

    Simulates enter_long, enter_short, and wait; picks the best and
    compares against the chosen action.  Returns ``None`` when candles
    don't cover the horizon.
    """
    horizon_ms = horizon_hours * 3600 * 1000
    entry_ms = entry_ts_unix * 1000
    cutoff_ms = entry_ms + horizon_ms

    # Candles within the horizon window.
    horizon_candles = [c for c in candles if entry_ms <= c[0] <= cutoff_ms]
    if not horizon_candles:
        return None

    # Forward return at horizon.
    fwd_return = _fwd_return_at_cutoff(horizon_candles, entry_price, cutoff_ms)
    if fwd_return is None:
        return None

    # MFE / MAE.
    max_runup, max_drawdown = _mfe_mae(horizon_candles, entry_price)

    # Simulate all three candidate actions through the horizon.
    outcomes: dict[str, float] = {
        "enter_long": _simulate_trade(
            horizon_candles, entry_ts_unix, entry_price, "long", sl, tp,
        ),
        "enter_short": _simulate_trade(
            horizon_candles, entry_ts_unix, entry_price, "short", sl, tp,
        ),
        "wait": 0.0,
    }

    best_action = max(outcomes, key=outcomes.get)
    best_outcome = outcomes[best_action]
    chosen_outcome = outcomes.get(chosen_action, 0.0)

    return {
        "fwd_return_pct": round(fwd_return, 4),
        "max_runup_pct": round(max_runup, 4),
        "max_drawdown_pct": round(max_drawdown, 4),
        "chosen_outcome_pct": round(chosen_outcome, 4),
        "best_action": best_action,
        "best_outcome_pct": round(best_outcome, 4),
        "regret_pct": round(best_outcome - chosen_outcome, 4),
    }


def _fwd_return_at_cutoff(
    horizon_candles: list[list],
    entry_price: float,
    cutoff_ms: float,
) -> float | None:
    """Return the % price change from entry to the candle nearest *cutoff_ms*.

    Returns ``None`` (label left null) when the nearest candle is farther
    than ``STALENESS_THRESHOLD_MS`` from the cutoff — a ledger gap spanning
    the horizon boundary must never be silently bridged with a stale candle.
    """
    if not horizon_candles or entry_price <= 0:
        return None

    best = min(horizon_candles, key=lambda c: abs(c[0] - cutoff_ms))
    if abs(best[0] - cutoff_ms) > STALENESS_THRESHOLD_MS:
        return None
    close = best[4]
    return (close - entry_price) / entry_price * 100


def _mfe_mae(
    horizon_candles: list[list],
    entry_price: float,
) -> tuple[float, float]:
    """Max favorable excursion (run-up) and max adverse excursion (drawdown).

    Both returned as positive percentages relative to *entry_price*.
    """
    if not horizon_candles or entry_price <= 0:
        return 0.0, 0.0

    max_above = 0.0
    max_below = 0.0

    for c in horizon_candles:
        above = (c[2] - entry_price) / entry_price * 100  # high
        below = (entry_price - c[3]) / entry_price * 100  # low
        if above > max_above:
            max_above = above
        if below > max_below:
            max_below = below

    return max_above, max_below


def _simulate_trade(
    horizon_candles: list[list],
    entry_ts_unix: float,
    entry_price: float,
    direction: str,
    sl: float | None,
    tp: float | None,
) -> float:
    """Simulate an enter_long or enter_short trade through the horizon.

    Uses ``find_first_cross`` for SL/TP detection (matching
    ``store/positions.py`` semantics: SL takes priority within the
    same candle).  Returns PnL % (positive = profitable).  If neither
    SL nor TP is hit, returns the forward return at the last candle.

    When *sl* or *tp* is ``None``, falls back to desk-level defaults
    (``DEFAULT_SL_PCT`` / ``DEFAULT_TP_PCT``).
    """
    if sl is None:
        sl = (
            entry_price * (1 - DEFAULT_SL_PCT)
            if direction == "long"
            else entry_price * (1 + DEFAULT_SL_PCT)
        )
    if tp is None:
        tp = (
            entry_price * (1 + DEFAULT_TP_PCT)
            if direction == "long"
            else entry_price * (1 - DEFAULT_TP_PCT)
        )

    cross = find_first_cross(horizon_candles, entry_ts_unix, direction, sl, tp)

    if cross is not None:
        exit_price, _reason = cross
        if direction == "long":
            return (exit_price - entry_price) / entry_price * 100
        return (entry_price - exit_price) / entry_price * 100

    # No SL/TP hit — use the last candle's close.
    last_close = horizon_candles[-1][4]
    if direction == "long":
        return (last_close - entry_price) / entry_price * 100
    return (entry_price - last_close) / entry_price * 100
