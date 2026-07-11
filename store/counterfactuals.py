"""store/counterfactuals.py — Deterministic counterfactual replay engine.

Replays unfilled wait decisions through recorded 5m candles to compute what
would have happened if the trade had been taken.  No LLM calls — pure price
replay via the same first-cross semantics as ``store.positions.find_first_cross``.

Public API
----------
run_counterfactual_replay(conn, config, ledger_dir) -> dict
    Select all eligible unfilled waits, replay each one, write results.
    Returns a summary dict with counts.

get_counterfactual_coverage(conn) -> dict
    Compute coverage metric: % of waits >= 24h old that have a filled
    counterfactual_result.

_replay_one(conn, wait_row, candles_df, config, ledger_dir) -> dict | None
    Internal: replay a single wait decision and return the outcome dict
    (or None if insufficient forward data).
"""
from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from store.positions import find_first_cross

logger = logging.getLogger(__name__)

#: Minimum age (hours) for a wait to be eligible for replay.
MIN_WAIT_AGE_HOURS = 2

#: Default lookback window (months) for candle data.
CANDLE_LOOKBACK_MONTHS = 3


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_counterfactual_replay(
    conn,
    config: dict,
    ledger_dir: str | Path | None = None,
) -> dict:
    """Select all eligible unfilled wait decisions, replay each one, write
    counterfactual results.

    Parameters
    ----------
    conn : sqlite3.Connection
        Database connection with the ``decisions`` table.
    config : dict
        Desk config (used for default SL/TP when details are missing).
    ledger_dir : str | Path | None
        Path to the ledger directory.  Defaults to ``store.ledger.LEDGER_DIR``.

    Returns
    -------
    dict
        Summary: ``{total_queued, processed, filled, errors}``.
    """
    if ledger_dir is None:
        from store.ledger import LEDGER_DIR

        ledger_dir = LEDGER_DIR

    # 1. Select all unfilled waits >= MIN_WAIT_AGE_HOURS old.
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=MIN_WAIT_AGE_HOURS)).isoformat().replace("+00:00", "Z")

    rows = conn.execute(
        """SELECT id, agent_id, timestamp, decision_action,
                  decision_reason, decision_details_json
           FROM decisions
           WHERE decision_action = 'wait'
             AND counterfactual_result IS NULL
             AND timestamp <= ?
           ORDER BY timestamp ASC""",
        (cutoff,),
    ).fetchall()

    total_queued = len(rows)
    processed = 0
    filled = 0
    errors = 0

    for row in rows:
        wait = dict(row)
        try:
            outcome = _replay_one(conn, wait, ledger_dir)
            if outcome is not None:
                conn.execute(
                    """UPDATE decisions
                       SET counterfactual_result = ?, counterfactual_was_better = ?
                       WHERE id = ?""",
                    (
                        json.dumps(outcome),
                        1 if outcome.get("profitable", False) else 0,
                        wait["id"],
                    ),
                )
                conn.commit()
                filled += 1
            processed += 1
        except Exception as exc:
            logger.warning(
                "Counterfactual replay error for decision %s: %s",
                wait["id"],
                exc,
                exc_info=True,
            )
            errors += 1

    return {
        "total_queued": total_queued,
        "processed": processed,
        "filled": filled,
        "errors": errors,
    }


