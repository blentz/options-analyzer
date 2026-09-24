"""Tests for the exact Black-Scholes solvers and the scenario-lab builders.

Reference numbers come from a hand analysis of HITI 10/16/26 $2.50 PUT on
2026-09-24 (S=2.66, 22 DTE, contract IV 73.44%, r=4%), cross-checked with
scipy.optimize.brentq.
"""

from datetime import date

import pytest

from app.services.bs_math import (
    calculate_option_price,
    calculate_touch_probability,
    solve_iv_for_option_price,
    solve_spot_for_option_price,
)
from app.services.exit_conditions import (
    DEFAULT_GRID_FRACTIONS,
    build_target_conditions,
    build_value_matrix,
    parse_grid_percents,
    select_volatility,
    target_days_grid,
)
from app.services.risk_analysis import estimate_underlying_for_option_value

R = 0.04
HITI = dict(option_type="PUT", strike=2.5)


class TestSolveSpot:
    def test_round_trip_put(self):
        price = calculate_option_price("PUT", 2.8, 2.5, 22, 0.7344, R)
        s = solve_spot_for_option_price("PUT", 2.5, price, 22, 0.7344, R)
        assert s == pytest.approx(2.8, abs=1e-4)

    def test_round_trip_call(self):
        price = calculate_option_price("CALL", 105, 100, 30, 0.3, R)
        s = solve_spot_for_option_price("CALL", 100, price, 30, 0.3, R)
        assert s == pytest.approx(105, abs=1e-3)

    def test_hiti_reference(self):
        s = solve_spot_for_option_price(target_price=0.05, days_to_expiry=22,
                                        volatility=0.7344, risk_free_rate=R, **HITI)
        assert s == pytest.approx(2.926, abs=0.002)

    def test_unreachable_target_returns_none(self):
        # A put can never be worth more than the discounted strike.
        assert solve_spot_for_option_price("PUT", 2.5, 3.0, 22, 0.7, R) is None
        assert solve_spot_for_option_price("PUT", 2.5, 0.0, 22, 0.7, R) is None

    def test_at_expiry_uses_intrinsic(self):
        assert solve_spot_for_option_price("PUT", 2.5, 0.05, 0, 0.7, R) == pytest.approx(2.45)
        assert solve_spot_for_option_price("CALL", 2.5, 0.05, 0, 0.7, R) == pytest.approx(2.55)


class TestSolveIv:
    def test_round_trip(self):
        price = calculate_option_price("PUT", 2.66, 2.5, 22, 0.7344, R)
        iv = solve_iv_for_option_price("PUT", 2.66, 2.5, 22, price, R)
        assert iv == pytest.approx(0.7344, abs=1e-4)

    def test_hiti_mid_implies_lower_iv(self):
        iv = solve_iv_for_option_price("PUT", 2.66, 2.5, 22, 0.10, R)
        assert iv == pytest.approx(0.6757, abs=0.002)

    def test_below_intrinsic_returns_none(self):
        # Put $5 ITM can't trade at $1.
        assert solve_iv_for_option_price("PUT", 95, 100, 30, 1.0, R) is None

    def test_zero_dte_returns_none(self):
        assert solve_iv_for_option_price("PUT", 2.66, 2.5, 0, 0.1, R) is None


class TestTouchProbability:
    def test_touch_at_least_terminal(self):
        from app.services.bs_math import _calculate_price_probability
        terminal = _calculate_price_probability(2.66, 2.93, 22, 0.7344, R, want_above=True)
        touch = calculate_touch_probability(2.66, 2.93, 22, 0.7344, R)
        assert terminal < touch <= 1.0

    def test_zero_drift_is_twice_terminal(self):
        # With mu = r - sigma^2/2 = 0, reflection gives P(touch) = 2 P(S_T > H).
        vol = 0.4
        r = vol ** 2 / 2
        from app.services.bs_math import _calculate_price_probability
        terminal = _calculate_price_probability(100, 110, 60, vol, r, want_above=True)
        touch = calculate_touch_probability(100, 110, 60, vol, r)
        assert touch == pytest.approx(2 * terminal, rel=1e-9)

    def test_lower_barrier(self):
        p = calculate_touch_probability(100, 90, 30, 0.3, R)
        assert 0 < p < 1

    def test_already_there(self):
        assert calculate_touch_probability(100, 100, 30, 0.3, R) == 1.0


