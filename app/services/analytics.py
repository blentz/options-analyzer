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


# Time-range presets exposed on the dashboard. Each maps a short label to a
# rolling timedelta ending "now". YTD and "all" are handled separately.
# Keep these short — they're used as both query-string values and button
# labels on the dashboard.
_PRESET_RANGES = {
    "1M": timedelta(days=30),
    "3M": timedelta(days=90),
    "6M": timedelta(days=180),
    "1Y": timedelta(days=365),
    "2Y": timedelta(days=730),
    "5Y": timedelta(days=1825),
}

# Canonical ordering for UI button rows — single source of truth.
PRESET_RANGE_LABELS: list[str] = ["1M", "3M", "6M", "YTD", "1Y", "2Y", "5Y", "ALL"]


@dataclass
class DateRange:
    """Resolved analysis window. `start` and `end` are inclusive bounds on
    OptionPosition.close_date. Either may be None to mean unbounded.

    `label` is the human-readable preset name ("1M", "YTD", "ALL", "Custom")
    that produced this range — used by the template to highlight the active
    button and render the date span next to the title.
    """
    start: Optional[datetime]
    end: Optional[datetime]
    label: str

    @property
    def is_unbounded(self) -> bool:
        return self.start is None and self.end is None


def resolve_date_range(
    range_name: Optional[str],
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    now: Optional[datetime] = None,
) -> DateRange:
    """Convert a dashboard time-range query into a concrete DateRange.

    Resolution order:
      - "all" (or no inputs at all) → unbounded
      - "ytd" → (Jan 1 of current year, now)
      - "custom" → use explicit start/end (either may be None)
      - "1M"/"3M"/"6M"/"1Y"/"2Y"/"5Y" → (now − window, now)
      - explicit start/end without a range name → custom range
      - anything else → unbounded (don't surprise the user)

    `now` is injectable so callers/tests can pin the clock.
    """
    now = now or datetime.utcnow()
    rn = (range_name or "").strip().lower()

    if rn == "all" or (not rn and start is None and end is None):
        return DateRange(None, None, "ALL")

    if rn == "ytd":
        return DateRange(datetime(now.year, 1, 1), now, "YTD")

    if rn == "custom" or (not rn and (start or end)):
        # Show "Custom" if either bound is set; if both are None we still
        # arrived here via an explicit range=custom — leave both None and
        # label as Custom so the UI shows the date inputs as active.
        return DateRange(start, end, "Custom")

    preset_key = rn.upper()
    if preset_key in _PRESET_RANGES:
        return DateRange(now - _PRESET_RANGES[preset_key], now, preset_key)

    return DateRange(None, None, "ALL")


def _in_range(close_date: Optional[datetime], dr: DateRange) -> bool:
    """Inclusive on both ends. Positions with no close_date never pass."""
    if close_date is None:
        return False
    if dr.start is not None and close_date < dr.start:
        return False
    if dr.end is not None and close_date > dr.end:
        return False
    return True


@dataclass
class OverallStats:
    """Overall trading statistics."""
    total_positions: int
    closed_positions: int
    open_positions: int
    winners: int
    losers: int
    breakeven_count: int  # Closed positions with exactly $0 P&L (scratches)
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


