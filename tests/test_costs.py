"""Tests for execution/costs.py — shared fee/funding computation module."""
import pytest
from execution.costs import (
    all_costs_from_trade,
    compute_fees,
    compute_funding_pnl,
    compute_funding_pnl_simple,
    compute_gross_pnl,
    compute_net_pnl,
    compute_position_size_in_coins,
    compute_true_notional,
)


class TestComputeTrueNotional:
    def test_basic_true_notional(self):
        # $5k margin, 3x leverage = $15k true notional
        assert compute_true_notional(50000.0, 0.10, 3) == pytest.approx(15000.0)

    def test_no_leverage(self):
        # 1x = margin itself
        assert compute_true_notional(50000.0, 0.10, 1) == pytest.approx(5000.0)

    def test_max_leverage(self):
        assert compute_true_notional(50000.0, 0.20, 10) == pytest.approx(100000.0)

    def test_zero_balance(self):
        assert compute_true_notional(0.0, 0.10, 3) == pytest.approx(0.0)


class TestComputeFees:
    def test_fees_on_leveraged_notional(self):
        # $15k true notional at 3.5 bps per side
        fees = compute_fees(true_notional=15000.0, taker_fee=0.00035, sides=2)
        assert fees["entry_fee"] == pytest.approx(5.25)  # 15000 * 0.00035
        assert fees["exit_fee"] == pytest.approx(5.25)
        assert fees["total_fees"] == pytest.approx(10.50)

    def test_single_side(self):
        fees = compute_fees(true_notional=15000.0, taker_fee=0.00035, sides=1)
        assert fees["entry_fee"] == pytest.approx(5.25)
        assert fees["exit_fee"] == pytest.approx(0.0)
        assert fees["total_fees"] == pytest.approx(5.25)

    def test_zero_notional(self):
        fees = compute_fees(true_notional=0.0, taker_fee=0.00035)
        assert fees["total_fees"] == 0.0


class TestComputeGrossPnl:
    def test_long_profit(self):
        # $15k long at 145.20, exit at 149.01, 3x leverage
        gross = compute_gross_pnl(
            entry_price=145.20, exit_price=149.01,
            direction="long", leverage=3, true_notional=15000.0,
        )
        expected_pnl_pct = (149.01 - 145.20) / 145.20 * 3
        assert gross["pnl_pct"] == pytest.approx(expected_pnl_pct)
        assert gross["pnl_usd"] == pytest.approx(15000.0 * expected_pnl_pct)

    def test_short_profit(self):
        gross = compute_gross_pnl(
            entry_price=150.0, exit_price=140.0,
            direction="short", leverage=2, true_notional=10000.0,
        )
        expected_pnl_pct = (150.0 - 140.0) / 150.0 * 2
        assert gross["pnl_pct"] == pytest.approx(expected_pnl_pct)
        assert gross["pnl_usd"] == pytest.approx(10000.0 * expected_pnl_pct)

    def test_long_loss(self):
        gross = compute_gross_pnl(
            entry_price=100.0, exit_price=95.0,
            direction="long", leverage=1, true_notional=5000.0,
        )
        assert gross["pnl_pct"] == pytest.approx(-0.05)
        assert gross["pnl_usd"] == pytest.approx(-250.0)


class TestComputeFundingPnl:
    def test_long_pays_positive_funding(self):
        # Long pays funding: if funding rate is positive, long loses money
        funding_history = [
            {"time": 1000, "fundingRate": 0.001},
            {"time": 2000, "fundingRate": 0.001},
        ]
        result = compute_funding_pnl(
            position_size_coins=100.0,  # 100 coins
            direction="long",
            funding_history=funding_history,
            entry_ts_unix=0.5,
            close_ts_unix=2.5,
        )
        # Long pays: -100 * (0.001 + 0.001) = -0.20
        assert result == pytest.approx(-0.20)

    def test_short_receives_positive_funding(self):
        # Short receives: if funding rate is positive, short makes money
        funding_history = [
            {"time": 1000, "fundingRate": 0.001},
            {"time": 2000, "fundingRate": 0.001},
        ]
        result = compute_funding_pnl(
            position_size_coins=100.0,
            direction="short",
            funding_history=funding_history,
            entry_ts_unix=0.5,
            close_ts_unix=2.5,
        )
        # Short receives: +100 * (0.001 + 0.001) = +0.20
        assert result == pytest.approx(0.20)

    def test_funding_sign_and_magnitude(self):
        # Known rate, known position size → exact arithmetic
        # 50 BTC at 0.0005 funding rate, long → pays 50 * 0.0005 = 0.025
        result = compute_funding_pnl(
            position_size_coins=50.0,
            direction="long",
            funding_history=[{"time": 5000, "fundingRate": 0.0005}],
            entry_ts_unix=1.0,
            close_ts_unix=10.0,
        )
        assert result == pytest.approx(-0.025)

    def test_empty_history_returns_zero(self):
        result = compute_funding_pnl(
            position_size_coins=100.0,
            direction="long",
            funding_history=[],
            entry_ts_unix=0.0,
            close_ts_unix=10.0,
        )
        assert result == 0.0

    def test_none_rate_skipped(self):
        funding_history = [
            {"time": 1000, "fundingRate": None},
            {"time": 2000, "fundingRate": 0.001},
        ]
        result = compute_funding_pnl(
            position_size_coins=100.0,
            direction="long",
            funding_history=funding_history,
            entry_ts_unix=0.5,
            close_ts_unix=2.5,
        )
        assert result == pytest.approx(-0.10)


