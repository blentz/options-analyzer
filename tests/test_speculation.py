"""Unit tests for speculation strategy analysis."""

from datetime import date, timedelta
import pytest

from app.services.speculation_analysis import (
    OptionLeg,
    calculate_strategy_pnl_at_price,
    find_breakeven_prices,
    calculate_max_profit_loss,
    analyze_strategy,
)


def _exp(days_out: int = 30) -> date:
    return date.today() + timedelta(days=days_out)


# ----------------------------------------------------------------------------
# Strategy P&L at price — covers spread payoff shape, not just single leg.
# ----------------------------------------------------------------------------

class TestStrategyPNL:
    def iron_condor(self):
        e = _exp(30)
        # Sell 95P / Buy 90P (5pt wing) + Sell 105C / Buy 110C (5pt wing).
        # Premiums chosen so net credit = $2/share ($200/contract).
        return [
            OptionLeg("PUT",  95,  e, "SELL", 1, premium=1.50),
            OptionLeg("PUT",  90,  e, "BUY",  1, premium=0.50),
            OptionLeg("CALL", 105, e, "SELL", 1, premium=1.50),
            OptionLeg("CALL", 110, e, "BUY",  1, premium=0.50),
        ]

    def test_max_profit_inside_body(self):
        legs = self.iron_condor()
        assert calculate_strategy_pnl_at_price(legs, 100) == pytest.approx(200)

    def test_max_loss_below_long_put(self):
        legs = self.iron_condor()
        assert calculate_strategy_pnl_at_price(legs, 85) == pytest.approx(-300)

    def test_max_loss_above_long_call(self):
        legs = self.iron_condor()
        assert calculate_strategy_pnl_at_price(legs, 115) == pytest.approx(-300)

    def test_pnl_zero_at_breakevens(self):
        # Short put be = 95 - 2 = 93; short call be = 105 + 2 = 107.
        legs = self.iron_condor()
        assert calculate_strategy_pnl_at_price(legs, 93) == pytest.approx(0)
        assert calculate_strategy_pnl_at_price(legs, 107) == pytest.approx(0)


# ----------------------------------------------------------------------------
# Exact breakevens (no sampling) — regression for the 200-sample-interp method.
# ----------------------------------------------------------------------------

class TestExactBreakevens:
    def test_iron_condor_two_breakevens_exact(self):
        e = _exp(30)
        legs = [
            OptionLeg("PUT",  95,  e, "SELL", 1, premium=1.50),
            OptionLeg("PUT",  90,  e, "BUY",  1, premium=0.50),
            OptionLeg("CALL", 105, e, "SELL", 1, premium=1.50),
            OptionLeg("CALL", 110, e, "BUY",  1, premium=0.50),
        ]
        bes = find_breakeven_prices(legs, current_price=100)
        assert sorted(bes) == [pytest.approx(93.0), pytest.approx(107.0)]

    def test_single_leg_long_call(self):
        e = _exp(30)
        legs = [OptionLeg("CALL", 100, e, "BUY", 1, premium=2.0)]
        bes = find_breakeven_prices(legs, current_price=100)
        assert bes == [pytest.approx(102.0)]


# ----------------------------------------------------------------------------
# Max profit / max loss
# ----------------------------------------------------------------------------

class TestMaxProfitLoss:
    def test_credit_spread_defined_risk(self):
        e = _exp(30)
        # Bull put spread: SELL 95P, BUY 90P. Net credit = 2.0.
        legs = [
            OptionLeg("PUT", 95, e, "SELL", 1, premium=2.5),
            OptionLeg("PUT", 90, e, "BUY",  1, premium=0.5),
        ]
        mp, ml = calculate_max_profit_loss(legs, current_price=100)
        assert mp == pytest.approx(200.0)     # Keep full credit
        assert ml == pytest.approx(-300.0)    # Wing width $5 - credit $2 = $3 loss * 100


# ----------------------------------------------------------------------------
# Multi-breakeven profit probability — regression for the single-BE-only check
# ----------------------------------------------------------------------------

class TestProfitProbability:
    def test_single_leg_long_call_has_probability(self):
        e = _exp(30)
        legs = [OptionLeg("CALL", 100, e, "BUY", 1, premium=2.0)]
        analysis = analyze_strategy("AAPL", legs, current_price=100, implied_volatility=0.30)
        assert analysis.profit_probability is not None
        assert 0 < analysis.profit_probability < 100

    def test_iron_condor_has_probability(self):
        # Previously returned None for any strategy with != 1 breakeven.
        e = _exp(30)
        legs = [
            OptionLeg("PUT",  95,  e, "SELL", 1, premium=1.50),
            OptionLeg("PUT",  90,  e, "BUY",  1, premium=0.50),
            OptionLeg("CALL", 105, e, "SELL", 1, premium=1.50),
            OptionLeg("CALL", 110, e, "BUY",  1, premium=0.50),
        ]
        analysis = analyze_strategy("AAPL", legs, current_price=100, implied_volatility=0.30)
        assert analysis.profit_probability is not None
        # ~64-65% for these parameters (verified by hand).
        assert 50 < analysis.profit_probability < 80

    def test_long_straddle_two_breakevens(self):
        # Long straddle: probability = P(big move either way).
        e = _exp(60)
        legs = [
            OptionLeg("CALL", 100, e, "BUY", 1, premium=4.0),
            OptionLeg("PUT",  100, e, "BUY", 1, premium=4.0),
        ]
        analysis = analyze_strategy("AAPL", legs, current_price=100, implied_volatility=0.30)
        assert analysis.profit_probability is not None
        # Cheap straddle with wide wings should have meaningful probability.
        assert 10 < analysis.profit_probability < 70
