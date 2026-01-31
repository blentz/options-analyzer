"""Risk analysis service for open options positions."""

import math
from dataclasses import dataclass, field
from datetime import datetime, date
from decimal import Decimal
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import OptionPosition, OptionContract


# Constants for Black-Scholes calculations
# Using 365 calendar days for time to expiry (industry standard for options)
# Note: Some practitioners use 252 trading days, but calendar days is more common
# for listed options since theta decays over weekends too.
CALENDAR_DAYS_PER_YEAR = 365.0
DEFAULT_RISK_FREE_RATE = 0.05
DEFAULT_VOLATILITY = 0.30


# Black-Scholes helper functions for option probability estimation
def _norm_cdf(x: float) -> float:
    """Standard normal cumulative distribution function."""
    return (1.0 + math.erf(x / math.sqrt(2.0))) / 2.0


def _calculate_d1_d2(
    spot: float,
    strike: float,
    time_years: float,
    risk_free_rate: float,
    volatility: float
) -> tuple[float, float]:
    """
    Calculate Black-Scholes d1 and d2 parameters.
    
    Returns (d1, d2) tuple.
    """
    sqrt_t = math.sqrt(time_years)
    d1 = (math.log(spot / strike) + (risk_free_rate + 0.5 * volatility ** 2) * time_years) / (volatility * sqrt_t)
    d2 = d1 - volatility * sqrt_t
    return d1, d2


def calculate_option_price(
    option_type: str,
    spot: float,
    strike: float,
    days_to_expiry: int,
    volatility: float = DEFAULT_VOLATILITY,
    risk_free_rate: float = DEFAULT_RISK_FREE_RATE
) -> float:
    """
    Calculate theoretical Black-Scholes option price.
    
    Args:
        option_type: "CALL" or "PUT"
        spot: Current underlying price
        strike: Strike price
        days_to_expiry: Days until expiration
        volatility: Implied volatility (annualized, as decimal e.g. 0.30 for 30%)
        risk_free_rate: Risk-free interest rate
    
    Returns:
        Theoretical option price per share
    """
    if days_to_expiry <= 0:
        # At expiry, return intrinsic value
        if option_type.upper() == "CALL":
            return max(0, spot - strike)
        else:
            return max(0, strike - spot)
    
    if spot <= 0 or strike <= 0 or volatility <= 0:
        return 0.0
    
    T = days_to_expiry / CALENDAR_DAYS_PER_YEAR
    
    try:
        d1, d2 = _calculate_d1_d2(spot, strike, T, risk_free_rate, volatility)
        
        if option_type.upper() == "CALL":
            # Call = S * N(d1) - K * e^(-rT) * N(d2)
            price = spot * _norm_cdf(d1) - strike * math.exp(-risk_free_rate * T) * _norm_cdf(d2)
        else:
            # Put = K * e^(-rT) * N(-d2) - S * N(-d1)
            price = strike * math.exp(-risk_free_rate * T) * _norm_cdf(-d2) - spot * _norm_cdf(-d1)
        
        return max(0, price)
    except (ValueError, ZeroDivisionError):
        return 0.0


def _calculate_price_probability(
    current_price: float,
    target_price: float,
    days_to_expiry: int,
    volatility: float = DEFAULT_VOLATILITY,
    risk_free_rate: float = DEFAULT_RISK_FREE_RATE,
    want_above: bool = True
) -> float:
    """
    Calculate probability of underlying reaching target price before expiry.
    
    Uses risk-neutral lognormal distribution from Black-Scholes.
    P(S_T > K) = N(d2) where d2 = (ln(S/K) + (r - σ²/2)T) / (σ√T)
    
    Args:
        current_price: Current underlying price
        target_price: Target price to reach
        days_to_expiry: Days until expiration
        volatility: Implied volatility (annualized)
        risk_free_rate: Risk-free interest rate
        want_above: If True, calculates P(S_T > target), else P(S_T < target)
    
    Returns:
        Probability (0 to 1)
    """
    if days_to_expiry <= 0:
        # At expiry, probability is binary
        if want_above:
            return 1.0 if current_price > target_price else 0.0
        else:
            return 1.0 if current_price < target_price else 0.0
    
    if current_price <= 0 or target_price <= 0:
        return 0.5
    
    T = days_to_expiry / CALENDAR_DAYS_PER_YEAR
    
    try:
        # d2 for probability calculation: P(S_T > K) = N(d2)
        # Note: This is different from delta's d1
        _, d2 = _calculate_d1_d2(current_price, target_price, T, risk_free_rate, volatility)
        
        if want_above:
            return _norm_cdf(d2)  # P(S_T > target)
        else:
            return _norm_cdf(-d2)  # P(S_T < target) = 1 - N(d2) = N(-d2)
    except (ValueError, ZeroDivisionError):
        return 0.5


