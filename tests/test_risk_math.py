"""Unit tests for the Black-Scholes-based risk math.

These cover the bugs we previously fixed by inspection. Each test pins the
correct behaviour so the next refactor can't silently reintroduce the bug.
"""

import math
import pytest

from app.services.risk_analysis import (
    calculate_close_pnl,
    calculate_option_pnl_at_expiry,
    calculate_max_risk,
    calculate_breakeven,
    calculate_option_price,
    calculate_option_greeks,
    _estimate_delta,
    _calculate_price_probability,
    estimate_underlying_for_option_value,
    generate_price_scenarios,
)


# ----------------------------------------------------------------------------
# calculate_close_pnl — sign handling for LONG vs SHORT
# This is the bug that silently inverted long-position P&L for months.
# ----------------------------------------------------------------------------

class TestCloseP:
    def test_short_put_buyback_for_profit(self):
        # Sold 1 put for $5/share ($500), buy back at $3/share ($300) => +$200
        assert calculate_close_pnl("SHORT PUT", premium=500.0, close_price_per_share=3.0, num_contracts=1) == 200.0

    def test_short_call_buyback_for_loss(self):
        # Sold for $1, buy back at $4 => -$300
        assert calculate_close_pnl("SHORT CALL", premium=100.0, close_price_per_share=4.0, num_contracts=1) == -300.0

    def test_long_call_sell_for_loss(self):
        # Long premium passes as NEGATIVE per convention. Paid $5, sell at $3 => -$200
        assert calculate_close_pnl("LONG CALL", premium=-500.0, close_price_per_share=3.0, num_contracts=1) == -200.0

    def test_long_put_sell_for_profit(self):
        # Paid $2, sell at $5 => +$300
        assert calculate_close_pnl("LONG PUT", premium=-200.0, close_price_per_share=5.0, num_contracts=1) == 300.0


# ----------------------------------------------------------------------------
# calculate_max_risk — short calls must be inf, not a 3x guess.
# ----------------------------------------------------------------------------

class TestMaxRisk:
    def test_short_call_is_infinite(self):
        risk, unlimited = calculate_max_risk(
            option_type="CALL", strategy="SHORT CALL", strike=100, premium=200.0,
            num_contracts=1, current_price=100.0,
        )
        assert math.isinf(risk)
        assert unlimited is True

    def test_short_put_is_bounded(self):
        # Max loss = strike * 100 - premium
        risk, unlimited = calculate_max_risk("PUT", "SHORT PUT", strike=50, premium=100.0, num_contracts=1)
        assert risk == 50 * 100 - 100
        assert unlimited is False

    def test_long_call_max_loss_is_premium(self):
        risk, unlimited = calculate_max_risk("CALL", "LONG CALL", strike=100, premium=200.0, num_contracts=1)
        assert risk == 200.0
        assert unlimited is False


# ----------------------------------------------------------------------------
# Black-Scholes pricing & Greeks
# ----------------------------------------------------------------------------

class TestBSPricing:
    def test_atm_call_price_grows_with_time(self):
        p30 = calculate_option_price("CALL", 100, 100, 30, volatility=0.30)
        p90 = calculate_option_price("CALL", 100, 100, 90, volatility=0.30)
        assert p90 > p30 > 0

    def test_intrinsic_at_expiry(self):
        # At dte=0 the price is intrinsic value only.
        assert calculate_option_price("CALL", 110, 100, 0) == pytest.approx(10.0)
        assert calculate_option_price("PUT", 90, 100, 0) == pytest.approx(10.0)
        assert calculate_option_price("CALL", 90, 100, 0) == pytest.approx(0.0)

    def test_put_call_parity_approx(self):
        # C - P = S - K * exp(-rT), within numerical tolerance.
        S, K, T_days = 100.0, 100.0, 60
        C = calculate_option_price("CALL", S, K, T_days, volatility=0.30, risk_free_rate=0.05)
        P = calculate_option_price("PUT", S, K, T_days, volatility=0.30, risk_free_rate=0.05)
        T = T_days / 365.0
        expected = S - K * math.exp(-0.05 * T)
        assert C - P == pytest.approx(expected, abs=1e-4)

    def test_atm_delta_near_half(self):
        # ATM call delta should be slightly above 0.5 (drift); put close to -0.5.
        g_c = calculate_option_greeks("CALL", 100, 100, 30, volatility=0.30)
        g_p = calculate_option_greeks("PUT", 100, 100, 30, volatility=0.30)
        assert 0.5 < g_c["delta"] < 0.65
        assert -0.65 < g_p["delta"] < -0.4

    def test_long_options_have_positive_vega(self):
        g = calculate_option_greeks("CALL", 100, 100, 30, volatility=0.30)
        assert g["vega"] > 0
        assert g["gamma"] > 0

    def test_long_options_have_negative_theta(self):
        # Per-share theta should be negative for ATM long options.
        g = calculate_option_greeks("CALL", 100, 100, 30, volatility=0.30)
        assert g["theta"] < 0


