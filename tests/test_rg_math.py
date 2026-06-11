"""Unit tests for the Reality Gap (RG) math (Rentschler 2026).

Each test pins a specific clause of the paper so a refactor can't silently
drift from the published definition. Section references are to the working
paper reproduced in reality-gap-paper.pdf.
"""

import math

from app.services.rg_math import (
    NOT_COVERED,
    tangible_equity,
    real_earnings_by_year,
    smoothed_earnings,
    capitalized_earnings,
    fundamental_base,
    reality_gap,
    earnings_trend,
    compute_rg,
    compute_rg_sensitivity,
    format_rg,
)


# ---------------------------------------------------------------------------
# Tangible Equity (§3.3)
# ---------------------------------------------------------------------------

class TestTangibleEquity:
    def test_strips_goodwill_and_intangibles(self):
        assert tangible_equity(100.0, 30.0, 20.0) == 50.0

    def test_can_go_negative(self):
        # Acquisitive firm: goodwill + intangibles exceed book equity.
        assert tangible_equity(50.0, 40.0, 30.0) == -20.0


# ---------------------------------------------------------------------------
# Inflation adjustment + smoothing (§3.4)
# ---------------------------------------------------------------------------

class TestInflationAdjust:
    def test_adjusts_to_reference_year(self):
        ni = {2020: 100.0, 2024: 100.0}
        cpi = {2020: 100.0, 2024: 125.0}
        adj = real_earnings_by_year(ni, cpi, reference_year=2024)
        assert adj[2024] == 100.0           # reference year unchanged
        assert adj[2020] == 125.0           # 100 * 125/100

    def test_none_cpi_is_nominal(self):
        ni = {2020: 100.0, 2024: 200.0}
        assert real_earnings_by_year(ni, None) == ni

    def test_missing_cpi_point_falls_back_to_nominal(self):
        ni = {2020: 100.0, 2024: 100.0}
        cpi = {2024: 125.0}  # no 2020 entry
        adj = real_earnings_by_year(ni, cpi, reference_year=2024)
        assert adj[2020] == 100.0  # graceful nominal fallback


class TestSmoothedEarnings:
    def test_mean_over_window(self):
        ni = {2021: 10.0, 2022: 20.0, 2023: 30.0}
        g, used = smoothed_earnings(ni, None, window=10)
        assert g == 20.0
        assert used == 3

    def test_window_truncates_to_recent_years(self):
        ni = {y: float(y) for y in range(2010, 2025)}  # 15 years
        g, used = smoothed_earnings(ni, None, window=10)
        assert used == 10
        # most recent 10 fiscal years: 2015..2024
        assert g == sum(range(2015, 2025)) / 10.0

    def test_empty(self):
        assert smoothed_earnings({}, None) == (0.0, 0)


# ---------------------------------------------------------------------------
# Capitalization + base + ratio (§3.4, §3.2)
# ---------------------------------------------------------------------------

class TestCapitalizedEarnings:
    def test_positive_g_capitalized(self):
        assert capitalized_earnings(10.0, n=10) == 100.0

    def test_non_positive_g_is_zero(self):
        assert capitalized_earnings(0.0, n=10) == 0.0
        assert capitalized_earnings(-5.0, n=10) == 0.0


class TestRealityGapRatio:
    def test_basic_ratio(self):
        fb = fundamental_base(te=50.0, cap_earnings=100.0)  # 150
        assert reality_gap(300.0, fb) == 2.0

    def test_non_positive_base_not_covered(self):
        assert reality_gap(300.0, 0.0) == NOT_COVERED
        assert reality_gap(300.0, -10.0) == NOT_COVERED
        assert math.isinf(NOT_COVERED)


# ---------------------------------------------------------------------------
# Special cases (§3.5)
# ---------------------------------------------------------------------------