def _estimate_delta(
    option_type: str,
    strike: float,
    current_price: float,
    days_to_expiry: int,
    risk_free_rate: float = DEFAULT_RISK_FREE_RATE,
    volatility: float = DEFAULT_VOLATILITY
) -> float:
    """
    Estimate option delta using Black-Scholes formula.
    
    IMPORTANT: Delta is NOT exactly the probability of expiring ITM.
    - Call delta = N(d1), but P(ITM) = N(d2) under risk-neutral measure
    - Put delta = N(d1) - 1, but P(ITM) = N(-d2)
    
    However, delta is commonly used as a rough proxy for ITM probability
    because d1 and d2 are close when volatility*sqrt(T) is small.
    
    For assignment risk estimation, this approximation is acceptable.
    
    Returns:
        float: Absolute delta value (0 to 1) representing approximate ITM probability
    """
    if days_to_expiry <= 0:
        # At expiration, ITM probability is binary
        if option_type == "CALL":
            return 1.0 if current_price > strike else 0.0
        else:
            return 1.0 if current_price < strike else 0.0
    
    T = days_to_expiry / CALENDAR_DAYS_PER_YEAR
    
    # Avoid log of zero or negative
    if current_price <= 0 or strike <= 0:
        return 0.5
    
    try:
        d1, d2 = _calculate_d1_d2(current_price, strike, T, risk_free_rate, volatility)
        
        # Return approximate ITM probability using N(d2) for more accuracy
        # N(d2) is the risk-neutral probability of expiring ITM
        if option_type == "CALL":
            return _norm_cdf(d2)  # P(S_T > K) under risk-neutral measure
        else:  # PUT
            return _norm_cdf(-d2)  # P(S_T < K) = 1 - N(d2) = N(-d2)
    except (ValueError, ZeroDivisionError):
        return 0.5


def calculate_price_at_delta(
    option_type: str,
    strike: float,
    days_to_expiry: int,
    target_delta: float = 0.5,
    risk_free_rate: float = DEFAULT_RISK_FREE_RATE,
    volatility: float = DEFAULT_VOLATILITY
) -> Optional[float]:
    """
    Calculate the underlying price where assignment probability equals target value.
    
    Uses d2 (not d1) for consistency with _estimate_delta() which calculates
    assignment probability as N(d2) for calls and N(-d2) for puts.
    
    For puts: We want N(-d2) = target_delta, so solve for S where d2 = -norm.ppf(target_delta)
    For calls: We want N(d2) = target_delta, so solve for S where d2 = norm.ppf(target_delta)
    
    Returns the price at which assignment probability equals target_delta.
    """
    if days_to_expiry <= 0:
        # At expiry, the 50% point is exactly at strike
        return strike
    
    T = days_to_expiry / CALENDAR_DAYS_PER_YEAR
    
    try:
        from scipy.stats import norm
        
        # Solve for S where assignment probability = target_delta
        # Assignment probability uses d2 (not d1):
        #   - Calls: P(ITM) = N(d2), so d2 = norm.ppf(target_delta)
        #   - Puts: P(ITM) = N(-d2), so -d2 = norm.ppf(target_delta), i.e., d2 = -norm.ppf(target_delta)
        
        if option_type == "PUT":
            # For puts: N(-d2) = target_delta, so d2 = -norm.ppf(target_delta)
            d2 = -norm.ppf(target_delta)
        else:
            # For calls: N(d2) = target_delta, so d2 = norm.ppf(target_delta)
            d2 = norm.ppf(target_delta)
        
        # d2 = (ln(S/K) + (r - σ²/2)T) / (σ√T)
        # Solving for S:
        # ln(S/K) = d2 * σ√T - (r - σ²/2)T
        # S = K * exp(d2 * σ√T - (r - σ²/2)T)
        sqrt_T = math.sqrt(T)
        exponent = d2 * volatility * sqrt_T - (risk_free_rate - 0.5 * volatility ** 2) * T
        price = strike * math.exp(exponent)
        return price
    except ImportError:
        # Fallback without scipy - use approximation
        # At 50% probability, price is approximately at strike adjusted for drift
        drift_adjustment = math.exp((risk_free_rate - 0.5 * volatility ** 2) * T)
        return strike * drift_adjustment
    except Exception:
        return strike


@dataclass
class ExitScenario:
    """P&L for a specific exit condition."""
    name: str
    description: str
    pnl: float
    pnl_percent: float  # Return on risk
    underlying_price: Optional[float] = None  # Estimated underlying price for this scenario
    probability: Optional[float] = None  # Probability (assignment prob or achievability)
    price_lower_bound: Optional[float] = None  # Lower bound of price estimate (3 sigma)
    price_upper_bound: Optional[float] = None  # Upper bound of price estimate (3 sigma)


@dataclass
class PriceScenario:
    """P&L at a specific underlying price."""
    underlying_price: float
    pnl: float
    pnl_percent: float  # Return on risk/margin


