"""Exit-condition analysis: what has to happen for an option to reach a price.

Pure functions over Black-Scholes (no DB, no IO), consumed by the risk
page's Scenario Lab. Given a target option price, three independent levers
can get the contract there, and each gets its own table:

  - spot_path: underlying price needed at each checkpoint (IV held constant)
  - hold_path: when the target is reached if the stock sits at a given level
  - iv_path:   IV needed at each checkpoint with the stock unchanged

build_value_matrix gives the combined view — option value and position
P&L over a spot x time grid.
"""

import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

from app.services.bs_math import (
    CALENDAR_DAYS_PER_YEAR,
    DEFAULT_RISK_FREE_RATE,
    DEFAULT_VOLATILITY,
    _calculate_price_probability,
    calculate_option_price,
    calculate_touch_probability,
    solve_iv_for_option_price,
    solve_spot_for_option_price,
)

# Spread classes where the mid is close enough to a real fill that IV backed
# out of it beats a scraped IV figure. See ContractQuote.spread_quality.
_TRADEABLE_SPREADS = {"tight", "moderate"}


@dataclass
class SpotPathRow:
    days_left: int
    date: date
    spot_needed: Optional[float]
    move_pct: Optional[float]


@dataclass
class HoldRow:
    spot: float
    move_pct: float
    days_left: Optional[int]  # None: never reaches target by expiry
    date: Optional[date]


@dataclass
class IvPathRow:
    days_left: int
    date: date
    iv_needed: Optional[float]  # None: no IV produces the target
    already_met: bool  # current IV already gets there on this day


@dataclass
class TargetConditions:
    target_price: float
    current_model_price: float
    direction: str  # "decrease" or "increase"
    spot_needed_now: Optional[float]
    move_pct: Optional[float]
    prob_finish_beyond: Optional[float]  # P(S_T past spot_needed_now)
    prob_touch: Optional[float]  # P(path touches spot_needed_now before expiry)
    spot_path: list[SpotPathRow] = field(default_factory=list)
    hold_path: list[HoldRow] = field(default_factory=list)
    iv_path: list[IvPathRow] = field(default_factory=list)


@dataclass
class ValueMatrix:
    spots: list[float]  # rows, descending
    days_left: list[int]  # columns, descending, last is 0 (expiry)
    dates: list[date]
    values: list[list[float]]  # option value per share
    pnl: list[list[float]]  # position P&L in dollars


# Checkpoints as fractions of the remaining life. The risk page exposes
# these as editable percentages (grid_pct), so keep the default in one place.
DEFAULT_GRID_FRACTIONS: tuple[float, ...] = (0.75, 0.5, 0.33, 0.2)
MAX_GRID_FRACTIONS = 8


def parse_grid_percents(raw: Optional[str]) -> tuple[float, ...]:
    """Parse a comma list of percents ("75,50,33,20") into fractions, descending.

    Blank means DEFAULT_GRID_FRACTIONS. Raises ValueError on non-numbers,
    values outside (0, 100), or more than MAX_GRID_FRACTIONS entries.
    """
    if raw is None or not raw.strip():
        return DEFAULT_GRID_FRACTIONS
    try:
        percents = {float(p) for p in raw.split(",")}
    except ValueError:
        raise ValueError(f"grid percents must be numbers: {raw!r}")
    if any(not 0 < p < 100 for p in percents):
        raise ValueError("grid percents must be between 0 and 100 (exclusive)")
    if len(percents) > MAX_GRID_FRACTIONS:
        raise ValueError(f"at most {MAX_GRID_FRACTIONS} grid percents")
    return tuple(sorted((p / 100 for p in percents), reverse=True))


def target_days_grid(
    days_to_expiry: int,
    fractions: tuple[float, ...] = DEFAULT_GRID_FRACTIONS,
) -> list[int]:
    """Checkpoints (days left) for the spot and IV paths, descending.

    Mixes fractions of the remaining life with fixed short-dated points:
    fractions keep a 120-DTE position from being all crammed into its last
    week, and the fixed 7/5/3/1 points cover where theta and gamma move
    fastest. Always starts at days_to_expiry; never includes 0 (at expiry
    the answer is just intrinsic value).
    """
    if days_to_expiry <= 0:
        return []
    points = {days_to_expiry}
    points.update(round(days_to_expiry * f) for f in fractions)
    points.update(d for d in (7, 5, 3, 1) if d < days_to_expiry)
    return sorted((d for d in points if 1 <= d <= days_to_expiry), reverse=True)


def select_volatility(
    override: Optional[float],
    contract_iv: Optional[float],
    mid_iv: Optional[float],
    spread_quality: Optional[str],
    symbol_iv: Optional[float],
) -> tuple[float, str]:
    """Pick the IV the lab models with, and name its source.

    Precedence: operator override > IV implied by a tradeable mid > scraped
    contract IV > IV implied by an untradeable mid > symbol IV > default.
    Symbol IV comes last among live sources because it blends every strike
    and expiry — HITI's was 116% while the $2.50P priced at ~68%.
    """
    if override:
        return override, "override"
    if mid_iv and spread_quality in _TRADEABLE_SPREADS:
        return mid_iv, "implied_from_mid"
    if contract_iv:
        return contract_iv, "contract"
    if mid_iv:
        return mid_iv, "implied_from_mid"
    if symbol_iv:
        return symbol_iv, "symbol"
    return DEFAULT_VOLATILITY, "default"