class TestTargetDaysGrid:
    @pytest.mark.parametrize("dte", [1, 5, 22, 45, 120, 400])
    def test_grid_shape(self, dte):
        grid = target_days_grid(dte)
        assert grid[0] == dte
        assert grid == sorted(set(grid), reverse=True)
        assert all(1 <= d <= dte for d in grid)
        assert len(grid) <= 10

    def test_zero_dte(self):
        assert target_days_grid(0) == []

    def test_default_fractions(self):
        assert target_days_grid(22) == [22, 16, 11, 7, 5, 4, 3, 1]

    def test_custom_fractions(self):
        # 90% and 60% of 22 DTE -> 20 and 13; fixed 7/5/3/1 always kept.
        assert target_days_grid(22, fractions=(0.9, 0.6)) == [22, 20, 13, 7, 5, 3, 1]

    def test_empty_fractions_keeps_fixed_points(self):
        assert target_days_grid(22, fractions=()) == [22, 7, 5, 3, 1]

    def test_fractions_flow_into_builders(self):
        tc = build_target_conditions(
            spot=2.66, days_to_expiry=22, volatility=0.7344, target_price=0.05,
            today=date(2026, 9, 24), risk_free_rate=R, fractions=(0.9,), **HITI,
        )
        assert [r.days_left for r in tc.spot_path] == [22, 20, 7, 5, 3, 1]
        assert [r.days_left for r in tc.iv_path] == [22, 20, 7, 5, 3, 1]
        m = build_value_matrix(
            option_type="PUT", strike=2.5, spot=2.66, days_to_expiry=22,
            volatility=0.7344, strategy="SHORT PUT", premium_per_share=0.25,
            num_contracts=1, today=date(2026, 9, 24), risk_free_rate=R, fractions=(0.9,),
        )
        assert m.days_left == [22, 20, 7, 5, 3, 1, 0]


class TestParseGridPercents:
    def test_parses_and_sorts(self):
        assert parse_grid_percents("20, 75,50,33") == (0.75, 0.5, 0.33, 0.2)

    def test_blank_means_default(self):
        assert parse_grid_percents(None) == DEFAULT_GRID_FRACTIONS
        assert parse_grid_percents("  ") == DEFAULT_GRID_FRACTIONS

    def test_dedupes(self):
        assert parse_grid_percents("50,50,25") == (0.5, 0.25)

    @pytest.mark.parametrize("bad", ["0", "100", "-5", "abc", "50,,x", ",".join(str(p) for p in range(10, 19))])
    def test_rejects_invalid(self, bad):
        with pytest.raises(ValueError):
            parse_grid_percents(bad)


class TestBuildTargetConditions:
    @pytest.fixture
    def hiti(self):
        return build_target_conditions(
            spot=2.66, days_to_expiry=22, volatility=0.7344, target_price=0.05,
            today=date(2026, 9, 24), risk_free_rate=R, **HITI,
        )

    def test_direction_and_now(self, hiti):
        assert hiti.direction == "decrease"
        assert hiti.spot_needed_now == pytest.approx(2.926, abs=0.002)
        assert hiti.move_pct == pytest.approx(10.0, abs=0.2)

    def test_spot_path_falls_with_time(self, hiti):
        by_day = {r.days_left: r.spot_needed for r in hiti.spot_path}
        assert by_day[22] == pytest.approx(2.926, abs=0.002)
        needed = [r.spot_needed for r in hiti.spot_path]
        assert needed == sorted(needed, reverse=True)

    def test_iv_path(self, hiti):
        by_day = {r.days_left: r for r in hiti.iv_path}
        assert by_day[22].iv_needed == pytest.approx(0.4534, abs=0.002)
        assert not by_day[22].already_met

    def test_hold_path_flat_stock(self, hiti):
        flat = next(r for r in hiti.hold_path if r.spot == pytest.approx(2.66))
        assert flat.days_left == 8
        assert flat.date == date(2026, 10, 8)

    def test_hold_path_below_strike_never(self, hiti):
        deep = build_target_conditions(
            spot=2.30, days_to_expiry=22, volatility=0.7344, target_price=0.05,
            today=date(2026, 9, 24), risk_free_rate=R, **HITI,
        )
        flat = next(r for r in deep.hold_path if r.spot == pytest.approx(2.30))
        assert flat.days_left is None

    def test_probabilities(self, hiti):
        assert 0.25 < hiti.prob_finish_beyond < 0.30
        assert hiti.prob_touch > hiti.prob_finish_beyond

    def test_increase_direction_has_no_hold_path(self):
        tc = build_target_conditions(
            spot=2.66, days_to_expiry=22, volatility=0.7344, target_price=0.30,
            today=date(2026, 9, 24), risk_free_rate=R, **HITI,
        )
        assert tc.direction == "increase"
        assert tc.hold_path == []
        assert tc.spot_needed_now < 2.66


