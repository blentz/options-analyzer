"""Analytics service for computing trading statistics."""

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Optional
from dataclasses import dataclass
from collections import defaultdict

from sqlalchemy import select, func, and_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import OptionContract, OptionTrade, OptionPosition


@dataclass
class OverallStats:
    """Overall trading statistics."""
    total_positions: int
    closed_positions: int
    open_positions: int
    winners: int
    losers: int
    win_rate: float
    expired: int
    assigned: int
    closed_early: int
    short_puts: int
    short_calls: int
    long_puts: int
    long_calls: int
    total_pnl: Decimal
    options_pnl: Decimal  # P&L from options only (premium)
    underlying_pnl: Decimal  # P&L from underlying stock (assignment/cover)
    total_premium_collected: Decimal
    total_premium_paid: Decimal
    total_commissions: Decimal
    total_fees: Decimal


@dataclass
class SymbolStats:
    """Per-symbol statistics."""
    symbol: str
    pnl: Decimal
    num_positions: int
    win_rate: float


@dataclass
class MonthlyStats:
    """Monthly statistics."""
    month: str
    pnl: Decimal
    num_trades: int
    winners: int
    losers: int


@dataclass
class PositionDetail:
    """Position detail for display."""
    contract_id: str
    symbol: str
    expiration: str
    strike: Decimal
    option_type: str
    strategy: str
    outcome: str
    open_date: str
    close_date: Optional[str]
    net_pnl: Decimal  # Options P&L only
    underlying_pnl: Decimal  # Underlying stock P&L
    total_pnl: Decimal  # Combined P&L
    is_winner: bool
    is_closed: bool


async def get_overall_stats(db: AsyncSession) -> OverallStats:
    """Get overall trading statistics."""
    stmt = select(OptionPosition).options(selectinload(OptionPosition.contract))
    result = await db.execute(stmt)
    positions = list(result.scalars().all())

    closed = [p for p in positions if p.is_closed]
    open_positions = [p for p in positions if not p.is_closed]
    # Use total_pnl for win/loss determination (includes underlying)
    winners = [p for p in closed if p.total_pnl > 0]
    losers = [p for p in closed if p.total_pnl <= 0]

    expired = len([p for p in closed if p.outcome == 'EXPIRED'])
    assigned = len([p for p in closed if p.outcome == 'ASSIGNED'])
    closed_early = len([p for p in closed if p.outcome == 'CLOSED'])

    short_puts = len([p for p in positions if p.strategy == 'SHORT PUT'])
    short_calls = len([p for p in positions if p.strategy == 'SHORT CALL'])
    long_puts = len([p for p in positions if p.strategy == 'LONG PUT'])
    long_calls = len([p for p in positions if p.strategy == 'LONG CALL'])

    # Calculate P&L components
    options_pnl = sum((p.net_pnl for p in closed), Decimal(0))
    underlying_pnl = sum((p.underlying_pnl for p in closed), Decimal(0))
    total_pnl = sum((p.total_pnl for p in closed), Decimal(0))

    premium_collected = sum((p.total_premium for p in closed if p.total_premium > 0), Decimal(0))
    premium_paid = sum((p.total_premium for p in closed if p.total_premium < 0), Decimal(0))
    total_commissions = sum((p.total_commission for p in closed), Decimal(0))
    total_fees = sum((p.total_fees for p in closed), Decimal(0))

    return OverallStats(
        total_positions=len(positions),
        closed_positions=len(closed),
        open_positions=len(open_positions),
        winners=len(winners),
        losers=len(losers),
        win_rate=len(winners) / len(closed) * 100 if closed else 0,
        expired=expired,
        assigned=assigned,
        closed_early=closed_early,
        short_puts=short_puts,
        short_calls=short_calls,
        long_puts=long_puts,
        long_calls=long_calls,
        total_pnl=total_pnl,
        options_pnl=options_pnl,
        underlying_pnl=underlying_pnl,
        total_premium_collected=premium_collected,
        total_premium_paid=premium_paid,
        total_commissions=total_commissions,
        total_fees=total_fees
    )