async def get_overall_stats(
    db: AsyncSession,
    date_range: Optional[DateRange] = None,
) -> OverallStats:
    """Get overall trading statistics.

    Wheel-aware: each wheel cycle counts as ONE trade for win/loss
    purposes. Constituent CSPs and CCs don't independently inflate the
    win/loss counters (which used to make a profitable wheel show up
    as "one loss + one win" — i.e., a 50% win rate for a strategy
    that actually printed money). Loose options (long calls/puts you
    bought outright, etc.) still count individually.

    When `date_range` is provided, ONLY closed positions and CLOSED
    cycles are filtered (by close_date / ended_at). The "open
    positions" count always reflects what's actually open right now.
    """
    from app.models import WheelCycle
    from app.services.wheel_detection import position_ids_in_cycles

    dr = date_range or DateRange(None, None, "ALL")
    stmt = select(OptionPosition).options(selectinload(OptionPosition.contract))
    result = await db.execute(stmt)
    positions = list(result.scalars().all())

    # Position IDs that belong to a wheel cycle — exclude from the per-
    # position W/L tally and re-add at the cycle level below.
    wheel_position_ids = await position_ids_in_cycles(db)

    closed = [
        p for p in positions
        if p.is_closed
        and _in_range(p.close_date, dr)
        and p.id not in wheel_position_ids
    ]
    open_positions = [p for p in positions if not p.is_closed]
    # Use total_pnl for win/loss determination (includes underlying).
    # Breakeven positions ($0 P&L) are neither wins nor losses, so they are
    # excluded from BOTH numerator and denominator of win_rate below.
    winners = [p for p in closed if p.total_pnl > 0]
    losers = [p for p in closed if p.total_pnl < 0]
    breakeven = [p for p in closed if p.total_pnl == 0]

    # Fold in CLOSED wheel cycles as single trades. Filter by the date
    # range using ended_at (analogous to close_date for positions).
    cycle_stmt = select(WheelCycle).where(WheelCycle.status == "CLOSED")
    closed_cycles = [
        c for c in (await db.execute(cycle_stmt)).scalars().all()
        if _in_range(c.ended_at, dr)
    ]
    cycle_winners = sum(1 for c in closed_cycles if c.total_pnl > 0)
    cycle_losers = sum(1 for c in closed_cycles if c.total_pnl < 0)
    cycle_breakeven = sum(1 for c in closed_cycles if c.total_pnl == 0)
    cycle_total_pnl = sum((c.total_pnl for c in closed_cycles), Decimal(0))
    cycle_options_pnl = sum((c.options_pnl for c in closed_cycles), Decimal(0))
    cycle_stock_pnl = sum((c.stock_pnl for c in closed_cycles), Decimal(0))

    decisive = (len(winners) + len(losers)) + (cycle_winners + cycle_losers)
    win_count = len(winners) + cycle_winners
    loss_count = len(losers) + cycle_losers
    breakeven_count = len(breakeven) + cycle_breakeven

    expired = len([p for p in closed if p.outcome == 'EXPIRED'])
    assigned = len([p for p in closed if p.outcome == 'ASSIGNED'])
    closed_early = len([p for p in closed if p.outcome == 'CLOSED'])

    short_puts = len([p for p in positions if p.strategy == 'SHORT PUT'])
    short_calls = len([p for p in positions if p.strategy == 'SHORT CALL'])
    long_puts = len([p for p in positions if p.strategy == 'LONG PUT'])
    long_calls = len([p for p in positions if p.strategy == 'LONG CALL'])

    # Calculate P&L components. Includes cycle P&L so the dashboard's
    # Total / Options / Underlying buckets sum to your actual realised gain.
    options_pnl = sum((p.net_pnl for p in closed), Decimal(0)) + cycle_options_pnl
    underlying_pnl = sum((p.underlying_pnl for p in closed), Decimal(0)) + cycle_stock_pnl
    total_pnl = sum((p.total_pnl for p in closed), Decimal(0)) + cycle_total_pnl

    premium_collected = sum((p.total_premium for p in closed if p.total_premium > 0), Decimal(0))
    premium_paid = sum((p.total_premium for p in closed if p.total_premium < 0), Decimal(0))
    total_commissions = sum((p.total_commission for p in closed), Decimal(0))
    total_fees = sum((p.total_fees for p in closed), Decimal(0))

    return OverallStats(
        total_positions=len(positions),
        # Closed-trade count = standalone closed positions PLUS closed cycles.
        closed_positions=len(closed) + len(closed_cycles),
        open_positions=len(open_positions),
        winners=win_count,
        losers=loss_count,
        breakeven_count=breakeven_count,
        win_rate=(win_count / decisive * 100) if decisive else 0,
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


async def get_pnl_by_symbol(
    db: AsyncSession,
    date_range: Optional[DateRange] = None,
) -> list[SymbolStats]:
    """Get P&L breakdown by underlying symbol within the chosen window."""
    dr = date_range or DateRange(None, None, "ALL")
    stmt = select(OptionPosition).options(selectinload(OptionPosition.contract)).where(
        OptionPosition.is_closed == True
    )
    result = await db.execute(stmt)
    positions = [p for p in result.scalars().all() if _in_range(p.close_date, dr)]

    symbol_data: dict[str, dict] = defaultdict(lambda: {'pnl': Decimal(0), 'count': 0, 'winners': 0})

    for p in positions:
        symbol = p.contract.symbol
        symbol_data[symbol]['pnl'] += p.total_pnl  # Use total_pnl (includes underlying); range already filtered above
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


async def get_monthly_pnl(
    db: AsyncSession,
    date_range: Optional[DateRange] = None,
) -> list[MonthlyStats]:
    """Get P&L breakdown by month within the chosen window."""
    dr = date_range or DateRange(None, None, "ALL")
    stmt = select(OptionPosition).options(selectinload(OptionPosition.contract)).where(
        OptionPosition.is_closed == True
    ).order_by(OptionPosition.close_date)
    result = await db.execute(stmt)
    positions = [p for p in result.scalars().all() if _in_range(p.close_date, dr)]

    # Explicit annotation so mypy can't think the dict values are `object`.
    monthly_data: dict[str, dict] = defaultdict(
        lambda: {'pnl': Decimal(0), 'trades': 0, 'winners': 0, 'losers': 0}
    )

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
            losers=data['losers'],
        ))

    return stats