@dataclass
class OpenPositionAnalysis:
    """Complete analysis of an open position."""
    contract_id: str
    symbol: str
    expiration: date
    days_to_expiry: int
    strike: float
    option_type: str  # CALL or PUT
    strategy: str  # SHORT PUT, SHORT CALL, etc.
    quantity: int  # Number of contracts
    premium_received: float  # Premium collected (positive) or paid (negative)

    # Current market data
    current_price: Optional[float]  # Current underlying price from API
    price_change: Optional[float]  # Today's price change
    price_change_percent: Optional[float]  # Today's price change %

    # Risk metrics
    max_profit: float
    max_loss: float  # For short options, this can be very large
    breakeven: float

    # Current P&L if position closed now (at current price)
    current_pnl: Optional[float]
    itm: Optional[bool]  # Is the option in the money?
    distance_to_strike: Optional[float]  # Current price - strike (for calls) or strike - current price (for puts)
    distance_to_strike_pct: Optional[float]  # As percentage

    # P&L scenarios at different prices
    scenarios: list[PriceScenario]
    
    # Exit scenario analysis
    exit_scenarios: list[ExitScenario] = field(default_factory=list)
    
    # Assignment risk metrics
    assignment_probability: Optional[float] = None  # Current probability of assignment
    price_at_50pct_assignment: Optional[float] = None  # Price where assignment prob = 50%
    
    # Live options data from StockNear
    implied_volatility: Optional[float] = None  # Live IV as decimal (e.g., 0.35 = 35%)
    iv_rank: Optional[float] = None  # IV Rank (0-100)
    iv_percentile: Optional[float] = None  # IV Percentile (0-100)
    max_pain: Optional[float] = None  # Max pain price for this expiration
    
    # Calculation details for tooltips
    iv_source: Optional[str] = None  # Where IV came from: "contract", "symbol", "default"
    calc_details: Optional[dict] = None  # Contains d1, d2, T, etc. for tooltip display


def calculate_option_pnl_at_expiry(
    option_type: str,
    strategy: str,
    strike: float,
    premium: float,
    underlying_price: float,
    num_contracts: int = 1
) -> float:
    """
    Calculate P&L at expiration for a given underlying price.

    Premium convention:
    - For SHORT positions: premium should be POSITIVE (amount collected)
    - For LONG positions: premium should be NEGATIVE (amount paid)
    
    The function uses abs(premium) internally to handle both conventions safely.
    
    Returns:
        P&L in dollars (positive = profit, negative = loss)
    """
    multiplier = 100 * num_contracts  # Standard options contract

    # Calculate intrinsic value at expiry
    if option_type == "CALL":
        intrinsic = max(0, underlying_price - strike)
    else:  # PUT
        intrinsic = max(0, strike - underlying_price)

    intrinsic_total = intrinsic * multiplier
    premium_abs = abs(premium)

    if "SHORT" in strategy:
        # Short: we collected premium, we owe intrinsic value
        pnl = premium_abs - intrinsic_total
    else:
        # Long: we paid premium, we receive intrinsic value
        pnl = intrinsic_total - premium_abs

    return pnl


def calculate_max_risk(
    option_type: str,
    strategy: str,
    strike: float,
    premium: float,
    num_contracts: int,
    current_price: Optional[float] = None
) -> tuple[float, bool]:
    """
    Calculate the maximum risk (potential loss) for a position.
    
    For short calls, risk is theoretically unlimited. We use a practical
    estimate based on a 3x price move from strike or current price.
    
    Returns:
        tuple: (max_risk_dollars, is_unlimited)
        - max_risk_dollars: Estimated maximum loss in dollars
        - is_unlimited: True if risk is theoretically unlimited (short calls)
    """
    multiplier = 100 * num_contracts
    premium_abs = abs(premium)
    
    if "SHORT" in strategy:
        if option_type == "PUT":
            # Max loss: stock goes to 0, we buy at strike
            max_risk = (strike * multiplier) - premium_abs
            return (max(0, max_risk), False)
        else:  # SHORT CALL
            # Unlimited risk - estimate based on 3x price move
            # Use current price if available, otherwise use strike
            reference_price = current_price if current_price else strike
            # Assume stock could triple (conservative for risk display)
            worst_case_price = reference_price * 3
            max_risk = ((worst_case_price - strike) * multiplier) - premium_abs
            return (max(0, max_risk), True)  # True = unlimited
    else:  # LONG positions
        # Max loss is premium paid
        return (premium_abs, False)


def calculate_breakeven(
    option_type: str,
    strategy: str,
    strike: float,
    premium_per_share: float
) -> float:
    """Calculate breakeven price for the position."""
    if option_type == "CALL":
        if "SHORT" in strategy:
            # Short call: breakeven is strike + premium received
            return strike + premium_per_share
        else:
            # Long call: breakeven is strike + premium paid
            return strike + abs(premium_per_share)
    else:  # PUT
        if "SHORT" in strategy:
            # Short put: breakeven is strike - premium received
            return strike - premium_per_share
        else:
            # Long put: breakeven is strike - premium paid
            return strike - abs(premium_per_share)


def generate_price_scenarios(
    option_type: str,
    strategy: str,
    strike: float,
    premium: float,
    num_contracts: int,
    current_price: Optional[float] = None,
    num_points: int = 21
) -> list[PriceScenario]:
    """Generate P&L scenarios across a range of underlying prices."""
    scenarios = []

    # Generate price range: +/- 30% from strike
    min_price = strike * 0.7
    max_price = strike * 1.3
    step = (max_price - min_price) / (num_points - 1)

    # Calculate max risk for percentage calculation
    max_risk, is_unlimited = calculate_max_risk(
        option_type, strategy, strike, premium, num_contracts, current_price
    )
    
    # If max_risk is 0 or very small, return None for percentages
    use_percentages = max_risk > 0.01

    for i in range(num_points):
        price = min_price + (step * i)
        pnl = calculate_option_pnl_at_expiry(
            option_type, strategy, strike, premium, price, num_contracts
        )
        
        if use_percentages:
            pnl_percent = (pnl / max_risk) * 100
        else:
            pnl_percent = 0.0  # Can't calculate meaningful percentage

        scenarios.append(PriceScenario(
            underlying_price=round(price, 2),
            pnl=round(pnl, 2),
            pnl_percent=round(pnl_percent, 2)
        ))

    return scenarios


