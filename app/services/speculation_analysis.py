"""
Options speculation analysis for hypothetical positions.

This module provides analysis for options strategies that the user is considering
but hasn't yet entered. It reuses the core calculations from risk_analysis.py
but works with hypothetical positions instead of database-backed ones.
"""

import math
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

from app.services.risk_analysis import (
    calculate_option_pnl_at_expiry,
    calculate_max_risk,
    calculate_breakeven,
    generate_price_scenarios,
    generate_exit_scenarios,
    _estimate_delta,
    _calculate_price_probability,
    calculate_price_at_delta,
    DEFAULT_VOLATILITY,
    DEFAULT_RISK_FREE_RATE,
    CALENDAR_DAYS_PER_YEAR,
    PriceScenario,
    ExitScenario,
)


@dataclass
class OptionLeg:
    """A single leg of an options strategy."""
    option_type: str  # "CALL" or "PUT"
    strike: float
    expiration: date
    action: str  # "BUY" or "SELL"
    quantity: int = 1
    premium: float = 0.0  # Premium per share (not per contract)
    implied_volatility: Optional[float] = None
    
    @property
    def strategy(self) -> str:
        """Return strategy string like 'LONG CALL' or 'SHORT PUT'."""
        direction = "LONG" if self.action == "BUY" else "SHORT"
        return f"{direction} {self.option_type}"
    
    @property
    def is_long(self) -> bool:
        return self.action == "BUY"
    
    @property
    def is_short(self) -> bool:
        return self.action == "SELL"
    
    @property
    def total_premium(self) -> float:
        """Total premium paid (negative) or received (positive)."""
        base = self.premium * 100 * self.quantity
        return base if self.is_short else -base


@dataclass
class StrategyAnalysis:
    """Analysis results for an options strategy."""
    strategy_name: str
    symbol: str
    legs: list[OptionLeg]
    current_price: float
    
    # Aggregate metrics
    net_premium: float  # Net credit (positive) or debit (negative)
    max_profit: float
    max_loss: float
    breakeven_prices: list[float]  # Can have multiple breakevens
    
    # Days to expiry (uses earliest expiration)
    days_to_expiry: int
    
    # Probability estimates
    profit_probability: Optional[float] = None
    
    # P&L scenarios at different prices
    scenarios: list[PriceScenario] = field(default_factory=list)
    
    # IV data
    implied_volatility: Optional[float] = None
    iv_rank: Optional[float] = None
    
    # Greeks (aggregate)
    delta: Optional[float] = None
    gamma: Optional[float] = None
    theta: Optional[float] = None
    vega: Optional[float] = None