class TestSpecialCases:
    def test_case_a_negative_earnings_base_reduces_to_tangible_equity(self):
        # G ≤ 0 → E=0 → FB=TE. MC=200, TE=100 → RG=2.0
        r = compute_rg(
            market_cap=200.0, book_equity=130.0, goodwill=20.0, intangibles=10.0,
            net_income_by_year={2022: -5.0, 2023: -8.0}, n=10,
        )
        assert r.components.capitalized_earnings == 0.0
        assert r.components.fundamental_base == 100.0
        assert r.value == 2.0
        assert r.covered is True

    def test_case_b_no_substance_no_earnings_is_not_covered(self):
        # TE ≤ 0 and G ≤ 0 → "not fundamentally covered" (RG = ∞).
        r = compute_rg(
            market_cap=1000.0, book_equity=10.0, goodwill=40.0, intangibles=30.0,
            net_income_by_year={2022: -5.0, 2023: -1.0}, n=10,
        )
        assert r.components.tangible_equity == -60.0
        assert math.isinf(r.value)
        assert r.covered is False

    def test_case_c_tiny_base_gives_large_finite_rg(self):
        # Positive but tiny FB → very large RG, treated as a signal not an error.
        r = compute_rg(
            market_cap=1_000_000.0, book_equity=1.0, goodwill=0.0, intangibles=0.0,
            net_income_by_year={2023: -1.0}, n=10,
        )
        assert r.value == 1_000_000.0
        assert r.covered is True


# ---------------------------------------------------------------------------
# Earnings trend appendix (§3.7, Table 1)
# ---------------------------------------------------------------------------

def _six_years(new_block, old_block):
    # years 2019-2024; older block = 2019-2021, newer = 2022-2024
    return {
        2024: new_block, 2023: new_block, 2022: new_block,
        2021: old_block, 2020: old_block, 2019: old_block,
    }


class TestEarningsTrend:
    def test_none_when_insufficient_history(self):
        assert earnings_trend({2023: 1.0, 2024: 2.0}) is None

    def test_strongly_increasing(self):
        assert earnings_trend(_six_years(200.0, 100.0)) == "++"   # +100%

    def test_increasing(self):
        assert earnings_trend(_six_years(110.0, 100.0)) == "+"    # +10%

    def test_stable(self):
        assert earnings_trend(_six_years(102.0, 100.0)) == "="    # +2%

    def test_declining(self):
        assert earnings_trend(_six_years(90.0, 100.0)) == "-"     # -10%

    def test_strongly_declining(self):
        assert earnings_trend(_six_years(60.0, 100.0)) == "--"    # -40%

    def test_turnaround_from_loss_is_strong_increase(self):
        assert earnings_trend(_six_years(50.0, -10.0)) == "++"

    def test_both_negative_improving(self):
        assert earnings_trend(_six_years(-5.0, -20.0)) == "+"     # less negative

    def test_both_negative_deteriorating(self):
        assert earnings_trend(_six_years(-30.0, -10.0)) == "-"    # more negative


# ---------------------------------------------------------------------------
# Inflation adjustment changes the RG (end-to-end)
# ---------------------------------------------------------------------------

class TestInflationAffectsRG:
    def test_real_vs_nominal_differ(self):
        ni = {2015: 100.0, 2024: 100.0}
        cpi = {2015: 80.0, 2024: 120.0}  # older dollars worth more in real terms
        nominal = compute_rg(1000.0, 200.0, 0.0, 0.0, ni, cpi_by_year=None, n=10, window=10)
        real = compute_rg(1000.0, 200.0, 0.0, 0.0, ni, cpi_by_year=cpi, n=10, window=10)
        # real-adjusted 2015 income inflates (100 * 120/80 = 150) → higher G → higher FB → lower RG
        assert real.value < nominal.value


# ---------------------------------------------------------------------------
# N-sensitivity + reporting form (§3.8)
# ---------------------------------------------------------------------------

class TestSensitivityAndFormat:
    def test_sensitivity_runs_all_ns(self):
        results = compute_rg_sensitivity(
            market_cap=1000.0, book_equity=100.0, goodwill=0.0, intangibles=0.0,
            net_income_by_year={y: 10.0 for y in range(2015, 2025)},
        )
        assert set(results) == {8, 10, 12}
        # Larger N → larger base → smaller RG.
        assert results[8].value > results[10].value > results[12].value

    def test_format_compact_form(self):
        r = compute_rg(
            market_cap=1130.0, book_equity=100.0, goodwill=0.0, intangibles=0.0,
            net_income_by_year=_six_years(90.0, 100.0), n=10, window=10,
        )
        s = format_rg(r)
        assert s.startswith("RG10 ")
        assert s.endswith("-")  # declining trend appended

    def test_format_star_for_short_window(self):
        r = compute_rg(1000.0, 100.0, 0.0, 0.0, {2023: 10.0, 2024: 10.0}, n=10, window=10)
        assert format_rg(r, short_window=True).startswith("RG10* ")

    def test_format_not_covered(self):
        r = compute_rg(1000.0, 0.0, 50.0, 0.0, {2023: -1.0}, n=10)
        assert "∞" in format_rg(r)