def estimate_underlying_for_option_value(
    option_type: str,
    strike: float,
    current_price: float,
    current_option_value: float,
    target_option_value: float,
    days_to_expiry: int,
    volatility: float = DEFAULT_VOLATILITY
) -> tuple[float, float, float]:
    """
    Estimate the underlying price where option would trade at target value.
    Uses delta approximation with volatility-based confidence interval.
    
    Note: This is a linear approximation using delta. It becomes less accurate
    for large price moves due to gamma (delta changes as price moves).
    
    Args:
        option_type: "CALL" or "PUT"
        strike: Option strike price
        current_price: Current underlying price
        current_option_value: Current option value per share
        target_option_value: Target option value per share
        days_to_expiry: Days until expiration
        volatility: Implied volatility (annualized)
    
    Returns: (estimated_price, lower_bound_3sigma, upper_bound_3sigma)
    """
    if current_option_value <= 0 or days_to_expiry <= 0:
        return (current_price, current_price, current_price)
    
    # Get current delta to estimate sensitivity
    # Delta is already per-share (dOption_price / dUnderlying_price)
    delta = _estimate_delta(option_type, strike, current_price, days_to_expiry, volatility=volatility)
    
    # Adjust delta sign for puts (our _estimate_delta returns absolute probability)
    if option_type == "PUT":
        # For puts, option value decreases as stock price increases
        effective_delta = -delta  # Negative delta for puts
    else:
        effective_delta = delta  # Positive delta for calls
    
    value_change_needed = target_option_value - current_option_value
    
    if abs(effective_delta) < 0.01:
        # Very low delta - option is deep OTM, price change has little effect
        # Return current price with wide confidence interval using lognormal bounds
        T = days_to_expiry / CALENDAR_DAYS_PER_YEAR
        drift = (DEFAULT_RISK_FREE_RATE - 0.5 * volatility ** 2) * T
        lower_bound = current_price * math.exp(drift - 3.0 * volatility * math.sqrt(T))
        upper_bound = current_price * math.exp(drift + 3.0 * volatility * math.sqrt(T))
        return (current_price, lower_bound, upper_bound)
    
    # Delta approximation: dOption = delta * dUnderlying
    # new_price = current_price + value_change_needed / delta
    # Note: No division by 100 - delta is already per-share
    price_change_needed = value_change_needed / effective_delta
    estimated_price = current_price + price_change_needed
    
    # Calculate confidence interval using lognormal distribution
    # Stock prices follow: S_T = S_0 * exp((r - σ²/2)T + σ√T * Z)
    # 3σ bounds give ~99.7% confidence interval
    T = days_to_expiry / CALENDAR_DAYS_PER_YEAR
    drift = (DEFAULT_RISK_FREE_RATE - 0.5 * volatility ** 2) * T
    
    # Apply lognormal bounds centered on the estimated price
    lower_bound = estimated_price * math.exp(drift - 3.0 * volatility * math.sqrt(T))
    upper_bound = estimated_price * math.exp(drift + 3.0 * volatility * math.sqrt(T))
    
    return (max(0, estimated_price), max(0, lower_bound), max(0, upper_bound))