# ----------------------------------------------------------------------------
# estimate_underlying_for_option_value — 3σ centered on CURRENT, not extrapolated
# ----------------------------------------------------------------------------

class TestPriceEstimation:
    def test_3sigma_centered_on_current_not_estimate(self):
        """
        Regression: previously the 3σ band was centered on the (extrapolated)
        estimated price, compounding linear-delta error into the confidence
        interval. Bounds should be relative to the CURRENT price.
        """
        # Setup: short put $100, currently $95 (in the money), target option value 0.
        est, low, high = estimate_underlying_for_option_value(
            option_type="PUT", strike=100, current_price=95,
            current_option_value=5.0, target_option_value=0.5,
            days_to_expiry=30, volatility=0.30,
        )
        # Compute bounds independently from current_price (95) — they must match.
        T = 30 / 365.0
        drift = (0.05 - 0.5 * 0.30 ** 2) * T
        expected_low = 95 * math.exp(drift - 3.0 * 0.30 * math.sqrt(T))
        expected_high = 95 * math.exp(drift + 3.0 * 0.30 * math.sqrt(T))
        assert low == pytest.approx(expected_low, rel=1e-6)
        assert high == pytest.approx(expected_high, rel=1e-6)

    def test_low_delta_returns_wide_band(self):
        # Deep OTM call: tiny delta, band should be wide and centered on current.
        est, low, high = estimate_underlying_for_option_value(
            option_type="CALL", strike=200, current_price=100,
            current_option_value=0.01, target_option_value=0.005,
            days_to_expiry=14, volatility=0.30,
        )
        assert low < 100 < high


# ----------------------------------------------------------------------------
# Generated price scenarios — dynamic range based on vol+dte
# ----------------------------------------------------------------------------

class TestPriceScenarios:
    def test_no_current_price_falls_back_to_strike_range(self):
        scenarios = generate_price_scenarios(
            "PUT", "SHORT PUT", strike=100, premium=200, num_contracts=1,
            current_price=None,
        )
        prices = [s.underlying_price for s in scenarios]
        # ±30% around strike
        assert min(prices) == pytest.approx(70, abs=0.5)
        assert max(prices) == pytest.approx(130, abs=0.5)

    def test_with_iv_uses_lognormal_3sigma(self):
        scenarios = generate_price_scenarios(
            "CALL", "LONG CALL", strike=100, premium=200, num_contracts=1,
            current_price=100, volatility=0.30, days_to_expiry=30,
        )
        prices = [s.underlying_price for s in scenarios]
        # 3σ for 30%*sqrt(30/365) ≈ 26%; expect a window roughly [76, 130]
        assert min(prices) < 85
        assert max(prices) > 115

    def test_low_vol_short_dte_still_includes_strike_kink(self):
        """The 3σ band can be tiny for low-vol low-dte combos; strike must
        still be inside the window so the payoff kink is visible."""
        scenarios = generate_price_scenarios(
            "PUT", "SHORT PUT", strike=100, premium=20, num_contracts=1,
            current_price=120, volatility=0.05, days_to_expiry=2,
        )
        prices = [s.underlying_price for s in scenarios]
        assert min(prices) <= 100 <= max(prices)
