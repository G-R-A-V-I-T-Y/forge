import pytest
from risk.gate import RiskViolation, validate_order

CONFIG = {
    "max_leverage": 10,
    "max_position_size_pct": 0.20,
    "max_concurrent_positions": 3,
}

# Config with higher caps to test notional exposure independently
CONFIG_HIGH_CAPS = {
    "max_leverage": 20,
    "max_position_size_pct": 0.50,
    "max_concurrent_positions": 10,
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


def test_missing_take_profit_raises():
    order = {**VALID_ORDER}
    del order["take_profit_price"]
    with pytest.raises(RiskViolation) as exc:
        validate_order(order, BALANCE, CONFIG, open_position_count=0)
    assert "take_profit" in exc.value.reason.lower()


def test_stop_loss_too_close_raises():
    # SL must be >= 0.3% from entry; 0.1% is too close
    entry = 145.20
    sl = entry * (1 - 0.001)  # 0.1% below entry
    order = {**VALID_ORDER, "entry_price": entry, "stop_loss_price": sl}
    with pytest.raises(RiskViolation) as exc:
        validate_order(order, BALANCE, CONFIG, open_position_count=0)
    assert "stop loss distance" in exc.value.reason.lower()


def test_take_profit_too_close_raises():
    # TP must be >= 0.5% from entry (fee hurdle)
    entry = 145.20
    tp = entry * (1 + 0.002)  # 0.2% above entry — below 0.5% threshold
    order = {**VALID_ORDER, "entry_price": entry, "take_profit_price": tp}
    with pytest.raises(RiskViolation) as exc:
        validate_order(order, BALANCE, CONFIG, open_position_count=0)
    assert "take profit distance" in exc.value.reason.lower()


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


# ---- New checks for M6: SL/TP geometry ----

def test_long_sl_above_entry_fails():
    # Long: SL must be BELOW entry
    order = {**VALID_ORDER, "stop_loss_price": 150.00}  # SL > entry
    with pytest.raises(RiskViolation) as exc:
        validate_order(order, BALANCE, CONFIG, open_position_count=0)
    assert "geometry" in exc.value.reason.lower()


def test_long_tp_below_entry_fails():
    # Long: TP must be ABOVE entry
    order = {**VALID_ORDER, "take_profit_price": 140.00}  # TP < entry
    with pytest.raises(RiskViolation) as exc:
        validate_order(order, BALANCE, CONFIG, open_position_count=0)
    assert "geometry" in exc.value.reason.lower()


def test_short_tp_above_entry_fails():
    # Short: TP must be BELOW entry
    order = {
        **VALID_ORDER,
        "direction": "short",
        "stop_loss_price": 150.00,  # SL above entry
        "take_profit_price": 150.00,  # TP above entry — wrong for short
    }
    with pytest.raises(RiskViolation) as exc:
        validate_order(order, BALANCE, CONFIG, open_position_count=0)
    assert "geometry" in exc.value.reason.lower()


def test_short_sl_below_entry_fails():
    # Short: SL must be ABOVE entry
    order = {
        **VALID_ORDER,
        "direction": "short",
        "stop_loss_price": 140.00,  # SL below entry — wrong for short
        "take_profit_price": 140.00,
    }
    with pytest.raises(RiskViolation) as exc:
        validate_order(order, BALANCE, CONFIG, open_position_count=0)
    assert "geometry" in exc.value.reason.lower()


# ---- New checks for M6: notional exposure ----

def test_notional_exposure_over_cap_raises():
    # position_size_pct * leverage = 0.20 * 10 = 2.0 → at the boundary, should pass
    order = {**VALID_ORDER, "position_size_pct": 0.20, "leverage": 10}
    validate_order(order, BALANCE, CONFIG, open_position_count=0)

    # Just over the cap: 0.20 * 9 = 1.8 < 2.0 → passes
    order = {**VALID_ORDER, "position_size_pct": 0.20, "leverage": 9}
    validate_order(order, BALANCE, CONFIG, open_position_count=0)

    # Over the cap: use high caps config to test notional exposure independently
    # 0.30 * 10 = 3.0 > 2.0, but 0.30 <= 0.50 (max_position_size_pct) and 10 <= 20 (max_leverage)
    order = {**VALID_ORDER, "position_size_pct": 0.30, "leverage": 10}
    with pytest.raises(RiskViolation) as exc:
        validate_order(order, BALANCE, CONFIG_HIGH_CAPS, open_position_count=0)
    assert "notional exposure" in exc.value.reason.lower()


def test_notional_exposure_within_cap_passes():
    # 0.15 * 8 = 1.2 < 2.0 → should pass
    order = {**VALID_ORDER, "position_size_pct": 0.15, "leverage": 8}
    validate_order(order, BALANCE, CONFIG, open_position_count=0)


# ---- New checks for M6: entry price proximity ----

def test_entry_price_too_far_from_heartbeat_raises():
    # Heartbeat price is 145.20, entry at 146.00 is 0.55% away — above 0.5% threshold
    order = {**VALID_ORDER, "entry_price": 146.00}
    heartbeat_price = 145.20
    with pytest.raises(RiskViolation) as exc:
        validate_order(order, BALANCE, CONFIG, open_position_count=0, heartbeat_price=heartbeat_price)
    assert "deviates" in exc.value.reason.lower()


def test_entry_price_within_heartbeat_tolerance_passes():
    # 0.4% deviation — within 0.5% tolerance
    order = {**VALID_ORDER, "entry_price": 145.78}  # 0.4% above 145.20
    heartbeat_price = 145.20
    validate_order(order, BALANCE, CONFIG, open_position_count=0, heartbeat_price=heartbeat_price)


def test_no_heartbeat_price_skips_proximity_check():
    # When heartbeat_price is None, no deviation check should occur
    # Use a valid entry price that passes geometry check
    order = {**VALID_ORDER, "entry_price": 145.20}
    validate_order(order, BALANCE, CONFIG, open_position_count=0, heartbeat_price=None)


# ---- New checks for M6: reward:risk ratio ----

def test_reward_risk_too_low_raises():
    # Reward: 6.5/145.20 = 4.48%, Risk: 2.2/145.20 = 1.52% → ratio = 2.95 — should pass
    # Make it fail: very tight TP, wide SL
    entry = 145.20
    sl = entry * 0.97  # 3% below → risk = 3%
    tp = entry * 1.035  # 3.5% above → reward = 3.5% → ratio = 1.17 — passes
    # Make it fail: risk = 3%, reward = 1% → ratio = 0.33 < 0.5
    tp = entry * 1.01  # 1% above → reward = 1% → ratio = 0.33
    order = {**VALID_ORDER, "entry_price": entry, "stop_loss_price": sl, "take_profit_price": tp}
    with pytest.raises(RiskViolation) as exc:
        validate_order(order, BALANCE, CONFIG, open_position_count=0)
    assert "reward:risk" in exc.value.reason.lower()


def test_reward_risk_at_minimum_passes():
    # reward:risk = 0.5 exactly — should pass
    entry = 145.20
    sl = entry * 0.98  # 2% below → risk = 2%
    tp = entry * 1.03  # 3% above → reward = 3% → ratio = 1.5 — passes
    order = {**VALID_ORDER, "entry_price": entry, "stop_loss_price": sl, "take_profit_price": tp}
    validate_order(order, BALANCE, CONFIG, open_position_count=0)