class TestValueMatrix:
    def test_short_put_pnl_signs(self):
        m = build_value_matrix(
            option_type="PUT", strike=2.5, spot=2.66, days_to_expiry=22,
            volatility=0.7344, strategy="SHORT PUT", premium_per_share=0.25,
            num_contracts=2, today=date(2026, 9, 24), risk_free_rate=R,
        )
        assert len(m.pnl) == len(m.spots)
        assert all(len(row) == len(m.days_left) for row in m.pnl)
        top, bottom = m.pnl[0], m.pnl[-1]  # spots descend
        assert m.spots[0] > m.spots[-1]
        assert top[-1] > bottom[-1]
        # At expiry column (days_left 0) far above strike: keep full premium.
        assert m.days_left[-1] == 0
        assert top[-1] == pytest.approx(0.25 * 200, abs=0.01)

    def test_long_pnl_is_mirror(self):
        kw = dict(option_type="CALL", strike=100, spot=100, days_to_expiry=30,
                  volatility=0.3, premium_per_share=2.0, num_contracts=1,
                  today=date(2026, 9, 24), risk_free_rate=R)
        short = build_value_matrix(strategy="SHORT CALL", **kw)
        long = build_value_matrix(strategy="LONG CALL", **kw)
        assert short.pnl[0][0] == pytest.approx(-long.pnl[0][0])


class TestSelectVolatility:
    def test_override_wins(self):
        assert select_volatility(override=0.5, contract_iv=0.7, mid_iv=0.6,
                                 spread_quality="tight", symbol_iv=1.1) == (0.5, "override")

    def test_tradeable_mid_beats_scraped_contract_iv(self):
        assert select_volatility(None, 0.7, 0.6, "moderate", 1.1) == (0.6, "implied_from_mid")

    def test_wide_spread_prefers_contract_iv(self):
        assert select_volatility(None, 0.7, 0.6, "very_wide", 1.1) == (0.7, "contract")

    def test_wide_mid_beats_symbol(self):
        assert select_volatility(None, None, 0.6, "wide", 1.1) == (0.6, "implied_from_mid")

    def test_fallbacks(self):
        assert select_volatility(None, None, None, None, 1.1) == (1.1, "symbol")
        vol, src = select_volatility(None, None, None, None, None)
        assert src == "default" and vol > 0


class TestEstimateUnderlyingIsExact:
    def test_accounts_for_gamma(self):
        # Model value at S=2.66 with 73.44% IV; halve it. The old delta-linear
        # estimate landed ~$0.10 short of the exact root.
        cur = calculate_option_price("PUT", 2.66, 2.5, 22, 0.7344)
        est, _, _ = estimate_underlying_for_option_value(
            "PUT", 2.5, 2.66, cur, cur / 2, 22, volatility=0.7344,
        )
        exact = solve_spot_for_option_price("PUT", 2.5, cur / 2, 22, 0.7344)
        assert est == pytest.approx(exact, abs=1e-3)
