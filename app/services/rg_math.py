"""Pure Reality Gap (RG) math.

Implements the heuristic valuation indicator from Rentschler (2026),
"Reality Gap (RG): A Heuristic Indicator for Measuring the Distance Between
Market Valuation and Fundamental Coverage" (SSRN working paper).

Zero dependencies on DB, models, async, or network. Everything here is a pure
function on numbers and plain dicts — easy to unit-test, easy to reason about.
Fetching the inputs (stocknear financials, BLS CPI) lives in the services that
call this module.

The core ratio (paper §3.2):

    RG = MarketCap / FundamentalBase
    FundamentalBase = TangibleEquity + CapitalizedEarnings
    TangibleEquity  = BookEquity − Goodwill − IntangibleAssets
    CapitalizedEarnings = N × G   if G > 0   else 0
    G = mean of inflation-adjusted annual net income over the last `window` years

Interpretation (paper §3.6): RG < 1 market value below fundamental base; RG ≈ 1
in its order of magnitude; RG > 1 a valuation premium. RG is NOT a fair-value
signal — a high RG only means market value sits well above the conservatively
defined base.

All amounts are in the same currency unit (dollars). We use float throughout:
RG is an explicitly heuristic, order-of-magnitude diagnostic, so float precision
is ample and keeps the math readable.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

# Sentinel for paper §3.5 Case B: neither tangible substance nor positive
# long-run earnings cover the market value. Reported as "not fundamentally
# covered", equivalent to RG = ∞.
NOT_COVERED = math.inf

# Base capitalization factor (paper §3.4: N=10 ⇒ implicit 10% return).
# Sensitivity values N=8 and N=12 are reported alongside (paper §3.8 form).
DEFAULT_N = 10
SENSITIVITY_NS = (8, 10, 12)

# Trend appendix needs two rolling 3-year blocks ⇒ 6 years of net income.
TREND_WINDOW = 6


# ---------------------------------------------------------------------------
# Fundamental base components
# ---------------------------------------------------------------------------

def tangible_equity(book_equity: float, goodwill: float, intangibles: float) -> float:
    """Tangible Equity (paper §3.3): book equity stripped of goodwill and
    other intangible assets. A deliberately conservative substance floor —
    items whose reliability depends on acquisition accounting are excluded.
    May be negative (heavily intangible / acquisitive firms)."""
    return book_equity - goodwill - intangibles


def real_earnings_by_year(
    net_income_by_year: dict[int, float],
    cpi_by_year: Optional[dict[int, float]],
    reference_year: Optional[int] = None,
) -> dict[int, float]:
    """Inflation-adjust each year's net income to `reference_year` dollars.

    Adjusted_y = NI_y × CPI_ref / CPI_y. Years without a CPI entry fall back to
    nominal (factor 1.0) so a missing CPI point degrades gracefully rather than
    dropping the year. If `cpi_by_year` is None, returns nominal income
    unchanged (caller opted out of inflation adjustment).
    """
    if not net_income_by_year:
        return {}
    if reference_year is None:
        reference_year = max(net_income_by_year)
    if not cpi_by_year:
        return dict(net_income_by_year)

    cpi_ref = cpi_by_year.get(reference_year)
    adjusted: dict[int, float] = {}
    for year, ni in net_income_by_year.items():
        cpi_y = cpi_by_year.get(year)
        if cpi_ref and cpi_y:
            adjusted[year] = ni * cpi_ref / cpi_y
        else:
            adjusted[year] = ni  # no CPI for this pair → nominal fallback
    return adjusted


def smoothed_earnings(
    net_income_by_year: dict[int, float],
    cpi_by_year: Optional[dict[int, float]] = None,
    window: int = 10,
) -> tuple[float, int]:
    """G (paper §3.4): mean inflation-adjusted net income over the most recent
    `window` fiscal years.

    Returns (mean, years_used). If fewer than `window` years are available, the
    available years are used and `years_used` reflects that — callers can label
    a short window (e.g. RG10* / RG5). Returns (0.0, 0) when no data.
    """
    if not net_income_by_year:
        return 0.0, 0
    years = sorted(net_income_by_year, reverse=True)[:window]
    reference_year = years[0]
    adjusted = real_earnings_by_year(net_income_by_year, cpi_by_year, reference_year)
    used = [adjusted[y] for y in years]
    return sum(used) / len(used), len(used)


def capitalized_earnings(g: float, n: int = DEFAULT_N) -> float:
    """E (paper §3.4): capitalize positive long-run earning power into a
    stock-equivalent figure. Non-positive G contributes nothing — only tangible
    substance then remains as coverage (Case A)."""
    return n * g if g > 0 else 0.0


def fundamental_base(te: float, cap_earnings: float) -> float:
    """FB = TangibleEquity + CapitalizedEarnings (paper §3.2)."""
    return te + cap_earnings


def reality_gap(market_cap: float, fb: float) -> float:
    """RG = MarketCap / FundamentalBase.

    A non-positive fundamental base means the market value is not covered by
    conservatively defined fundamentals at all — paper §3.5 Case B reports this
    as "not fundamentally covered" (RG = ∞). This single guard subsumes Cases A
    and B: when G ≤ 0, FB collapses to TE, and a non-positive TE then yields ∞.
    A tiny positive FB (Case C) intentionally yields a very large finite RG —
    a signal, not an error.
    """
    if fb <= 0:
        return NOT_COVERED
    return market_cap / fb


# ---------------------------------------------------------------------------
# Earnings trend appendix (paper §3.7)
# ---------------------------------------------------------------------------

def earnings_trend(net_income_by_year: dict[int, float]) -> Optional[str]:
    """Qualitative trend code comparing two rolling 3-year blocks of *nominal*
    net income (paper §3.7, Table 1):

        B_new = mean(NI[t-1], NI[t-2], NI[t-3])
        B_old = mean(NI[t-4], NI[t-5], NI[t-6])

    When B_old > 0, the relative change B_new/B_old − 1 maps to:
        ++  > +25%      +  +5..+25%      =  −5..+5%      -  −25..−5%      --  < −25%

    When B_old ≤ 0 a percentage change is meaningless; we apply defined
    directional rules (the paper specifies "special rules" without fixed
    thresholds): a swing from non-positive to positive earnings is ++; otherwise
    we compare levels (improving → +, deteriorating → -, unchanged → =).

    Returns None when fewer than 6 years of net income are available — the trend
    is a contextual supplement, not part of the numeric RG (paper §3.7).
    """
    years = sorted(net_income_by_year, reverse=True)
    if len(years) < TREND_WINDOW:
        return None
    recent = [net_income_by_year[y] for y in years[:3]]
    older = [net_income_by_year[y] for y in years[3:6]]
    b_new = sum(recent) / 3.0
    b_old = sum(older) / 3.0

    if b_old > 0:
        change = b_new / b_old - 1.0
        if change > 0.25:
            return "++"
        if change > 0.05:
            return "+"
        if change >= -0.05:
            return "="
        if change >= -0.25:
            return "-"
        return "--"

    # B_old ≤ 0: percentage comparison undefined — use level direction.
    if b_new > 0:
        return "++"  # non-positive → positive earnings: strong turnaround
    if b_new > b_old:
        return "+"   # less negative
    if b_new < b_old:
        return "-"    # more negative
    return "="


# ---------------------------------------------------------------------------
# Top-level assembly
# ---------------------------------------------------------------------------

@dataclass
class RGComponents:
    """The fully-decomposed coverage breakdown for one (ticker, N) computation,
    so the UI can show how RG reconciles: MC / (TE + E)."""
    market_cap: float
    book_equity: float
    goodwill: float
    intangibles: float
    tangible_equity: float
    smoothed_earnings: float
    years_used: int
    capitalized_earnings: float
    fundamental_base: float


@dataclass
class RGResult:
    n: int
    value: float                  # may be math.inf (not fundamentally covered)
    trend: Optional[str]
    components: RGComponents
    covered: bool = field(init=False)

    def __post_init__(self) -> None:
        self.covered = math.isfinite(self.value)


def compute_rg(
    market_cap: float,
    book_equity: float,
    goodwill: float,
    intangibles: float,
    net_income_by_year: dict[int, float],
    cpi_by_year: Optional[dict[int, float]] = None,
    n: int = DEFAULT_N,
    window: int = 10,
) -> RGResult:
    """Compute a single RG result for capitalization factor `n`.

    `cpi_by_year` maps fiscal year → CPI index level; pass None to use nominal
    (unadjusted) net income. `window` is the smoothing horizon (10 for RG10).
    """
    te = tangible_equity(book_equity, goodwill, intangibles)
    g, years_used = smoothed_earnings(net_income_by_year, cpi_by_year, window)
    cap_e = capitalized_earnings(g, n)
    fb = fundamental_base(te, cap_e)
    value = reality_gap(market_cap, fb)
    components = RGComponents(
        market_cap=market_cap,
        book_equity=book_equity,
        goodwill=goodwill,
        intangibles=intangibles,
        tangible_equity=te,
        smoothed_earnings=g,
        years_used=years_used,
        capitalized_earnings=cap_e,
        fundamental_base=fb,
    )
    return RGResult(n=n, value=value, trend=earnings_trend(net_income_by_year), components=components)


def compute_rg_sensitivity(
    market_cap: float,
    book_equity: float,
    goodwill: float,
    intangibles: float,
    net_income_by_year: dict[int, float],
    cpi_by_year: Optional[dict[int, float]] = None,
    ns: tuple[int, ...] = SENSITIVITY_NS,
    window: int = 10,
) -> dict[int, RGResult]:
    """Compute RG for each capitalization factor in `ns` (default 8/10/12), the
    paper's standard reporting form (§3.8). Returns {N: RGResult}."""
    return {
        n: compute_rg(
            market_cap, book_equity, goodwill, intangibles,
            net_income_by_year, cpi_by_year, n=n, window=window,
        )
        for n in ns
    }


def format_rg(result: RGResult, short_window: bool = False) -> str:
    """Render the paper's compact form (§3.8): ``RG10 11.3-`` or, when fewer
    than `window` years fed the average, ``RG10* 11.3-``. Not-covered renders
    as ``RG10 ∞``."""
    star = "*" if short_window else ""
    value = "∞" if math.isinf(result.value) else f"{result.value:.1f}"
    trend = result.trend or ""
    return f"RG{result.n}{star} {value}{trend}"
