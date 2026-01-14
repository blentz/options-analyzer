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


# Black-Scholes helper functions for assignment probability estimation
def _norm_cdf(x: float) -> float:
    """Standard normal cumulative distribution function."""
    return (1.0 + math.erf(x / math.sqrt(2.0))) / 2.0


def _estimate_delta(
    option_type: str,
    strike: float,
    current_price: float,
    days_to_expiry: int,
    risk_free_rate: float = 0.05,
    volatility: float = 0.30
) -> float:
    """
    Estimate option delta using Black-Scholes approximation.
    Delta represents the probability the option will expire ITM.
    
    For short options:
    - Short put delta: probability of assignment (put ending ITM)
    - Short call delta: probability of assignment (call ending ITM)
    """
    if days_to_expiry <= 0:
        # At expiration, delta is binary
        if option_type == "CALL":
            return 1.0 if current_price > strike else 0.0
        else:
            return 1.0 if current_price < strike else 0.0
    
    T = days_to_expiry / 365.0
    
    # Avoid log of zero or negative
    if current_price <= 0 or strike <= 0:
        return 0.5
    
    try:
        d1 = (math.log(current_price / strike) + (risk_free_rate + 0.5 * volatility ** 2) * T) / (volatility * math.sqrt(T))
        
        if option_type == "CALL":
            return _norm_cdf(d1)
        else:  # PUT
            return _norm_cdf(-d1)  # Probability of put being ITM
    except (ValueError, ZeroDivisionError):
        return 0.5


