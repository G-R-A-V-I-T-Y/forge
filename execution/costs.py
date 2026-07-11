"""execution/costs.py — Shared fee and funding computation.

Single source of truth for cost model used by both live paper trading and
backtesting. Ensures parity between live-paper and backtest cost calculations.

Exchange arithmetic:
  True notional = margin (balance x position_size_pct) x leverage
  Fees = true_notional x taker_fee per side
  Gross PnL (USD) = true_notional x price_move_pct — leverage determines how
    much notional the margin controls; it must not multiply the dollar PnL a
    second time.  Return-on-margin (pnl_pct) = price_move_pct x leverage.
  Funding = true_notional x rate per funding event — a funding rate is a
    fraction of position value, never a per-coin amount.

Every function in this module is a pure function with no I/O.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def compute_true_notional(
    balance: float,
    position_size_pct: float,
    leverage: float | int,
) -> float:
    """Compute the true leveraged notional from margin and leverage.

    True notional = balance x position_size_pct x leverage

    This is the amount on which fees and funding are calculated — matching
    how an exchange computes them.
    """
    return balance * position_size_pct * leverage


def compute_fees(
    true_notional: float,
    taker_fee: float,
    sides: int = 2,
) -> dict[str, float]:
    """Compute entry and exit fees on true notional.

    Args:
        true_notional: margin x leverage (USD)
        taker_fee: fee rate (e.g. 0.00035 for 3.5 bps)
        sides: 1 for entry only, 2 for entry + exit

    Returns:
        dict with entry_fee, exit_fee, total_fees
    """
    entry_fee = true_notional * taker_fee
    exit_fee = true_notional * taker_fee if sides >= 2 else 0.0
    return {
        "entry_fee": entry_fee,
        "exit_fee": exit_fee,
        "total_fees": entry_fee + exit_fee,
    }


def compute_gross_pnl(
    entry_price: float,
    exit_price: float,
    direction: str,
    leverage: float | int,
    true_notional: float,
) -> dict[str, float]:
    """Compute gross PnL before fees and funding.

    pnl_usd = true_notional x price_move — true_notional already contains
    leverage (margin x leverage), so leverage must NOT multiply the dollar
    amount again.  pnl_pct is the levered return on margin
    (price_move x leverage), the number an exchange shows as position ROE.

    Returns:
        dict with pnl_pct (return on margin), pnl_usd (dollar amount)
    """
    if entry_price <= 0:
        return {"pnl_pct": 0.0, "pnl_usd": 0.0}

    if direction == "long":
        price_move = (exit_price - entry_price) / entry_price
    else:
        price_move = (entry_price - exit_price) / entry_price

    pnl_usd = true_notional * price_move
    pnl_pct = price_move * leverage

    return {"pnl_pct": pnl_pct, "pnl_usd": pnl_usd}


def compute_net_pnl(
    gross_pnl_usd: float,
    fees: dict[str, float],
    funding_pnl: float,
) -> dict[str, float]:
    """Compute net PnL after fees and funding.

    Returns:
        dict with net_pnl_usd, net_pnl_pct (relative to true_notional),
        fee_total, funding_pnl
    """
    fee_total = fees.get("total_fees", 0.0)
    net_pnl_usd = gross_pnl_usd - fee_total + funding_pnl
    return {
        "net_pnl_usd": net_pnl_usd,
        "fee_total": fee_total,
        "funding_pnl": funding_pnl,
    }


def compute_funding_pnl(
    true_notional: float,
    direction: str,
    funding_history: list[dict[str, Any]],
    entry_ts_unix: float,
    close_ts_unix: float,
) -> float:
    """Compute net funding PnL between entry and close using funding history.

    funding_history is a list of dicts with {"time": ms_timestamp, "fundingRate": float}.
    Each funding rate is a fraction of position value, so each event pays
    true_notional x rate — computing it on a coin count without the price
    term understates BTC funding by ~the price of BTC.

    Long pays positive funding: long_funding_pnl = -true_notional * rate
    Short receives positive funding: short_funding_pnl = +true_notional * rate

    Returns the total funding PnL (positive = PnL gain, negative = PnL cost).
    """
    if not funding_history or true_notional <= 0:
        return 0.0

    total_payment = 0.0
    samples = 0
    for ev in funding_history:
        rate = ev.get("fundingRate")
        if rate is None:
            continue
        ev_ts_ms = ev.get("time", 0)
        if entry_ts_unix * 1000 <= ev_ts_ms <= close_ts_unix * 1000:
            total_payment += true_notional * rate
            samples += 1

    duration_hours = (close_ts_unix - entry_ts_unix) / 3600 if close_ts_unix > entry_ts_unix else 0

    # If we have suspiciously few samples for the duration, fall back to
    # average rate across all available history.
    if duration_hours > 72 and samples < duration_hours * 0.5:
        all_rates = [
            ev.get("fundingRate", 0.0)
            for ev in funding_history
            if ev.get("fundingRate") is not None
        ]
        if all_rates:
            avg_rate = sum(all_rates) / len(all_rates)
            total_payment = true_notional * avg_rate * duration_hours

    if direction == "long":
        return -total_payment
    else:
        return total_payment


def compute_funding_pnl_simple(
    true_notional: float,
    direction: str,
    avg_funding_rate: float,
    duration_hours: float,
) -> float:
    """Simplified funding PnL for backtesting when we have an average rate.

    Useful when the backtest doesn't have access to the full tick-level
    funding history and uses an interpolated or per-bar average rate instead.
    """
    total_payment = true_notional * avg_funding_rate * duration_hours
    if direction == "long":
        return -total_payment
    else:
        return total_payment


def compute_position_size_in_coins(
    true_notional: float,
    entry_price: float,
) -> float:
    """Compute position size in units of the asset (coins/tokens).

    true_notional / entry_price gives the number of coins the position
    represents, which is the basis for funding accrual.
    """
    if entry_price <= 0:
        return 0.0
    return true_notional / entry_price


def all_costs_from_trade(
    entry_price: float,
    exit_price: float,
    direction: str,
    leverage: float | int,
    true_notional: float,
    taker_fee: float,
    funding_history: list[dict[str, Any]] | None = None,
    entry_ts_unix: float | None = None,
    close_ts_unix: float | None = None,
    sides: int = 2,
) -> dict[str, float]:
    """Convenience: compute all costs for a complete trade in one call.

    Returns a flat dict with:
        true_notional, gross_pnl_pct, gross_pnl_usd,
        entry_fee, exit_fee, total_fees,
        funding_pnl, net_pnl_usd
    """
    fees = compute_fees(true_notional, taker_fee, sides=sides)
    gross = compute_gross_pnl(entry_price, exit_price, direction, leverage, true_notional)

    funding_pnl = 0.0
    if funding_history is not None and entry_ts_unix is not None and close_ts_unix is not None:
        funding_pnl = compute_funding_pnl(
            true_notional, direction, funding_history, entry_ts_unix, close_ts_unix,
        )

    net = compute_net_pnl(gross["pnl_usd"], fees, funding_pnl)

    return {
        "true_notional": true_notional,
        "gross_pnl_pct": gross["pnl_pct"],
        "gross_pnl_usd": gross["pnl_usd"],
        "entry_fee": fees["entry_fee"],
        "exit_fee": fees["exit_fee"],
        "total_fees": fees["total_fees"],
        "funding_pnl": funding_pnl,
        "net_pnl_usd": net["net_pnl_usd"],
    }