async def get_cumulative_pnl(
    db: AsyncSession,
    date_range: Optional[DateRange] = None,
) -> list[tuple[datetime, Decimal]]:
    """Get cumulative P&L over time for charting.

    Cumulative resets to zero at the first closed position INSIDE the window —
    we're answering "how did this window perform on its own", not "what's
    your all-time running total truncated to a recent slice."
    """
    dr = date_range or DateRange(None, None, "ALL")
    stmt = select(OptionPosition).where(
        OptionPosition.is_closed == True
    ).order_by(OptionPosition.close_date)
    result = await db.execute(stmt)
    positions = [p for p in result.scalars().all() if _in_range(p.close_date, dr)]

    cumulative = []
    running_total = Decimal(0)

    for p in positions:
        if p.close_date:
            running_total += p.total_pnl  # Use total_pnl
            cumulative.append((p.close_date, running_total))

    return cumulative


def _position_active_in_range(p: OptionPosition, dr: "DateRange") -> bool:
    """True if `p` was active at any point within `dr` (overlap semantics).

    For the positions page the most useful question isn't "closed in window"
    but "was this trade on the books at any time during the window." A short
    put I opened in March 2024 and closed in October 2024 should show up
    when I ask for "Q2 2024" — even though neither its open nor close date
    is inside that quarter. So we use interval overlap:

        opened-before-end  AND  (closed-after-start OR still-open)

    Open positions (close_date None) are considered ongoing through "now".
    Unbounded ends collapse to trivially-true.
    """
    if dr.is_unbounded:
        return True
    # opened before end?
    if dr.end is not None and p.open_date and p.open_date > dr.end:
        return False
    # closed after start? (or still open)
    if dr.start is not None and p.close_date is not None and p.close_date < dr.start:
        return False
    return True


async def get_positions(
    db: AsyncSession,
    closed_only: bool = False,
    open_only: bool = False,
    date_range: Optional[DateRange] = None,
) -> list[PositionDetail]:
    """Get all positions with details.

    `date_range` uses OVERLAP semantics (see _position_active_in_range): a
    position is included if it was active for any portion of the window.
    Different from the dashboard's close-date filter — on the positions
    page you usually want "what did I have on at any point in this period."
    """
    dr = date_range or DateRange(None, None, "ALL")
    stmt = select(OptionPosition).options(selectinload(OptionPosition.contract))

    if closed_only:
        stmt = stmt.where(OptionPosition.is_closed == True)
    elif open_only:
        stmt = stmt.where(OptionPosition.is_closed == False)

    stmt = stmt.order_by(OptionPosition.open_date.desc())
    result = await db.execute(stmt)
    positions = [p for p in result.scalars().all() if _position_active_in_range(p, dr)]

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


async def get_strategy_breakdown(
    db: AsyncSession,
    date_range: Optional[DateRange] = None,
) -> dict[str, dict]:
    """Get performance breakdown by strategy within the chosen window.

    Wheel-aware: positions belonging to a closed wheel cycle are pulled
    out of their per-leg buckets (SHORT PUT / SHORT CALL) and rolled
    into a single WHEEL bucket using the cycle's total P&L. Otherwise
    a profitable wheel would split into an apparent SHORT PUT loss and
    SHORT CALL win, making both strategies look misleading.
    """
    from app.models import WheelCycle
    from app.services.wheel_detection import position_ids_in_cycles

    dr = date_range or DateRange(None, None, "ALL")
    stmt = select(OptionPosition).where(OptionPosition.is_closed == True)
    raw = (await db.execute(stmt)).scalars().all()

    wheel_pids = await position_ids_in_cycles(db)
    positions = [
        p for p in raw
        if _in_range(p.close_date, dr) and p.id not in wheel_pids
    ]

    strategies: dict[str, dict] = defaultdict(lambda: {'count': 0, 'pnl': Decimal(0), 'winners': 0})

    for p in positions:
        strategies[p.strategy]['count'] += 1
        strategies[p.strategy]['pnl'] += p.total_pnl
        if p.total_pnl > 0:
            strategies[p.strategy]['winners'] += 1

    # Add closed wheel cycles as their own bucket.
    cycles = [
        c for c in (await db.execute(
            select(WheelCycle).where(WheelCycle.status == "CLOSED")
        )).scalars().all()
        if _in_range(c.ended_at, dr)
    ]
    if cycles:
        bucket = strategies["WHEEL"]
        for c in cycles:
            bucket['count'] += 1
            bucket['pnl'] += c.total_pnl
            if c.total_pnl > 0:
                bucket['winners'] += 1

    result_dict = {}
    for strategy, data in strategies.items():
        win_rate = data['winners'] / data['count'] * 100 if data['count'] > 0 else 0
        result_dict[strategy] = {
            'count': data['count'],
            'pnl': float(data['pnl']),
            'win_rate': win_rate
        }

    return result_dict
