"""Tests for live-paper vs backtest cost parity.

Both the paper bridge and the backtest engine use execution/costs.py as
their single source of truth for fee and funding computation. These tests
verify that with identical inputs, both paths produce identical cost outputs.
"""
import pytest
from execution.costs import (
    all_costs_from_trade,
    compute_fees,
    compute_funding_pnl,
    compute_gross_pnl,
    compute_position_size_in_coins,
    compute_true_notional,
)


class TestPaperAndBacktestCostsIdentical:
    """The shared cost module is the ONLY fee/funding computation path for
    both paper bridge and backtest engine. These tests verify the module
    itself is internally consistent — since both paths call the same functions,
    parity is guaranteed by construction at the source level.

    The tests below provide concrete numerical regression against known
    trade scenarios so that any drift is caught at the module level rather
    than discovered as live/backtest divergence later."""

    def test_long_trade_cost_regression(self):
        # Balance=$50k, size=10%, lev=3x → true_notional=$15k
        # Entry=$145.20, Exit=$149.01, taker_fee=3.5bps
        true_notional = compute_true_notional(50000.0, 0.10, 3)
        assert true_notional == pytest.approx(15000.0)

        costs = all_costs_from_trade(
            entry_price=145.20,
            exit_price=149.01,
            direction="long",
            leverage=3,
            true_notional=true_notional,
            taker_fee=0.00035,
        )

        # Fees on $15k, not $5k (margin)
        assert costs["entry_fee"] == pytest.approx(5.25)
        assert costs["exit_fee"] == pytest.approx(5.25)
        assert costs["total_fees"] == pytest.approx(10.50)

        # Gross PnL on $15k: (149.01-145.20)/145.20 * 3 = 7.87%
        expected_pnl_pct = (149.01 - 145.20) / 145.20 * 3
        assert costs["gross_pnl_pct"] == pytest.approx(expected_pnl_pct, rel=1e-5)
        assert costs["gross_pnl_usd"] == pytest.approx(15000.0 * expected_pnl_pct, rel=1e-5)

    def test_short_trade_cost_regression(self):
        # Balance=$50k, size=20%, lev=2x → true_notional=$20k
        # Entry=$150.00, Exit=$140.00, taker_fee=3.5bps
        true_notional = compute_true_notional(50000.0, 0.20, 2)
        assert true_notional == pytest.approx(20000.0)

        costs = all_costs_from_trade(
            entry_price=150.00,
            exit_price=140.00,
            direction="short",
            leverage=2,
            true_notional=true_notional,
            taker_fee=0.00035,
        )

        assert costs["entry_fee"] == pytest.approx(7.0)  # 20000 * 0.00035
        assert costs["exit_fee"] == pytest.approx(7.0)
        assert costs["total_fees"] == pytest.approx(14.0)

        # Gross: (150-140)/150 * 2 = 13.33%
        expected_pnl_pct = (150.0 - 140.0) / 150.0 * 2
        assert costs["gross_pnl_pct"] == pytest.approx(expected_pnl_pct, rel=1e-5)

    def test_long_trade_with_funding_and_fees(self):
        # Long trade with known funding and fees:
        # - $10k margin, 5x lev → $50k true_notional
        # - Entry $100, Exit $105
        # - Funding: 0.0001/h over 24h
        # Position coins = 50000/100 = 500 coins
        # Funding cost = -500 * 0.0001 * 24 = -1.20
        # Gross = 50000 * (5/100) * 5 = 50000 * 5% ... wait
        # Gross PnL = 50000 * (105-100)/100 * 5 = 50000 * 0.25 = 12500
        # Fees = 50000 * 0.00035 * 2 = 35
        # Net = 12500 - 35 - 1.20 = 12463.80
        costs = all_costs_from_trade(
            entry_price=100.0,
            exit_price=105.0,
            direction="long",
            leverage=5,
            true_notional=50000.0,
            taker_fee=0.00035,
            funding_history=[
                {"time": 1000, "fundingRate": 0.0001},
                {"time": 3601000, "fundingRate": 0.0001},
                {"time": 7201000, "fundingRate": 0.0001},
                {"time": 10801000, "fundingRate": 0.0001},
            ],
            entry_ts_unix=0.5,
            close_ts_unix=86400.5,  # 24h later
        )
        assert costs["true_notional"] == 50000.0
        assert costs["entry_fee"] == pytest.approx(17.50)
        assert costs["total_fees"] == pytest.approx(35.0)
        # Funding: long pays positive rates
        assert costs["funding_pnl"] < 0
        # Gross PnL: (105-100)/100 * 5 = 25%
        assert costs["gross_pnl_pct"] == pytest.approx(0.25)
        assert costs["gross_pnl_usd"] == pytest.approx(12500.0)