async def get_pnl_by_symbol(db: AsyncSession) -> list[SymbolStats]:
    """Get P&L breakdown by underlying symbol."""
    stmt = select(OptionPosition).options(selectinload(OptionPosition.contract)).where(
        OptionPosition.is_closed == True
    )
    result = await db.execute(stmt)
    positions = list(result.scalars().all())

    symbol_data = defaultdict(lambda: {'pnl': Decimal(0), 'count': 0, 'winners': 0})

    for p in positions:
        symbol = p.contract.symbol
        symbol_data[symbol]['pnl'] += p.total_pnl  # Use total_pnl (includes underlying)
        symbol_data[symbol]['count'] += 1
        if p.total_pnl > 0:
            symbol_data[symbol]['winners'] += 1

    stats = []
    for symbol, data in sorted(symbol_data.items(), key=lambda x: x[1]['pnl'], reverse=True):
        win_rate = data['winners'] / data['count'] * 100 if data['count'] > 0 else 0
        stats.append(SymbolStats(
            symbol=symbol,
            pnl=data['pnl'],
            num_positions=data['count'],
            win_rate=win_rate
        ))

    return stats


async def get_monthly_pnl(db: AsyncSession) -> list[MonthlyStats]:
    """Get P&L breakdown by month."""
    stmt = select(OptionPosition).options(selectinload(OptionPosition.contract)).where(
        OptionPosition.is_closed == True
    ).order_by(OptionPosition.close_date)
    result = await db.execute(stmt)
    positions = list(result.scalars().all())

    monthly_data = defaultdict(lambda: {'pnl': Decimal(0), 'trades': 0, 'winners': 0, 'losers': 0})

    for p in positions:
        if p.close_date:
            month_key = p.close_date.strftime('%Y-%m')
            monthly_data[month_key]['pnl'] += p.total_pnl  # Use total_pnl
            monthly_data[month_key]['trades'] += 1
            if p.total_pnl > 0:
                monthly_data[month_key]['winners'] += 1
            else:
                monthly_data[month_key]['losers'] += 1

    stats = []
    for month, data in sorted(monthly_data.items()):
        stats.append(MonthlyStats(
            month=month,
            pnl=data['pnl'],
            num_trades=data['trades'],
            winners=data['winners'],
            losers=data['losers']
        ))

    return stats


async def get_cumulative_pnl(db: AsyncSession) -> list[tuple[datetime, Decimal]]:
    """Get cumulative P&L over time for charting."""
    stmt = select(OptionPosition).where(
        OptionPosition.is_closed == True
    ).order_by(OptionPosition.close_date)
    result = await db.execute(stmt)
    positions = list(result.scalars().all())

    cumulative = []
    running_total = Decimal(0)

    for p in positions:
        if p.close_date:
            running_total += p.total_pnl  # Use total_pnl
            cumulative.append((p.close_date, running_total))

    return cumulative


async def get_positions(db: AsyncSession, closed_only: bool = False, open_only: bool = False) -> list[PositionDetail]:
    """Get all positions with details."""
    stmt = select(OptionPosition).options(selectinload(OptionPosition.contract))

    if closed_only:
        stmt = stmt.where(OptionPosition.is_closed == True)
    elif open_only:
        stmt = stmt.where(OptionPosition.is_closed == False)

    stmt = stmt.order_by(OptionPosition.open_date.desc())
    result = await db.execute(stmt)
    positions = list(result.scalars().all())

    details = []
    for p in positions:
        details.append(PositionDetail(
            contract_id=p.contract.contract_id,
            symbol=p.contract.symbol,
            expiration=p.contract.expiration.strftime('%m/%d/%y'),
            strike=p.contract.strike,
            option_type=p.contract.option_type,
            strategy=p.strategy,
            outcome=p.outcome,
            open_date=p.open_date.strftime('%m/%d/%y'),
            close_date=p.close_date.strftime('%m/%d/%y') if p.close_date else None,
            net_pnl=p.net_pnl,
            underlying_pnl=p.underlying_pnl,
            total_pnl=p.total_pnl,
            is_winner=p.total_pnl > 0,
            is_closed=p.is_closed
        ))

    return details


async def get_strategy_breakdown(db: AsyncSession) -> dict[str, dict]:
    """Get performance breakdown by strategy."""
    stmt = select(OptionPosition).where(OptionPosition.is_closed == True)
    result = await db.execute(stmt)
    positions = list(result.scalars().all())

    strategies = defaultdict(lambda: {'count': 0, 'pnl': Decimal(0), 'winners': 0})

    for p in positions:
        strategies[p.strategy]['count'] += 1
        strategies[p.strategy]['pnl'] += p.total_pnl  # Use total_pnl
        if p.total_pnl > 0:
            strategies[p.strategy]['winners'] += 1

    result_dict = {}
    for strategy, data in strategies.items():
        win_rate = data['winners'] / data['count'] * 100 if data['count'] > 0 else 0
        result_dict[strategy] = {
            'count': data['count'],
            'pnl': float(data['pnl']),
            'win_rate': win_rate
        }

    return result_dict
