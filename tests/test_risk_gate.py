import pytest
from risk.gate import RiskViolation, validate_order

CONFIG = {
    "max_leverage": 10,
    "max_position_size_pct": 0.20,
    "max_concurrent_positions": 3,
}
BALANCE = 50000.0

VALID_ORDER = {
    "asset": "SOL-PERP",
    "direction": "long",
    "entry_price": 145.20,
    "stop_loss_price": 143.00,
    "take_profit_price": 152.00,
    "leverage": 3,
    "position_size_pct": 0.10,
}


def test_valid_order_passes():
    validate_order(VALID_ORDER, BALANCE, CONFIG, open_position_count=0)


def test_missing_stop_loss_raises():
    order = {**VALID_ORDER}
    del order["stop_loss_price"]
    with pytest.raises(RiskViolation) as exc:
        validate_order(order, BALANCE, CONFIG, open_position_count=0)
    assert "stop_loss" in exc.value.reason.lower()


def test_stop_loss_too_close_raises():
    # SL must be >= 0.3% from entry; 0.1% is too close
    entry = 145.20
    sl = entry * (1 - 0.001)  # 0.1% below entry
    order = {**VALID_ORDER, "entry_price": entry, "stop_loss_price": sl}
    with pytest.raises(RiskViolation) as exc:
        validate_order(order, BALANCE, CONFIG, open_position_count=0)
    assert "stop loss distance" in exc.value.reason.lower()


def test_leverage_over_cap_raises():
    order = {**VALID_ORDER, "leverage": 11}
    with pytest.raises(RiskViolation) as exc:
        validate_order(order, BALANCE, CONFIG, open_position_count=0)
    assert "leverage" in exc.value.reason.lower()


def test_position_size_over_cap_raises():
    order = {**VALID_ORDER, "position_size_pct": 0.25}
    with pytest.raises(RiskViolation) as exc:
        validate_order(order, BALANCE, CONFIG, open_position_count=0)
    assert "position size" in exc.value.reason.lower()


def test_too_many_open_positions_raises():
    with pytest.raises(RiskViolation) as exc:
        validate_order(VALID_ORDER, BALANCE, CONFIG, open_position_count=3)
    assert "concurrent positions" in exc.value.reason.lower()


def test_liquidation_price_too_close_raises():
    # Liquidation for 10x long ≈ entry * (1 - 1/leverage)
    # entry=145.20, leverage=10 → liq ≈ 130.68 (distance = 14.52)
    # SL at 143.00 → SL distance = 2.20
    # Requirement: liq must be >= 2x SL distance from entry
    # 14.52 >= 2 * 2.20 = 4.40  → this passes.
    # To fail: leverage=10, SL very close to liq
    entry = 145.20
    sl = entry * (1 - 0.06)   # 6% below entry — SL distance = 8.71
    # liq at 10x = 145.20 * (1 - 0.1) = 130.68 → liq distance = 14.52
    # need liq_dist >= 2 * sl_dist → 14.52 >= 17.42 → FAILS
    order = {**VALID_ORDER, "entry_price": entry, "stop_loss_price": sl, "leverage": 10}
    with pytest.raises(RiskViolation) as exc:
        validate_order(order, BALANCE, CONFIG, open_position_count=0)
    assert "liquidation" in exc.value.reason.lower()
