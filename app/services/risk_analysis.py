"""Risk analysis service for open options positions."""

from dataclasses import dataclass
from datetime import datetime, date
from decimal import Decimal
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import OptionPosition, OptionContract


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
        scenarios=scenarios
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