def generate_exit_scenarios(
    option_type: str,
    strategy: str,
    strike: float,
    premium: float,
    num_contracts: int,
    current_price: Optional[float] = None,
    days_to_expiry: int = 0,
    custom_close_price: Optional[float] = None,
    volatility: float = DEFAULT_VOLATILITY
) -> list[ExitScenario]:
    """
    Generate P&L for different exit conditions:
    1. Option expires worthless (max profit for short positions)
    2. Close at 50% premium with estimated underlying price range
    3. Pre-expiration assignment at current price and breakeven
    """
    scenarios = []
    multiplier = 100 * num_contracts
    premium_abs = abs(premium)
    premium_per_share = premium_abs / multiplier if multiplier > 0 else 0
    
    # Calculate max risk for percentage calculation
    max_risk, is_unlimited = calculate_max_risk(
        option_type, strategy, strike, premium, num_contracts, current_price
    )
    
    # Ensure we have a valid denominator for percentages
    use_percentages = max_risk > 0.01

    # Scenario 1: Option expires worthless (OTM at expiration)
    if "SHORT" in strategy:
        expire_worthless_pnl = premium_abs
        expire_worthless_desc = "Option expires OTM - keep full premium"
    else:
        expire_worthless_pnl = -premium_abs  # Loss of premium paid
        expire_worthless_desc = "Option expires OTM - lose full premium"
    
    expire_pct = (expire_worthless_pnl / max_risk) * 100 if use_percentages else 0.0
    scenarios.append(ExitScenario(
        name="Expire Worthless",
        description=expire_worthless_desc,
        pnl=round(expire_worthless_pnl, 2),
        pnl_percent=round(expire_pct, 2),
        underlying_price=None,
        probability=0.0 if "SHORT" in strategy else None
    ))

    # Scenario 2: Close at 50% premium with underlying price estimate
    if "SHORT" in strategy and premium_abs > 0 and current_price is not None:
        pct = 0.50
        close_cost = premium_abs * pct  # Cost to buy back (50% of collected)
        close_pnl = premium_abs - close_cost  # Keep the other 50%
        
        # Estimate underlying price where option would be worth 50% of original
        target_option_value = premium_per_share * pct  # Target option price per share
        
        est_price, lower_bound, upper_bound = estimate_underlying_for_option_value(
            option_type=option_type,
            strike=strike,
            current_price=current_price,
            current_option_value=premium_per_share,
            target_option_value=target_option_value,
            days_to_expiry=days_to_expiry,
            volatility=volatility
        )
        
        # Calculate probability of reaching favorable price using proper d2 formula
        prob_achievable = _calculate_price_probability(
            current_price=current_price,
            target_price=est_price,
            days_to_expiry=days_to_expiry,
            volatility=volatility,
            want_above=(option_type == "PUT")  # Puts want price UP, calls want price DOWN
        )
        
        # Build description with price estimate
        desc_parts = [f"Buy to close at ${close_cost/multiplier:.2f}/share (50% of premium)"]
        if est_price != current_price:
            desc_parts.append(f"Est. underlying: ${est_price:.2f} (range: ${lower_bound:.2f}-${upper_bound:.2f})")
        
        close_pct = (close_pnl / max_risk) * 100 if use_percentages else 0.0
        scenarios.append(ExitScenario(
            name="Close at 50% Premium",
            description=" | ".join(desc_parts),
            pnl=round(close_pnl, 2),
            pnl_percent=round(close_pct, 2),
            underlying_price=round(est_price, 2),
            probability=round(prob_achievable * 100, 1),
            price_lower_bound=round(lower_bound, 2),
            price_upper_bound=round(upper_bound, 2)
        ))
    
    # Custom close price if provided
    if custom_close_price is not None:
        custom_close_cost = custom_close_price * multiplier
        if "SHORT" in strategy:
            custom_pnl = premium_abs - custom_close_cost
            custom_desc = f"Buy to close at ${custom_close_price:.2f}/share"
        else:
            custom_pnl = custom_close_cost - premium_abs
            custom_desc = f"Sell to close at ${custom_close_price:.2f}/share"
        
        custom_pct = (custom_pnl / max_risk) * 100 if use_percentages else 0.0
        scenarios.append(ExitScenario(
            name="Custom Close",
            description=custom_desc,
            pnl=round(custom_pnl, 2),
            pnl_percent=round(custom_pct, 2),
            underlying_price=None,
            probability=None
        ))

    # Scenario 3: Assignment scenarios (current price and breakeven only - not strike since it's always 50%)
    if "SHORT" in strategy and current_price is not None:
        # Assignment at current price
        assignment_pnl_current = calculate_option_pnl_at_expiry(
            option_type, strategy, strike, premium_abs, current_price, num_contracts
        )
        
        assign_prob_current = _estimate_delta(
            option_type, strike, current_price, days_to_expiry
        ) if days_to_expiry > 0 else (1.0 if (
            (option_type == "PUT" and current_price < strike) or
            (option_type == "CALL" and current_price > strike)
        ) else 0.0)
        
        assign_pct = (assignment_pnl_current / max_risk) * 100 if use_percentages else 0.0
        scenarios.append(ExitScenario(
            name="Assignment at Current",
            description=f"Assigned at current price ${current_price:.2f}",
            pnl=round(assignment_pnl_current, 2),
            pnl_percent=round(assign_pct, 2),
            underlying_price=current_price,
            probability=round(assign_prob_current * 100, 1)
        ))
        
        # Assignment at breakeven (skip "Assignment at Strike" since it's always ~50% by definition)
        if option_type == "PUT":
            breakeven = strike - premium_per_share
        else:
            breakeven = strike + premium_per_share
        
        assignment_pnl_breakeven = calculate_option_pnl_at_expiry(
            option_type, strategy, strike, premium_abs, breakeven, num_contracts
        )
        
        assign_prob_breakeven = _estimate_delta(
            option_type, strike, breakeven, days_to_expiry
        ) if days_to_expiry > 0 else 0.5
        
        be_pct = (assignment_pnl_breakeven / max_risk) * 100 if use_percentages else 0.0
        scenarios.append(ExitScenario(
            name="Assignment at Breakeven",
            description=f"Assigned at breakeven ${breakeven:.2f} (P&L = $0)",
            pnl=round(assignment_pnl_breakeven, 2),
            pnl_percent=round(be_pct, 2),
            underlying_price=round(breakeven, 2),
            probability=round(assign_prob_breakeven * 100, 1)
        ))

    return scenarios