# Common strategy templates
STRATEGY_TEMPLATES = {
    "long_call": {
        "name": "Long Call",
        "legs": [{"action": "BUY", "option_type": "CALL", "strike_offset": 0}],
        "description": "Bullish strategy with unlimited upside, limited downside"
    },
    "long_put": {
        "name": "Long Put",
        "legs": [{"action": "BUY", "option_type": "PUT", "strike_offset": 0}],
        "description": "Bearish strategy, profit if stock falls"
    },
    "short_put": {
        "name": "Short Put (Cash Secured)",
        "legs": [{"action": "SELL", "option_type": "PUT", "strike_offset": 0}],
        "description": "Neutral to bullish, collect premium, risk of assignment"
    },
    "short_call": {
        "name": "Short Call (Naked)",
        "legs": [{"action": "SELL", "option_type": "CALL", "strike_offset": 0}],
        "description": "Bearish to neutral, collect premium, unlimited risk"
    },
    "covered_call": {
        "name": "Covered Call",
        "legs": [{"action": "SELL", "option_type": "CALL", "strike_offset": 0}],
        "description": "Own stock + sell call, collect premium, cap upside",
        "requires_stock": True
    },
    "bull_call_spread": {
        "name": "Bull Call Spread",
        "legs": [
            {"action": "BUY", "option_type": "CALL", "strike_offset": 0},
            {"action": "SELL", "option_type": "CALL", "strike_offset": 1},
        ],
        "description": "Bullish with limited risk and reward"
    },
    "bear_put_spread": {
        "name": "Bear Put Spread",
        "legs": [
            {"action": "BUY", "option_type": "PUT", "strike_offset": 0},
            {"action": "SELL", "option_type": "PUT", "strike_offset": -1},
        ],
        "description": "Bearish with limited risk and reward"
    },
    "bull_put_spread": {
        "name": "Bull Put Spread",
        "legs": [
            {"action": "SELL", "option_type": "PUT", "strike_offset": 0},
            {"action": "BUY", "option_type": "PUT", "strike_offset": -1},
        ],
        "description": "Credit spread, bullish, collect premium"
    },
    "bear_call_spread": {
        "name": "Bear Call Spread",
        "legs": [
            {"action": "SELL", "option_type": "CALL", "strike_offset": 0},
            {"action": "BUY", "option_type": "CALL", "strike_offset": 1},
        ],
        "description": "Credit spread, bearish, collect premium"
    },
    "long_straddle": {
        "name": "Long Straddle",
        "legs": [
            {"action": "BUY", "option_type": "CALL", "strike_offset": 0},
            {"action": "BUY", "option_type": "PUT", "strike_offset": 0},
        ],
        "description": "Profit from big move in either direction"
    },
    "short_straddle": {
        "name": "Short Straddle",
        "legs": [
            {"action": "SELL", "option_type": "CALL", "strike_offset": 0},
            {"action": "SELL", "option_type": "PUT", "strike_offset": 0},
        ],
        "description": "Profit from low volatility, unlimited risk"
    },
    "long_strangle": {
        "name": "Long Strangle",
        "legs": [
            {"action": "BUY", "option_type": "CALL", "strike_offset": 1},
            {"action": "BUY", "option_type": "PUT", "strike_offset": -1},
        ],
        "description": "Cheaper than straddle, needs bigger move"
    },
    "short_strangle": {
        "name": "Short Strangle",
        "legs": [
            {"action": "SELL", "option_type": "CALL", "strike_offset": 1},
            {"action": "SELL", "option_type": "PUT", "strike_offset": -1},
        ],
        "description": "Collect premium, profit if stock stays in range"
    },
    "iron_condor": {
        "name": "Iron Condor",
        "legs": [
            {"action": "SELL", "option_type": "PUT", "strike_offset": -1},
            {"action": "BUY", "option_type": "PUT", "strike_offset": -2},
            {"action": "SELL", "option_type": "CALL", "strike_offset": 1},
            {"action": "BUY", "option_type": "CALL", "strike_offset": 2},
        ],
        "description": "Profit if stock stays in range, defined risk"
    },
    "iron_butterfly": {
        "name": "Iron Butterfly",
        "legs": [
            {"action": "SELL", "option_type": "PUT", "strike_offset": 0},
            {"action": "BUY", "option_type": "PUT", "strike_offset": -1},
            {"action": "SELL", "option_type": "CALL", "strike_offset": 0},
            {"action": "BUY", "option_type": "CALL", "strike_offset": 1},
        ],
        "description": "Max profit at strike, limited risk"
    },
}


def calculate_strategy_pnl_at_price(
    legs: list[OptionLeg],
    underlying_price: float
) -> float:
    """Calculate combined P&L for all legs at a given underlying price."""
    total_pnl = 0.0
    
    for leg in legs:
        # Calculate intrinsic value at expiry
        if leg.option_type == "CALL":
            intrinsic = max(0, underlying_price - leg.strike)
        else:  # PUT
            intrinsic = max(0, leg.strike - underlying_price)
        
        multiplier = 100 * leg.quantity
        intrinsic_total = intrinsic * multiplier
        premium_total = leg.premium * multiplier
        
        if leg.is_short:
            # Short: collected premium, owe intrinsic
            leg_pnl = premium_total - intrinsic_total
        else:
            # Long: paid premium, receive intrinsic
            leg_pnl = intrinsic_total - premium_total
        
        total_pnl += leg_pnl
    
    return total_pnl


def generate_strategy_scenarios(
    legs: list[OptionLeg],
    current_price: float,
    num_points: int = 50
) -> list[PriceScenario]:
    """Generate P&L scenarios across a range of prices for a multi-leg strategy."""
    if not legs:
        return []
    
    # Determine price range based on strikes and current price
    all_strikes = [leg.strike for leg in legs]
    min_strike = min(all_strikes)
    max_strike = max(all_strikes)
    
    # Extend range 20% beyond strikes
    price_range = max_strike - min_strike
    if price_range < current_price * 0.1:
        price_range = current_price * 0.2
    
    low_price = max(0.01, min(min_strike, current_price) - price_range * 0.5)
    high_price = max(max_strike, current_price) + price_range * 0.5
    
    step = (high_price - low_price) / num_points
    
    # Calculate net premium for percentage calculation
    net_premium = sum(leg.total_premium for leg in legs)
    risk_basis = abs(net_premium) if net_premium != 0 else 1
    
    scenarios = []
    for i in range(num_points + 1):
        price = low_price + (i * step)
        pnl = calculate_strategy_pnl_at_price(legs, price)
        pnl_percent = (pnl / risk_basis) * 100 if risk_basis > 0 else 0
        
        scenarios.append(PriceScenario(
            underlying_price=round(price, 2),
            pnl=round(pnl, 2),
            pnl_percent=round(pnl_percent, 2)
        ))
    
    return scenarios


