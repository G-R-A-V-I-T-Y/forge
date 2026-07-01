"""
risk/gate.py — stateless order validation.

Raises RiskViolation on any rule breach; returns None on pass.
No DB calls, no I/O, no imports from other forge modules.
"""

MIN_SL_DISTANCE_PCT = 0.003   # 0.3% minimum stop-loss distance from entry


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

    Raises
    ------
    RiskViolation
        On any rule breach; the `.reason` attribute describes which rule
        failed and why.
    """

    # Rule 1: stop_loss_price must be present and non-None
    if "stop_loss_price" not in order or order.get("stop_loss_price") is None:
        raise RiskViolation("stop_loss_price is required")

    entry = order["entry_price"]
    sl = order["stop_loss_price"]
    direction = order["direction"]
    leverage = order["leverage"]
    size_pct = order["position_size_pct"]

    # Rule 2: SL distance must be >= 0.3% from entry
    sl_dist = abs(entry - sl) / entry
    if sl_dist < MIN_SL_DISTANCE_PCT:
        raise RiskViolation(
            f"stop loss distance {sl_dist:.4%} is below minimum {MIN_SL_DISTANCE_PCT:.4%}"
        )

    # Rule 3: leverage cap
    if leverage > config["max_leverage"]:
        raise RiskViolation(
            f"leverage {leverage}x exceeds max {config['max_leverage']}x"
        )

    # Rule 4: position size cap
    if size_pct > config["max_position_size_pct"]:
        raise RiskViolation(
            f"position size {size_pct:.0%} exceeds max {config['max_position_size_pct']:.0%}"
        )

    # Rule 5: concurrent positions cap
    if open_position_count >= config["max_concurrent_positions"]:
        raise RiskViolation(
            f"concurrent positions {open_position_count} at max {config['max_concurrent_positions']}"
        )

    # Rule 6: liquidation distance must be >= 2x SL distance
    #   Long:  liq_price = entry * (1 - 1/leverage)
    #   Short: liq_price = entry * (1 + 1/leverage)
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

    # Rule 7: confidence floor (optional key, but if present must be >= 0.50)
    if "confidence" in order and order["confidence"] < 0.50:
        raise RiskViolation(
            f"confidence {order['confidence']} is below firm minimum 0.50"
        )