def calculate_close_pnl(
    strategy: str,
    premium: float,
    close_price_per_share: float,
    num_contracts: int
) -> float:
    """
    Calculate P&L when closing a position at a specific option price.
    
    Args:
        strategy: Position strategy (SHORT PUT, LONG CALL, etc.)
        premium: Original premium (positive for short, negative for long)
        close_price_per_share: Price per share to close at
        num_contracts: Number of contracts
    
    Returns:
        P&L from closing the position
    """
    multiplier = 100 * num_contracts
    close_cost = close_price_per_share * multiplier
    
    if "SHORT" in strategy:
        # For short positions: we collected premium, now pay to close
        return premium - close_cost
    else:
        # For long positions: we paid premium (negative), now receive from close
        return close_cost + premium


def calculate_close_scenario_with_probability(
    option_type: str,
    strategy: str,
    strike: float,
    premium: float,
    num_contracts: int,
    current_price: float,
    days_to_expiry: int,
    close_price_per_share: float,
    current_option_price: Optional[float] = None,
    volatility: float = DEFAULT_VOLATILITY
) -> dict:
    """
    Calculate P&L and probability for closing at a specific option price.
    
    Returns dict with:
    - pnl: The profit/loss from closing
    - pnl_percent: Return on risk
    - estimated_underlying: Underlying price where this option price is expected
    - probability: Probability of underlying reaching favorable price before expiry
    - price_lower_bound: Lower 3σ bound (lognormal)
    - price_upper_bound: Upper 3σ bound (lognormal)
    """
    multiplier = 100 * num_contracts
    premium_abs = abs(premium)
    premium_per_share = premium_abs / multiplier if multiplier > 0 else 0
    
    # Use provided current option price or estimate from premium
    if current_option_price is None:
        current_option_price = premium_per_share
    
    # Calculate P&L
    pnl = calculate_close_pnl(strategy, premium, close_price_per_share, num_contracts)
    
    # Calculate max risk for percentage
    max_risk, _ = calculate_max_risk(
        option_type, strategy, strike, premium, num_contracts, current_price
    )
    
    pnl_percent = (pnl / max_risk) * 100 if max_risk > 0.01 else 0
    
    # Estimate underlying price where option would trade at close_price_per_share
    est_price, lower_bound, upper_bound = estimate_underlying_for_option_value(
        option_type=option_type,
        strike=strike,
        current_price=current_price,
        current_option_value=current_option_price,
        target_option_value=close_price_per_share,
        days_to_expiry=days_to_expiry,
        volatility=volatility
    )
    
    # Determine if we want price above or below estimated price
    # For short options to decrease in value:
    # - Short put: want stock UP (option decreases)
    # - Short call: want stock DOWN (option decreases)
    if "SHORT" in strategy:
        want_above = (option_type == "PUT")
    else:
        # Long options want favorable price movement to increase value
        want_above = (option_type == "CALL")
    
    probability = _calculate_price_probability(
        current_price=current_price,
        target_price=est_price,
        days_to_expiry=days_to_expiry,
        volatility=volatility,
        want_above=want_above
    ) * 100
    
    return {
        "pnl": round(pnl, 2),
        "pnl_percent": round(pnl_percent, 2),
        "estimated_underlying": round(est_price, 2),
        "probability": round(probability, 1),
        "price_lower_bound": round(lower_bound, 2),
        "price_upper_bound": round(upper_bound, 2),
        "description": f"{'Buy' if 'SHORT' in strategy else 'Sell'} to close at ${close_price_per_share:.2f}/share"
    }


def calculate_assignment_scenario_with_probability(
    option_type: str,
    strategy: str,
    strike: float,
    premium: float,
    num_contracts: int,
    current_price: float,
    days_to_expiry: int,
    assignment_price: float,
    volatility: float = DEFAULT_VOLATILITY
) -> dict:
    """
    Calculate P&L and probability for assignment at a specific underlying price.
    
    Returns dict with:
    - pnl: The profit/loss if assigned at this price
    - pnl_percent: Return on risk
    - assignment_probability: Probability of option being ITM at this price
    - price_probability: Probability of underlying reaching this price before expiry
    """
    multiplier = 100 * num_contracts
    
    # Calculate P&L at assignment price
    pnl = calculate_option_pnl_at_expiry(
        option_type, strategy, strike, premium, assignment_price, num_contracts
    )
    
    # Calculate max risk for percentage
    max_risk, _ = calculate_max_risk(
        option_type, strategy, strike, premium, num_contracts, current_price
    )
    
    pnl_percent = (pnl / max_risk) * 100 if max_risk > 0.01 else 0
    
    # Assignment probability at this price (ITM probability)
    assignment_prob = _estimate_delta(option_type, strike, assignment_price, days_to_expiry, volatility=volatility)
    
    # Probability of underlying reaching assignment_price
    # We want probability of reaching that price, regardless of direction
    want_above = assignment_price > current_price
    price_probability = _calculate_price_probability(
        current_price=current_price,
        target_price=assignment_price,
        days_to_expiry=days_to_expiry,
        volatility=volatility,
        want_above=want_above
    ) * 100
    
    # Calculate 3σ price range using lognormal distribution (~99.7% confidence)
    # Stock prices follow: S_T = S_0 * exp((r - σ²/2)T + σ√T * Z)
    if days_to_expiry > 0:
        T = days_to_expiry / CALENDAR_DAYS_PER_YEAR
        drift = (DEFAULT_RISK_FREE_RATE - 0.5 * volatility ** 2) * T
        expected_low = current_price * math.exp(drift - 3.0 * volatility * math.sqrt(T))
        expected_high = current_price * math.exp(drift + 3.0 * volatility * math.sqrt(T))
    else:
        expected_low = current_price
        expected_high = current_price
    
    return {
        "pnl": round(pnl, 2),
        "pnl_percent": round(pnl_percent, 2),
        "assignment_probability": round(assignment_prob * 100, 1),
        "price_probability": round(price_probability, 1),
        "expected_range_low": round(expected_low, 2),
        "expected_range_high": round(expected_high, 2),
        "description": f"Assignment at ${assignment_price:.2f}"
    }