class TestComputeFundingPnlSimple:
    def test_long_pays_at_average_rate(self):
        result = compute_funding_pnl_simple(
            position_size_coins=100.0,
            direction="long",
            avg_funding_rate=0.001,
            duration_hours=8.0,
        )
        # -100 * 0.001 * 8 = -0.80
        assert result == pytest.approx(-0.80)

    def test_short_receives_at_average_rate(self):
        result = compute_funding_pnl_simple(
            position_size_coins=100.0,
            direction="short",
            avg_funding_rate=0.0005,
            duration_hours=24.0,
        )
        assert result == pytest.approx(1.20)


class TestComputePositionSizeInCoins:
    def test_basic(self):
        # $15k notional at $145.20 = 103.3... coins
        size = compute_position_size_in_coins(15000.0, 145.20)
        expected = 15000.0 / 145.20
        assert size == pytest.approx(expected)

    def test_zero_price(self):
        assert compute_position_size_in_coins(1000.0, 0.0) == 0.0


class TestAllCostsFromTrade:
    def test_complete_long_trade_costs(self):
        # $5k margin, 10% at 3x = $1.5k margin? No:
        # balance=50000, size_pct=0.10, leverage=3
        # true_notional = 50000 * 0.10 * 3 = 15000
        # entry at 145.20, exit at 149.01
        costs = all_costs_from_trade(
            entry_price=145.20,
            exit_price=149.01,
            direction="long",
            leverage=3,
            true_notional=15000.0,
            taker_fee=0.00035,
        )
        assert costs["true_notional"] == 15000.0
        assert costs["entry_fee"] == pytest.approx(5.25)
        assert costs["exit_fee"] == pytest.approx(5.25)
        assert costs["total_fees"] == pytest.approx(10.50)
        # Gross PnL before fees
        expected_pnl_pct = (149.01 - 145.20) / 145.20 * 3
        assert costs["gross_pnl_pct"] == pytest.approx(expected_pnl_pct)
        expected_gross_usd = 15000.0 * expected_pnl_pct
        assert costs["gross_pnl_usd"] == pytest.approx(expected_gross_usd)
        # Net = gross - total_fees + funding (funding not provided = 0)
        assert costs["net_pnl_usd"] == pytest.approx(expected_gross_usd - 10.50)
        assert costs["funding_pnl"] == 0.0

    def test_with_funding_history(self):
        costs = all_costs_from_trade(
            entry_price=100.0,
            exit_price=110.0,
            direction="long",
            leverage=2,
            true_notional=10000.0,
            taker_fee=0.00035,
            funding_history=[{"time": 5000, "fundingRate": 0.001}],
            entry_ts_unix=1.0,
            close_ts_unix=10.0,
        )
        # Position size = 10000/100 = 100 coins
        # Funding = -100 * 0.001 = -0.10
        assert costs["funding_pnl"] == pytest.approx(-0.10)
        assert costs["total_fees"] == pytest.approx(7.0)  # 10000 * 0.00035 * 2

    def test_five_x_leverage_fees_on_true_notional(self):
        # 5x leverage, $5k balance, 10% size → $2.5k margin → $12.5k true notional
        # Fees on $12.5k, not on $2.5k
        costs = all_costs_from_trade(
            entry_price=100.0,
            exit_price=101.0,
            direction="long",
            leverage=5,
            true_notional=12500.0,
            taker_fee=0.00035,
        )
        # Fees = 12500 * 0.00035 * 2 = 8.75
        assert costs["entry_fee"] == pytest.approx(4.375)
        assert costs["total_fees"] == pytest.approx(8.75)