def calculate_price_at_delta(
    option_type: str,
    strike: float,
    days_to_expiry: int,
    target_delta: float = 0.5,
    risk_free_rate: float = 0.05,
    volatility: float = 0.30
) -> Optional[float]:
    """
    Calculate the underlying price where delta equals target value.
    For puts, this is where assignment probability = target_delta.
    For calls, this is where the call would have target_delta ITM probability.
    
    Returns the price at which assignment probability equals target_delta.
    """
    if days_to_expiry <= 0:
        # At expiry, the 50% point is exactly at strike
        return strike
    
    T = days_to_expiry / 365.0
    
    try:
        # Solve for S where N(d1) = target_delta for calls
        # or N(-d1) = target_delta for puts
        from scipy.stats import norm
        
        if option_type == "PUT":
            # For puts: N(-d1) = target_delta, so -d1 = norm.ppf(target_delta)
            d1 = -norm.ppf(target_delta)
        else:
            # For calls: N(d1) = target_delta
            d1 = norm.ppf(target_delta)
        
        # d1 = (ln(S/K) + (r + σ²/2)T) / (σ√T)
        # Solving for S:
        # ln(S/K) = d1 * σ√T - (r + σ²/2)T
        # S = K * exp(d1 * σ√T - (r + σ²/2)T)
        exponent = d1 * volatility * math.sqrt(T) - (risk_free_rate + 0.5 * volatility ** 2) * T
        price = strike * math.exp(exponent)
        return price
    except ImportError:
        # Fallback without scipy - use approximation
        # At 50% delta, price is approximately at strike adjusted for drift
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
    price_lower_bound: Optional[float] = None  # Lower bound of price estimate (1.5 sigma)
    price_upper_bound: Optional[float] = None  # Upper bound of price estimate (1.5 sigma)


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

    For SHORT positions (sold options):
    - Premium is positive (collected)
    - P&L = Premium - Intrinsic Value at Expiry

    For LONG positions (bought options):
    - Premium is negative (paid)
    - P&L = Intrinsic Value at Expiry + Premium (which is negative)
    """
    multiplier = 100 * num_contracts  # Standard options contract

    # Calculate intrinsic value at expiry
    if option_type == "CALL":
        intrinsic = max(0, underlying_price - strike)
    else:  # PUT
        intrinsic = max(0, strike - underlying_price)

    intrinsic_total = intrinsic * multiplier

    if "SHORT" in strategy:
        # Short: we collected premium, we owe intrinsic value
        pnl = premium - intrinsic_total
    else:
        # Long: we paid premium (negative), we receive intrinsic value
        pnl = intrinsic_total + premium

    return pnl


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
    num_points: int = 21
) -> list[PriceScenario]:
    """Generate P&L scenarios across a range of underlying prices."""
    scenarios = []

    # Generate price range: +/- 30% from strike
    min_price = strike * 0.7
    max_price = strike * 1.3
    step = (max_price - min_price) / (num_points - 1)

    # Calculate max risk for percentage calculation
    if "SHORT" in strategy:
        if option_type == "PUT":
            max_risk = strike * 100 * num_contracts  # Max loss if stock goes to 0
        else:
            max_risk = abs(premium) if premium < 0 else premium * 10  # Calls have unlimited risk
    else:
        max_risk = abs(premium)  # Long options max loss is premium paid

    if max_risk == 0:
        max_risk = 1  # Avoid division by zero

    for i in range(num_points):
        price = min_price + (step * i)
        pnl = calculate_option_pnl_at_expiry(
            option_type, strategy, strike, premium, price, num_contracts
        )
        pnl_percent = (pnl / max_risk) * 100

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
    volatility: float = 0.30
) -> tuple[float, float, float]:
    """
    Estimate the underlying price where option would trade at target value.
    Uses delta approximation with volatility-based confidence interval.
    
    For a short put to be worth 50% of original premium, the stock needs to move
    favorably (up for puts, down for calls).
    
    Returns: (estimated_price, lower_bound_1_5_sigma, upper_bound_1_5_sigma)
    """
    if current_option_value <= 0 or days_to_expiry <= 0:
        return (current_price, current_price, current_price)
    
    # Get current delta to estimate sensitivity
    delta = _estimate_delta(option_type, strike, current_price, days_to_expiry, volatility=volatility)
    
    # Delta represents dOption/dUnderlying
    # For puts: delta is negative (option loses value as stock rises)
    # For calls: delta is positive (option gains value as stock rises)
    
    # We want to find price where option_value = target_option_value
    # dOption = delta * dPrice
    # target - current = delta * (new_price - current_price)
    # new_price = current_price + (target - current) / delta
    
    value_change_needed = target_option_value - current_option_value
    
    if abs(delta) < 0.01:
        # Very low delta - option is deep OTM, price change has little effect
        # Return current price with wide confidence interval
        T = days_to_expiry / 365.0
        price_volatility = current_price * volatility * math.sqrt(T)
        return (current_price, current_price - 1.5 * price_volatility, current_price + 1.5 * price_volatility)
    
    # For puts (negative delta), if we want option to decrease in value (target < current),
    # value_change_needed is negative, delta is negative, so price change is positive (stock up)
    price_change_needed = value_change_needed / delta / 100  # per-share delta
    estimated_price = current_price + price_change_needed
    
    # Calculate confidence interval based on volatility
    T = days_to_expiry / 365.0
    price_volatility = current_price * volatility * math.sqrt(T)
    
    # 1.5 sigma confidence interval (~87% confidence)
    sigma_1_5 = 1.5 * price_volatility
    
    # The direction of the interval depends on option type
    if option_type == "PUT":
        # For puts, favorable move is UP, so lower bound is less favorable
        lower_bound = estimated_price - sigma_1_5
        upper_bound = estimated_price + sigma_1_5
    else:
        # For calls, favorable move is UP for long, DOWN for short
        lower_bound = estimated_price - sigma_1_5
        upper_bound = estimated_price + sigma_1_5
    
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
    volatility: float = 0.30
) -> list[ExitScenario]:
    """
    Generate P&L for different exit conditions:
    1. Option expires worthless (max profit for short positions)
    2. Close at 50% premium with estimated underlying price range
    3. Pre-expiration assignment at current price and breakeven
    """
    scenarios = []
    multiplier = 100 * num_contracts
    premium_per_share = premium / multiplier if multiplier > 0 else 0
    
    # Calculate max risk for percentage calculation
    if "SHORT" in strategy:
        if option_type == "PUT":
            max_risk = strike * multiplier  # Max loss if stock goes to 0
        else:
            max_risk = premium * 10 if premium > 0 else abs(premium)  # Calls have unlimited risk
    else:
        max_risk = abs(premium)  # Long options max loss is premium paid
    
    if max_risk == 0:
        max_risk = 1

    # Scenario 1: Option expires worthless (OTM at expiration)
    if "SHORT" in strategy:
        expire_worthless_pnl = premium
        expire_worthless_desc = "Option expires OTM - keep full premium"
    else:
        expire_worthless_pnl = premium  # premium is negative for long
        expire_worthless_desc = "Option expires OTM - lose full premium"
    
    scenarios.append(ExitScenario(
        name="Expire Worthless",
        description=expire_worthless_desc,
        pnl=round(expire_worthless_pnl, 2),
        pnl_percent=round((expire_worthless_pnl / max_risk) * 100, 2),
        underlying_price=None,
        probability=0.0 if "SHORT" in strategy else None
    ))

    # Scenario 2: Close at 50% premium with underlying price estimate
    if "SHORT" in strategy and premium > 0 and current_price is not None:
        pct = 0.50
        close_cost = premium * pct  # Cost to buy back (50% of collected)
        close_pnl = premium - close_cost  # Keep the other 50%
        
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
        
        # Calculate probability of reaching that price (how likely is it to be achievable)
        # For a short put, we want stock to go UP to est_price
        # Probability = 1 - N(d2) where d2 uses est_price as target
        if days_to_expiry > 0:
            T = days_to_expiry / 365.0
            try:
                d2 = (math.log(current_price / est_price) + (0.05 - 0.5 * volatility ** 2) * T) / (volatility * math.sqrt(T))
                if option_type == "PUT":
                    # For puts, favorable is stock going UP past est_price
                    prob_achievable = _norm_cdf(d2)  # Prob stock > est_price
                else:
                    # For calls, favorable is stock going DOWN past est_price (for short calls)
                    prob_achievable = 1 - _norm_cdf(d2)  # Prob stock < est_price
            except (ValueError, ZeroDivisionError):
                prob_achievable = 0.5
        else:
            prob_achievable = 0.5
        
        # Build description with price estimate
        desc_parts = [f"Buy to close at ${close_cost/multiplier:.2f}/share (50% of premium)"]
        if est_price != current_price:
            desc_parts.append(f"Est. underlying: ${est_price:.2f} (range: ${lower_bound:.2f}-${upper_bound:.2f})")
        
        scenarios.append(ExitScenario(
            name="Close at 50% Premium",
            description=" | ".join(desc_parts),
            pnl=round(close_pnl, 2),
            pnl_percent=round((close_pnl / max_risk) * 100, 2),
            underlying_price=round(est_price, 2),
            probability=round(prob_achievable * 100, 1),
            price_lower_bound=round(lower_bound, 2),
            price_upper_bound=round(upper_bound, 2)
        ))
    
    # Custom close price if provided
    if custom_close_price is not None:
        custom_close_cost = custom_close_price * multiplier
        if "SHORT" in strategy:
            custom_pnl = premium - custom_close_cost
            custom_desc = f"Buy to close at ${custom_close_price:.2f}/share"
        else:
            custom_pnl = custom_close_cost + premium  # premium is negative
            custom_desc = f"Sell to close at ${custom_close_price:.2f}/share"
        
        scenarios.append(ExitScenario(
            name="Custom Close",
            description=custom_desc,
            pnl=round(custom_pnl, 2),
            pnl_percent=round((custom_pnl / max_risk) * 100, 2),
            underlying_price=None,
            probability=None
        ))

    # Scenario 3: Assignment scenarios (current price and breakeven only - not strike since it's always 50%)
    if "SHORT" in strategy and current_price is not None:
        # Assignment at current price
        assignment_pnl_current = calculate_option_pnl_at_expiry(
            option_type, strategy, strike, premium, current_price, num_contracts
        )
        
        assign_prob_current = _estimate_delta(
            option_type, strike, current_price, days_to_expiry
        ) if days_to_expiry > 0 else (1.0 if (
            (option_type == "PUT" and current_price < strike) or
            (option_type == "CALL" and current_price > strike)
        ) else 0.0)
        
        scenarios.append(ExitScenario(
            name="Assignment at Current",
            description=f"Assigned at current price ${current_price:.2f}",
            pnl=round(assignment_pnl_current, 2),
            pnl_percent=round((assignment_pnl_current / max_risk) * 100, 2),
            underlying_price=current_price,
            probability=round(assign_prob_current * 100, 1)
        ))
        
        # Assignment at breakeven (skip "Assignment at Strike" since it's always ~50% by definition)
        if option_type == "PUT":
            breakeven = strike - (premium / multiplier)
        else:
            breakeven = strike + (premium / multiplier)
        
        assignment_pnl_breakeven = calculate_option_pnl_at_expiry(
            option_type, strategy, strike, premium, breakeven, num_contracts
        )
        
        assign_prob_breakeven = _estimate_delta(
            option_type, strike, breakeven, days_to_expiry
        ) if days_to_expiry > 0 else 0.5
        
        scenarios.append(ExitScenario(
            name="Assignment at Breakeven",
            description=f"Assigned at breakeven ${breakeven:.2f} (P&L = $0)",
            pnl=round(assignment_pnl_breakeven, 2),
            pnl_percent=round((assignment_pnl_breakeven / max_risk) * 100, 2),
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
    volatility: float = 0.30
) -> dict:
    """
    Calculate P&L and probability for closing at a specific option price.
    
    Returns dict with:
    - pnl: The profit/loss from closing
    - pnl_percent: Return on risk
    - estimated_underlying: Underlying price where this option price is expected
    - probability: Probability of underlying reaching favorable price before expiry
    - price_lower_bound: Lower 1.5σ bound
    - price_upper_bound: Upper 1.5σ bound
    """
    multiplier = 100 * num_contracts
    premium_per_share = premium / multiplier if multiplier > 0 else 0
    
    # Use provided current option price or estimate from premium
    if current_option_price is None:
        current_option_price = premium_per_share
    
    # Calculate P&L
    pnl = calculate_close_pnl(strategy, premium, close_price_per_share, num_contracts)
    
    # Calculate max risk for percentage
    if "SHORT" in strategy:
        if option_type == "PUT":
            max_risk = strike * multiplier
        else:
            max_risk = premium * 10 if premium > 0 else abs(premium)
    else:
        max_risk = abs(premium)
    
    pnl_percent = (pnl / max_risk) * 100 if max_risk else 0
    
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
    
    # Calculate probability of reaching favorable price
    # For short options, favorable means option value decreasing
    if days_to_expiry > 0:
        T = days_to_expiry / 365.0
        try:
            d2 = (math.log(current_price / est_price) + (0.05 - 0.5 * volatility ** 2) * T) / (volatility * math.sqrt(T))
            
            if "SHORT" in strategy:
                if option_type == "PUT":
                    # Short put: want stock UP (above est_price) for option to decrease
                    probability = _norm_cdf(d2) * 100  # P(S > est_price)
                else:
                    # Short call: want stock DOWN (below est_price) for option to decrease
                    probability = (1 - _norm_cdf(d2)) * 100  # P(S < est_price)
            else:
                if option_type == "PUT":
                    # Long put: want stock DOWN for option to increase in value
                    probability = (1 - _norm_cdf(d2)) * 100
                else:
                    # Long call: want stock UP for option to increase in value
                    probability = _norm_cdf(d2) * 100
        except (ValueError, ZeroDivisionError):
            probability = 50.0
    else:
        probability = 50.0
    
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
    volatility: float = 0.30
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
    if "SHORT" in strategy:
        if option_type == "PUT":
            max_risk = strike * multiplier
        else:
            max_risk = premium * 10 if premium > 0 else abs(premium)
    else:
        max_risk = abs(premium)
    
    pnl_percent = (pnl / max_risk) * 100 if max_risk else 0
    
    # Assignment probability at this price (delta)
    assignment_prob = _estimate_delta(option_type, strike, assignment_price, days_to_expiry, volatility=volatility)
    
    # Probability of underlying reaching this price before expiry
    # Use lognormal distribution to calculate P(S_T > X) or P(S_T < X)
    if days_to_expiry > 0:
        T = days_to_expiry / 365.0
        try:
            # d2 for probability calculation
            d2 = (math.log(current_price / assignment_price) + (0.05 - 0.5 * volatility ** 2) * T) / (volatility * math.sqrt(T))
            
            if assignment_price > current_price:
                # Target is above current - probability of going up
                price_probability = _norm_cdf(d2) * 100  # P(S > assignment_price)
            else:
                # Target is below current - probability of going down
                price_probability = (1 - _norm_cdf(d2)) * 100  # P(S < assignment_price)
        except (ValueError, ZeroDivisionError):
            price_probability = 50.0
    else:
        # At expiry
        price_probability = 100.0 if assignment_price == current_price else 0.0
    
    # Calculate 1σ price range to give context
    if days_to_expiry > 0:
        T = days_to_expiry / 365.0
        sigma_move = current_price * volatility * math.sqrt(T)
        expected_low = current_price - sigma_move
        expected_high = current_price + sigma_move
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
    current_quote: Optional[dict] = None
) -> OpenPositionAnalysis:
    """Analyze a single open position."""
    contract = position.contract

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
    premium_per_share = premium / (100 * num_contracts) if num_contracts > 0 else 0

    # Calculate breakeven
    breakeven = calculate_breakeven(option_type, strategy, strike, abs(premium_per_share))

    # Calculate max profit/loss
    if "SHORT" in strategy:
        max_profit = premium  # Max profit is premium received
        if option_type == "PUT":
            max_loss = (strike * 100 * num_contracts) - premium  # Stock goes to 0
        else:
            max_loss = float('inf')  # Short calls have unlimited risk
    else:
        max_profit = float('inf') if option_type == "CALL" else (strike * 100 * num_contracts) + premium
        max_loss = abs(premium)  # Max loss is premium paid

    # Generate scenarios
    scenarios = generate_price_scenarios(
        option_type, strategy, strike, premium, num_contracts
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
        
        # Calculate assignment probability using delta approximation
        if "SHORT" in strategy:
            assignment_probability = _estimate_delta(
                option_type, strike, current_price, days_to_expiry
            )
            assignment_probability = round(assignment_probability * 100, 1)  # Convert to percentage

    # Calculate price at 50% assignment probability
    if "SHORT" in strategy:
        price_at_50pct = calculate_price_at_delta(
            option_type, strike, days_to_expiry, target_delta=0.5
        )
        if price_at_50pct:
            price_at_50pct = round(price_at_50pct, 2)
    
    # Generate exit scenarios
    exit_scenarios = generate_exit_scenarios(
        option_type=option_type,
        strategy=strategy,
        strike=strike,
        premium=premium,
        num_contracts=num_contracts,
        current_price=current_price,
        days_to_expiry=days_to_expiry
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
        price_at_50pct_assignment=price_at_50pct
    )


async def get_open_positions_analysis(db: AsyncSession) -> list[OpenPositionAnalysis]:
    """Get risk analysis for all open positions that haven't expired."""
    from datetime import date as date_type
    from app.services.price_service import get_multiple_prices

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

    # Analyze each position with current price data
    analyses = []
    for position in valid_positions:
        quote = quotes.get(position.contract.symbol)
        analysis = await analyze_open_position(db, position, quote)
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