async def analyze_open_position(
    db: AsyncSession,
    position: OptionPosition,
    current_quote = None,
    live_volatility: Optional[float] = None
) -> OpenPositionAnalysis:
    """
    Analyze a single open position.
    
    Args:
        db: Database session
        position: The option position to analyze
        current_quote: Current stock quote with price data
        live_volatility: Live implied volatility from StockNear (as decimal, e.g. 0.35)
                        If None, falls back to DEFAULT_VOLATILITY (0.30)
    """
    contract = position.contract
    
    # Use live volatility if provided, otherwise default
    volatility = live_volatility if live_volatility else DEFAULT_VOLATILITY

    strike = float(contract.strike)
    option_type = contract.option_type
    strategy = position.strategy
    premium = float(position.total_premium)

    # Estimate number of contracts from the position
    num_contracts = position.num_contracts or 1

    # Days to expiry
    today = date.today()
    days_to_expiry = (contract.expiration - today).days

    # Premium per share (for breakeven calculation)
    premium_abs = abs(premium)
    premium_per_share = premium_abs / (100 * num_contracts) if num_contracts > 0 else 0

    # Calculate breakeven
    breakeven = calculate_breakeven(option_type, strategy, strike, premium_per_share)

    # Get current price if available for max risk calculation
    current_price_for_risk = None
    if current_quote and hasattr(current_quote, 'price'):
        current_price_for_risk = current_quote.price

    # Calculate max profit/loss using the new function
    max_risk, is_unlimited = calculate_max_risk(
        option_type, strategy, strike, premium, num_contracts, current_price_for_risk
    )
    
    if "SHORT" in strategy:
        max_profit = premium_abs  # Max profit is premium received
        max_loss = max_risk
        if is_unlimited:
            max_loss = float('inf')  # Mark as unlimited for display
    else:
        if option_type == "CALL":
            max_profit = float('inf')  # Long calls have unlimited upside
        else:
            max_profit = (strike * 100 * num_contracts) - premium_abs  # Long put max profit if stock goes to 0
        max_loss = premium_abs  # Max loss is premium paid

    # Generate scenarios
    scenarios = generate_price_scenarios(
        option_type, strategy, strike, premium, num_contracts, current_price_for_risk
    )

    # Current price data from quote
    current_price = None
    price_change = None
    price_change_percent = None
    current_pnl = None
    itm = None
    distance_to_strike = None
    distance_to_strike_pct = None
    assignment_probability = None
    price_at_50pct = None

    if current_quote:
        current_price = current_quote.price
        price_change = current_quote.change
        price_change_percent = current_quote.change_percent

        # Calculate current P&L at current price
        current_pnl = calculate_option_pnl_at_expiry(
            option_type, strategy, strike, premium, current_price, num_contracts
        )

        # Determine if in the money
        if option_type == "CALL":
            itm = current_price > strike
            distance_to_strike = current_price - strike
        else:  # PUT
            itm = current_price < strike
            distance_to_strike = strike - current_price

        distance_to_strike_pct = (distance_to_strike / strike) * 100 if strike > 0 else 0
        
        # Calculate assignment probability using delta approximation with live IV
        if "SHORT" in strategy:
            assignment_probability = _estimate_delta(
                option_type, strike, current_price, days_to_expiry, volatility=volatility
            )
            assignment_probability = round(assignment_probability * 100, 1)  # Convert to percentage

    # Calculate price at 50% assignment probability
    calc_details = None
    if "SHORT" in strategy:
        price_at_50pct = calculate_price_at_delta(
            option_type, strike, days_to_expiry, target_delta=0.5, volatility=volatility
        )
        if price_at_50pct:
            price_at_50pct = round(price_at_50pct, 2)
        
        # Build calculation details for tooltip
        if current_price and days_to_expiry > 0:
            T = days_to_expiry / CALENDAR_DAYS_PER_YEAR
            d1, d2 = _calculate_d1_d2(current_price, strike, T, DEFAULT_RISK_FREE_RATE, volatility)
            drift_term = (0.5 * volatility ** 2 - DEFAULT_RISK_FREE_RATE) * T
            
            calc_details = {
                "iv_percent": round(volatility * 100, 1),
                "T_years": round(T, 4),
                "d1": round(d1, 4),
                "d2": round(d2, 4),
                "drift_term": round(drift_term, 4),
                "risk_free_rate": DEFAULT_RISK_FREE_RATE,
            }
    
    # Generate exit scenarios with live volatility
    exit_scenarios = generate_exit_scenarios(
        option_type=option_type,
        strategy=strategy,
        strike=strike,
        premium=premium,
        num_contracts=num_contracts,
        current_price=current_price,
        days_to_expiry=days_to_expiry,
        volatility=volatility
    )

    return OpenPositionAnalysis(
        contract_id=contract.contract_id,
        symbol=contract.symbol,
        expiration=contract.expiration,
        days_to_expiry=days_to_expiry,
        strike=strike,
        option_type=option_type,
        strategy=strategy,
        quantity=num_contracts,
        premium_received=premium,
        current_price=current_price,
        price_change=price_change,
        price_change_percent=price_change_percent,
        max_profit=max_profit if max_profit != float('inf') else 999999,
        max_loss=max_loss if max_loss != float('inf') else -999999,
        breakeven=breakeven,
        current_pnl=current_pnl,
        itm=itm,
        distance_to_strike=distance_to_strike,
        distance_to_strike_pct=distance_to_strike_pct,
        scenarios=scenarios,
        exit_scenarios=exit_scenarios,
        assignment_probability=assignment_probability,
        price_at_50pct_assignment=price_at_50pct,
        implied_volatility=volatility if live_volatility else None,
        calc_details=calc_details
    )