def _pct(new: Optional[float], base: float) -> Optional[float]:
    if new is None or base <= 0:
        return None
    return (new / base - 1.0) * 100.0


def _hold_spots(spot: float, strike: float, spot_needed_now: Optional[float]) -> list[float]:
    levels = {spot, strike, spot * 0.95, spot * 0.975, spot * 1.025, spot * 1.05}
    if spot_needed_now:
        levels.add(spot_needed_now)
    return sorted({round(s, 2) for s in levels if s > 0}, reverse=True)


def build_target_conditions(
    option_type: str,
    strike: float,
    spot: float,
    days_to_expiry: int,
    volatility: float,
    target_price: float,
    today: date,
    risk_free_rate: float = DEFAULT_RISK_FREE_RATE,
    fractions: tuple[float, ...] = DEFAULT_GRID_FRACTIONS,
) -> TargetConditions:
    """Conditions under which the option's model value reaches target_price."""
    def price(s: float, d: float, v: float = volatility) -> float:
        return calculate_option_price(option_type, s, strike, d, v, risk_free_rate)

    current = price(spot, days_to_expiry)
    direction = "decrease" if target_price < current else "increase"
    expiry = today + timedelta(days=days_to_expiry)

    spot_now = solve_spot_for_option_price(
        option_type, strike, target_price, days_to_expiry, volatility, risk_free_rate
    )
    prob_beyond = prob_touch = None
    if spot_now is not None and days_to_expiry > 0:
        want_above = spot_now > spot
        prob_beyond = _calculate_price_probability(
            spot, spot_now, days_to_expiry, volatility, risk_free_rate, want_above=want_above
        )
        prob_touch = calculate_touch_probability(spot, spot_now, days_to_expiry, volatility, risk_free_rate)

    grid = target_days_grid(days_to_expiry, fractions)

    spot_path = []
    for d in grid:
        s = solve_spot_for_option_price(option_type, strike, target_price, d, volatility, risk_free_rate)
        spot_path.append(SpotPathRow(d, expiry - timedelta(days=d), s, _pct(s, spot)))

    iv_path = []
    for d in grid:
        iv = solve_iv_for_option_price(option_type, spot, strike, d, target_price, risk_free_rate)
        met = price(spot, d) <= target_price if direction == "decrease" else price(spot, d) >= target_price
        iv_path.append(IvPathRow(d, expiry - timedelta(days=d), iv, met))

    # Time only helps a falling target: decay pulls value toward intrinsic.
    hold_path = []
    if direction == "decrease":
        for s in _hold_spots(spot, strike, spot_now):
            reached = next((d for d in range(days_to_expiry, -1, -1) if price(s, d) <= target_price), None)
            hold_path.append(HoldRow(
                spot=s,
                move_pct=_pct(s, spot),
                days_left=reached,
                date=expiry - timedelta(days=reached) if reached is not None else None,
            ))

    return TargetConditions(
        target_price=target_price,
        current_model_price=current,
        direction=direction,
        spot_needed_now=spot_now,
        move_pct=_pct(spot_now, spot),
        prob_finish_beyond=prob_beyond,
        prob_touch=prob_touch,
        spot_path=spot_path,
        hold_path=hold_path,
        iv_path=iv_path,
    )


def build_value_matrix(
    option_type: str,
    strike: float,
    spot: float,
    days_to_expiry: int,
    volatility: float,
    strategy: str,
    premium_per_share: float,
    num_contracts: int,
    today: date,
    risk_free_rate: float = DEFAULT_RISK_FREE_RATE,
    rows: int = 9,
    fractions: tuple[float, ...] = DEFAULT_GRID_FRACTIONS,
) -> ValueMatrix:
    """Option value and position P&L over spot (rows) x days left (columns).

    Spot rows span +/-1.5 sigma of the move to expiry, so the grid widens
    with IV and DTE instead of using a fixed percentage.
    """
    T = max(days_to_expiry, 1) / CALENDAR_DAYS_PER_YEAR
    half_width = min(0.9, 1.5 * volatility * math.sqrt(T))
    step = 2 * half_width / (rows - 1)
    spots = [round(spot * (1 + half_width - i * step), 2) for i in range(rows)]

    days = target_days_grid(days_to_expiry, fractions) + [0]
    expiry = today + timedelta(days=days_to_expiry)
    sign = -1.0 if "SHORT" in strategy else 1.0
    multiplier = 100 * num_contracts

    values, pnl = [], []
    for s in spots:
        row_v = [calculate_option_price(option_type, s, strike, d, volatility, risk_free_rate) for d in days]
        values.append([round(v, 4) for v in row_v])
        pnl.append([round(sign * (v - premium_per_share) * multiplier, 2) for v in row_v])

    return ValueMatrix(
        spots=spots,
        days_left=days,
        dates=[expiry - timedelta(days=d) for d in days],
        values=values,
        pnl=pnl,
    )
