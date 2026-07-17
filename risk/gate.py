"""
risk/gate.py — stateless order validation.

Raises RiskViolation on any rule breach; returns None on pass.
No DB calls, no I/O, no imports from other forge modules.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from meta.risk_officer import RiskOfficerOutput

logger = logging.getLogger(__name__)

# Minimum stop-loss distance from entry (0.3%)
MIN_SL_DISTANCE_PCT = 0.003

# Maximum allowed notional exposure: position_size_pct * leverage.
# This caps the total leveraged notional relative to account size.
# E.g. position_size_pct=0.20 and leverage=10 → exposure = 2.0 (200% of account).
MAX_NOTIONAL_EXPOSURE = 2.0

# Maximum entry price deviation from heartbeat price (0.5%).
# Prevents orders filled at a wildly stale price.
MAX_ENTRY_PRICE_DEVIATION_PCT = 0.005

# Minimum reward-to-risk ratio (0.5 = reward must be at least half of risk).
MIN_REWARD_TO_RISK = 0.5

# Minimum take-profit distance (in pct) to clear the fee hurdle.
# With taker_fee=0.00035 (0.035%) round-trip = 0.07%, we need TP distance
# to be meaningfully larger than the fee cost.  0.5% is a safe floor.
MIN_TP_DISTANCE_PCT = 0.005


class RiskViolation(Exception):
    """Raised when an order violates a risk rule."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def validate_order(
    order: dict,
    account_balance: float,
    config: dict,
    open_position_count: int,
    heartbeat_price: float | None = None,
    *,
    agent_id: str | None = None,
    risk_officer_output: RiskOfficerOutput | None = None,
) -> None:
    """Validate an order against hard risk rules.

    Parameters
    ----------
    order:
        Dict with keys: asset, direction, entry_price, stop_loss_price,
        take_profit_price, leverage, position_size_pct.
    account_balance:
        Current account equity in USD.
    config:
        Dict with keys: max_leverage, max_position_size_pct,
        max_concurrent_positions.
    open_position_count:
        Number of positions currently open (not including this new order).
    heartbeat_price:
        Current heartbeat price for the order's asset. Used to validate
        that the entry price is within tolerance of the live market price.
        If None, this check is skipped.

    Raises
    ------
    RiskViolation
        On any rule breach; the `.reason` attribute describes which rule
        failed and why.
    """

    if risk_officer_output is not None and agent_id is not None:
        if agent_id in risk_officer_output.entry_disabled_agents:
            logger.warning(
                "Risk gate blocked entry for %s: %s",
                agent_id, risk_officer_output.reason,
            )
            raise RiskViolation(
                f"risk officer disabled entries for {agent_id}: "
                f"{risk_officer_output.reason}"
            )

    # ------------------------------------------------------------------
    # Rule 1: stop_loss_price must be present and non-None
    # ------------------------------------------------------------------
    if "stop_loss_price" not in order or order.get("stop_loss_price") is None:
        raise RiskViolation("stop_loss_price is required")

    # ------------------------------------------------------------------
    # Rule 2: take_profit_price must be present and non-None
    # ------------------------------------------------------------------
    if "take_profit_price" not in order or order.get("take_profit_price") is None:
        raise RiskViolation("take_profit_price is required")

    entry = order["entry_price"]
    sl = order["stop_loss_price"]
    tp = order["take_profit_price"]
    direction = order["direction"]
    leverage = order["leverage"]
    size_pct = order["position_size_pct"]

    # ------------------------------------------------------------------
    # Rule 3: SL/TP geometry — must bracket the entry price.
    #   Long:  SL < entry < TP
    #   Short: TP < entry < SL
    # ------------------------------------------------------------------
    if direction == "long":
        if not (sl < entry < tp):
            raise RiskViolation(
                f"SL/TP geometry invalid for long: need SL({sl}) < entry({entry}) < TP({tp})"
            )
    elif direction == "short":
        if not (tp < entry < sl):
            raise RiskViolation(
                f"SL/TP geometry invalid for short: need TP({tp}) < entry({entry}) < SL({sl})"
            )
    else:
        raise RiskViolation(f"unknown direction {direction!r}")

    # ------------------------------------------------------------------
    # Rule 4: SL distance must be >= 0.3% from entry
    # ------------------------------------------------------------------
    sl_dist = abs(entry - sl) / entry
    if sl_dist < MIN_SL_DISTANCE_PCT:
        raise RiskViolation(
            f"stop loss distance {sl_dist:.4%} is below minimum {MIN_SL_DISTANCE_PCT:.4%}"
        )

    # ------------------------------------------------------------------
    # Rule 5: TP distance must be >= 0.5% (fee-hurdle floor)
    # ------------------------------------------------------------------
    tp_dist = abs(tp - entry) / entry
    if tp_dist < MIN_TP_DISTANCE_PCT:
        raise RiskViolation(
            f"take profit distance {tp_dist:.4%} is below minimum {MIN_TP_DISTANCE_PCT:.4%} "
            f"(must clear fee hurdle)"
        )

    # ------------------------------------------------------------------
    # Rule 6: reward:risk >= 0.5
    # ------------------------------------------------------------------
    reward = abs(tp - entry) / entry
    risk = abs(entry - sl) / entry
    if risk > 0 and (reward / risk) < MIN_REWARD_TO_RISK:
        raise RiskViolation(
            f"reward:risk ratio {reward/risk:.2f} is below minimum {MIN_REWARD_TO_RISK}"
        )

    # ------------------------------------------------------------------
    # Rule 7: leverage cap
    # ------------------------------------------------------------------
    if leverage > config["max_leverage"]:
        raise RiskViolation(
            f"leverage {leverage}x exceeds max {config['max_leverage']}x"
        )

    # ------------------------------------------------------------------
    # Rule 8: position size cap
    # ------------------------------------------------------------------
    if size_pct > config["max_position_size_pct"]:
        raise RiskViolation(
            f"position size {size_pct:.0%} exceeds max {config['max_position_size_pct']:.0%}"
        )

    # ------------------------------------------------------------------
    # Rule 9: notional exposure cap (position_size_pct * leverage).
    #   This is the true risk metric — not the two factors separately.
    #   E.g. 20% size * 10x leverage = 200% notional exposure.
    # ------------------------------------------------------------------
    notional_exposure = size_pct * leverage
    if notional_exposure > MAX_NOTIONAL_EXPOSURE:
        raise RiskViolation(
            f"notional exposure {notional_exposure:.2f} (size {size_pct:.0%} × leverage {leverage}x) "
            f"exceeds max {MAX_NOTIONAL_EXPOSURE:.2f}"
        )

    # ------------------------------------------------------------------
    # Rule 10: concurrent positions cap
    # ------------------------------------------------------------------
    if open_position_count >= config["max_concurrent_positions"]:
        raise RiskViolation(
            f"concurrent positions {open_position_count} at max {config['max_concurrent_positions']}"
        )

    # ------------------------------------------------------------------
    # Rule 11: entry price must be within ~0.5% of heartbeat price
    # ------------------------------------------------------------------
    if heartbeat_price is not None and heartbeat_price > 0:
        deviation = abs(entry - heartbeat_price) / heartbeat_price
        if deviation > MAX_ENTRY_PRICE_DEVIATION_PCT:
            raise RiskViolation(
                f"entry price {entry} deviates {deviation:.4%} from heartbeat price {heartbeat_price} "
                f"(max allowed {MAX_ENTRY_PRICE_DEVIATION_PCT:.4%})"
            )

    # ------------------------------------------------------------------
    # Rule 12: liquidation distance must be >= 2x SL distance
    # ------------------------------------------------------------------
    if direction == "long":
        liq_price = entry * (1 - 1 / leverage)
        liq_dist = (entry - liq_price) / entry
    else:
        liq_price = entry * (1 + 1 / leverage)
        liq_dist = (liq_price - entry) / entry

    if liq_dist < 2 * sl_dist:
        raise RiskViolation(
            f"liquidation distance {liq_dist:.4%} must be >= 2x stop loss distance {sl_dist:.4%}"
        )

    # ------------------------------------------------------------------
    # Rule 13: confidence floor (optional key, but if present must be >= 0.50)
    # ------------------------------------------------------------------
    if "confidence" in order and order["confidence"] < 0.50:
        raise RiskViolation(
            f"confidence {order['confidence']} is below firm minimum 0.50"
        )