def find_breakeven_prices(
    legs: list[OptionLeg],
    current_price: float
) -> list[float]:
    """Find prices where P&L crosses zero."""
    scenarios = generate_strategy_scenarios(legs, current_price, num_points=200)
    
    breakevens = []
    for i in range(1, len(scenarios)):
        prev_pnl = scenarios[i-1].pnl
        curr_pnl = scenarios[i].pnl
        
        # Check for sign change
        if (prev_pnl < 0 and curr_pnl >= 0) or (prev_pnl >= 0 and curr_pnl < 0):
            # Linear interpolation to find exact breakeven
            if curr_pnl != prev_pnl:
                ratio = abs(prev_pnl) / abs(curr_pnl - prev_pnl)
                breakeven = scenarios[i-1].underlying_price + ratio * (
                    scenarios[i].underlying_price - scenarios[i-1].underlying_price
                )
                breakevens.append(round(breakeven, 2))
    
    return breakevens


def calculate_max_profit_loss(
    legs: list[OptionLeg],
    current_price: float
) -> tuple[float, float]:
    """Calculate maximum profit and maximum loss for a strategy."""
    scenarios = generate_strategy_scenarios(legs, current_price, num_points=500)
    
    pnls = [s.pnl for s in scenarios]
    max_profit = max(pnls)
    max_loss = min(pnls)
    
    return max_profit, max_loss


def analyze_strategy(
    symbol: str,
    legs: list[OptionLeg],
    current_price: float,
    strategy_name: str = "Custom Strategy",
    implied_volatility: Optional[float] = None,
    iv_rank: Optional[float] = None,
) -> StrategyAnalysis:
    """
    Perform full analysis on an options strategy.
    
    Args:
        symbol: Underlying symbol
        legs: List of OptionLeg objects defining the strategy
        current_price: Current underlying price
        strategy_name: Name of the strategy
        implied_volatility: IV as decimal (optional)
        iv_rank: IV Rank 0-100 (optional)
    
    Returns:
        StrategyAnalysis with full P&L scenarios and risk metrics
    """
    if not legs:
        raise ValueError("Strategy must have at least one leg")
    
    # Calculate net premium
    net_premium = sum(leg.total_premium for leg in legs)
    
    # Calculate days to expiry (use earliest expiration)
    today = date.today()
    min_expiration = min(leg.expiration for leg in legs)
    days_to_expiry = (min_expiration - today).days
    
    # Generate scenarios
    scenarios = generate_strategy_scenarios(legs, current_price)
    
    # Find breakeven prices
    breakevens = find_breakeven_prices(legs, current_price)
    
    # Calculate max profit/loss
    max_profit, max_loss = calculate_max_profit_loss(legs, current_price)
    
    # Estimate profit probability using volatility
    vol = implied_volatility or DEFAULT_VOLATILITY
    profit_probability = None
    
    if breakevens and days_to_expiry > 0:
        # For strategies with breakevens, estimate probability of profit
        # This is a simplification - real probability depends on strategy type
        if len(breakevens) == 1:
            # Single breakeven - estimate probability of crossing it favorably
            be = breakevens[0]
            if net_premium > 0:  # Credit strategy - want to stay away from breakeven
                # Probability of NOT reaching breakeven
                profit_probability = 1 - _calculate_price_probability(
                    current_price, be, days_to_expiry, vol, want_above=(be > current_price)
                )
            else:  # Debit strategy - need to cross breakeven
                profit_probability = _calculate_price_probability(
                    current_price, be, days_to_expiry, vol, want_above=(be < current_price)
                )
            profit_probability = profit_probability * 100  # Convert to percentage
    
    return StrategyAnalysis(
        strategy_name=strategy_name,
        symbol=symbol,
        legs=legs,
        current_price=current_price,
        net_premium=round(net_premium, 2),
        max_profit=round(max_profit, 2),
        max_loss=round(max_loss, 2),
        breakeven_prices=breakevens,
        days_to_expiry=days_to_expiry,
        profit_probability=round(profit_probability, 1) if profit_probability else None,
        scenarios=scenarios,
        implied_volatility=implied_volatility,
        iv_rank=iv_rank,
    )


def analyze_single_leg(
    symbol: str,
    option_type: str,
    strike: float,
    expiration: date,
    action: str,
    premium: float,
    current_price: float,
    quantity: int = 1,
    implied_volatility: Optional[float] = None,
) -> StrategyAnalysis:
    """
    Convenience function to analyze a single-leg option strategy.
    """
    leg = OptionLeg(
        option_type=option_type,
        strike=strike,
        expiration=expiration,
        action=action,
        quantity=quantity,
        premium=premium,
        implied_volatility=implied_volatility,
    )
    
    strategy_name = leg.strategy
    
    return analyze_strategy(
        symbol=symbol,
        legs=[leg],
        current_price=current_price,
        strategy_name=strategy_name,
        implied_volatility=implied_volatility,
    )