def get_counterfactual_coverage(conn) -> dict:
    """Compute counterfactual coverage for waits >= 24h old.

    Returns
    -------
    dict
        ``{total_waits, eligible_waits, filled, coverage_pct}``.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat().replace("+00:00", "Z")

    total_waits = conn.execute(
        """SELECT COUNT(*) FROM decisions
           WHERE decision_action = 'wait'
             AND timestamp <= ?""",
        (cutoff,),
    ).fetchone()[0]

    eligible_waits = conn.execute(
        """SELECT COUNT(*) FROM decisions
           WHERE decision_action = 'wait'
             AND timestamp <= ?""",
        (cutoff,),
    ).fetchone()[0]

    filled = conn.execute(
        """SELECT COUNT(*) FROM decisions
           WHERE decision_action = 'wait'
             AND counterfactual_result IS NOT NULL
             AND timestamp <= ?""",
        (cutoff,),
    ).fetchone()[0]

    coverage_pct = round(filled / eligible_waits * 100, 2) if eligible_waits > 0 else 0.0

    return {
        "total_waits": total_waits,
        "eligible_waits": eligible_waits,
        "filled": filled,
        "coverage_pct": coverage_pct,
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _replay_one(
    conn,
    wait_row: dict,
    ledger_dir: str | Path,
) -> dict | None:
    """Replay a single wait decision through candles.

    Returns an outcome dict or None if insufficient forward data.
    """
    details = json.loads(wait_row["decision_details_json"]) if wait_row["decision_details_json"] else {}

    # Extract candidate info from decision details.
    # For compiled agents: details has a "candidate" key.
    # For LLM agents: details may have direct fields or be empty.
    candidate = details.get("candidate") if isinstance(details, dict) else None

    if candidate is None:
        # Try to extract from top-level details.
        asset = details.get("asset")
        direction = details.get("direction")
        entry_price = details.get("entry_price")
        sl = details.get("stop_loss_price")
        tp = details.get("take_profit_price")
    else:
        asset = candidate.get("asset")
        direction = candidate.get("direction")
        entry_price = candidate.get("entry_price")
        sl = candidate.get("stop_loss_price")
        tp = candidate.get("take_profit_price")

    if not all([asset, direction, entry_price, sl is not None, tp is not None]):
        # Insufficient candidate data — skip this wait.
        logger.debug(
            "Skipping wait %s: insufficient candidate data (asset=%s, dir=%s, entry=%s, sl=%s, tp=%s)",
            wait_row["id"], asset, direction, entry_price, sl, tp,
        )
        return None

    # Parse wait timestamp.
    try:
        wait_ts = datetime.fromisoformat(wait_row["timestamp"].replace("Z", "+00:00"))
        entry_ts = int(wait_ts.timestamp())
    except (ValueError, TypeError):
        return None

    # Fetch 5m candles for the asset covering the wait period.
    candles = _fetch_candles(conn, asset, wait_ts, ledger_dir)
    if candles is None or len(candles) == 0:
        return None

    # Determine max hold period.
    max_hold_hours = candidate.get("max_hold_hours", 48) if candidate else 48

    # Run the replay.
    cross = find_first_cross(candles, entry_ts, direction, sl, tp)

    if cross:
        exit_price, reason = cross
        # Calculate PnL.
        if direction == "long":
            pnl_pct = (exit_price - entry_price) / entry_price
        else:
            pnl_pct = (entry_price - exit_price) / entry_price
        profitable = pnl_pct > 0

        return {
            "exit_price": exit_price,
            "reason": reason,
            "pnl_pct": round(pnl_pct * 100, 4),
            "profitable": profitable,
            "max_hold_hours": max_hold_hours,
        }

    # No SL/TP hit — check if we have enough forward data to determine
    # the trade is still open vs. just missing data.
    if not _has_sufficient_forward_data(candles, entry_ts, max_hold_hours):
        # Not enough data — leave null (don't guess).
        return None

    # We have enough data but no cross — compute PnL at last candle.
    last_candle = candles[-1]
    last_price = last_candle[4]  # close

    if direction == "long":
        pnl_pct = (last_price - entry_price) / entry_price
    else:
        pnl_pct = (entry_price - last_price) / entry_price

    profitable = pnl_pct > 0

    return {
        "exit_price": last_price,
        "reason": "max_hold_timeout",
        "pnl_pct": round(pnl_pct * 100, 4),
        "profitable": profitable,
        "max_hold_hours": max_hold_hours,
    }


def _fetch_candles(
    conn,
    asset: str,
    wait_ts: datetime,
    ledger_dir: str | Path,
) -> list[list] | None:
    """Fetch 5m candles for *asset* starting from *wait_ts*.

    Tries ledger partitions first, then falls back to the heartbeat file.
    Returns a list of [ts_ms, open, high, low, close, volume] or None.
    """
    # Try ledger first.
    candles = _read_candle_ledger(asset, wait_ts, ledger_dir)
    if candles and len(candles) > 0:
        return candles

    # Fallback: try heartbeat file.
    candles = _read_candle_heartbeat(asset, wait_ts)
    return candles


def _read_candle_ledger(
    asset: str,
    start_ts: datetime,
    ledger_dir: str | Path,
) -> list[list] | None:
    """Read candles_5m ledger partition for the month of *start_ts*.

    Returns filtered candles >= start_ts, or None.
    """
    from store.ledger import read_partition

    candles_df = read_partition("candles_5m", start_ts, ledger_dir)
    if candles_df is None or candles_df.empty:
        return None

    # Normalise ts to epoch-milliseconds.  The production ledger
    # (market/heartbeat.py's export_heartbeat_to_ledger) writes ts as an
    # ISO string ("2026-07-11T17:43:58Z"); older/synthetic partitions may
    # carry numeric epoch-ms.  pd.to_numeric alone maps ISO strings to NaN,
    # which silently empties the ledger path and every wait falls through
    # to the 25h heartbeat fallback.
    ts_col = pd.to_numeric(candles_df["ts"], errors="coerce")
    if ts_col.isna().any():
        ts_dt = pd.to_datetime(candles_df["ts"], errors="coerce", utc=True)
        ts_ms = (ts_dt - pd.Timestamp(0, tz="utc")) // pd.Timedelta(milliseconds=1)
        ts_col = ts_col.fillna(ts_ms)

    start_ms = int(start_ts.timestamp() * 1000)
    mask = (candles_df["asset"] == asset) & (ts_col >= start_ms)
    subset = candles_df.loc[mask, ["ts", "o", "h", "l", "c", "v"]].copy()

    if subset.empty:
        return None

    # Return numeric ts — find_first_cross compares c[0] arithmetically.
    subset["ts"] = ts_col[mask]
    return [row.tolist() for row in subset.values]


def _read_candle_heartbeat(
    asset: str,
    wait_ts: datetime,
) -> list[list] | None:
    """Read 5m candles from the heartbeat file for *asset*.

    Returns candles >= wait_ts, or None.
    """
    from market.heartbeat import read_heartbeat_or_none

    heartbeat = read_heartbeat_or_none(
        "data/heartbeat.json",
        max_age_seconds=3600 * 25,  # 25h heartbeat window
    )
    if heartbeat is None:
        return None

    asset_data = (heartbeat.get("assets") or {}).get(asset)
    if not asset_data:
        return None

    candles_5m = list(asset_data.get("candles_5m") or [])
    wait_ts_ms = int(wait_ts.timestamp() * 1000)

    # Filter to candles >= wait_ts.
    filtered = [c for c in candles_5m if c[0] >= wait_ts_ms]
    return filtered if filtered else None


def _has_sufficient_forward_data(candles: list, entry_ts: int, max_hold_hours: int) -> bool:
    """Check if candles extend far enough to confirm no SL/TP hit.

    We need at least *max_hold_hours* of candle data after entry to be
    confident the trade didn't hit SL/TP before data ran out.
    """
    if not candles:
        return False

    entry_ts_ms = entry_ts * 1000
    min_ts = entry_ts_ms
    max_ts = max(c[0] for c in candles)

    ms_per_hour = 60 * 60 * 1000
    hours_covered = (max_ts - min_ts) / ms_per_hour
    return hours_covered >= max_hold_hours