async def get_open_positions_analysis(db: AsyncSession) -> list[OpenPositionAnalysis]:
    """Get risk analysis for all open positions that haven't expired."""
    from datetime import date as date_type
    from app.services.price_service import get_multiple_prices
    from app.services.stocknear_service import get_options_overview, get_contract_quote

    today = date_type.today()

    stmt = select(OptionPosition).options(
        selectinload(OptionPosition.contract)
    ).where(OptionPosition.is_closed == False)

    result = await db.execute(stmt)
    positions = list(result.scalars().all())

    # Filter to valid positions
    valid_positions = []
    for position in positions:
        if position.strategy == "UNKNOWN":
            continue
        if position.contract.expiration < today:
            continue
        valid_positions.append(position)

    if not valid_positions:
        return []

    # Fetch current prices for all unique symbols
    symbols = list(set(p.contract.symbol for p in valid_positions))
    quotes = await get_multiple_prices(symbols)
    
    # Fetch symbol-level options data as fallback
    options_data_by_symbol = {}
    for symbol in symbols:
        try:
            options_data = await get_options_overview(db, symbol)
            if options_data:
                options_data_by_symbol[symbol] = options_data
        except Exception as e:
            print(f"Warning: Could not fetch StockNear data for {symbol}: {e}")

    # Analyze each position with current price data and contract-specific IV
    analyses = []
    for position in valid_positions:
        quote = quotes.get(position.contract.symbol)
        options_data = options_data_by_symbol.get(position.contract.symbol)
        contract = position.contract
        
        # Try to get contract-specific IV first (most accurate)
        live_iv = None
        iv_source = None
        iv_rank = None
        iv_percentile = None
        
        try:
            # Format expiration for StockNear lookup (e.g., "Jul 18, 2025")
            exp_str = contract.expiration.strftime("%b %d, %Y")
            contract_quote = await get_contract_quote(
                db, 
                contract.symbol, 
                exp_str, 
                float(contract.strike), 
                contract.option_type
            )
            if contract_quote and contract_quote.implied_volatility:
                live_iv = contract_quote.implied_volatility
                iv_source = "contract"
        except Exception as e:
            print(f"Warning: Could not fetch contract IV for {contract.contract_id}: {e}")
        
        # Fall back to symbol-level IV if contract-specific not available
        if live_iv is None and options_data:
            live_iv = options_data.implied_volatility
            iv_source = "symbol"
            iv_rank = options_data.iv_rank
            iv_percentile = options_data.iv_percentile
        
        # Fall back to default if neither available
        if live_iv is None:
            iv_source = "default"
        
        analysis = await analyze_open_position(db, position, quote, live_iv)
        
        # Add IV source info and StockNear-specific fields
        analysis.iv_source = iv_source
        if options_data:
            analysis.iv_rank = iv_rank
            analysis.iv_percentile = iv_percentile
        
        analyses.append(analysis)

    return analyses


async def get_portfolio_risk_summary(db: AsyncSession) -> dict:
    """Get aggregate risk metrics for all open positions."""
    analyses = await get_open_positions_analysis(db)

    if not analyses:
        return {
            "total_positions": 0,
            "total_premium": 0,
            "total_max_profit": 0,
            "total_max_loss": 0,
            "total_current_pnl": 0,
            "positions_expiring_soon": 0,
            "positions_itm": 0,
            "analyses": []
        }

    total_premium = sum(a.premium_received for a in analyses)
    total_max_profit = sum(a.max_profit for a in analyses if a.max_profit < 999999)
    total_max_loss = sum(a.max_loss for a in analyses if a.max_loss > -999999)
    total_current_pnl = sum(a.current_pnl for a in analyses if a.current_pnl is not None)
    expiring_soon = sum(1 for a in analyses if a.days_to_expiry <= 7)
    positions_itm = sum(1 for a in analyses if a.itm is True)

    return {
        "total_positions": len(analyses),
        "total_premium": total_premium,
        "total_max_profit": total_max_profit,
        "total_max_loss": total_max_loss,
        "total_current_pnl": total_current_pnl,
        "positions_expiring_soon": expiring_soon,
        "positions_itm": positions_itm,
        "analyses": analyses
    }
