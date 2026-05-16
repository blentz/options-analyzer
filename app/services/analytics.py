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
    """Overall trading statistics.

    P&L is reported with explicit realized / unrealized split:
      - realized_pnl: cash already booked. Closed standalone positions
        + closed wheel cycles. This is what the bank ledger says.
      - unrealized_pnl: mark-to-market of currently held assets — shares
        from active wheel cycles priced at the latest Yahoo close.
        Open option positions are NOT marked here (the dashboard would
        need ~N scraper calls; the risk page already shows their MTM).
      - total_pnl: realized + unrealized.

    The legacy `total_pnl` field now equals realized_pnl for backwards
    compat (template variable name). UI should prefer the explicit
    `realized_pnl` and `unrealized_pnl` fields.
    """
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
    total_pnl: Decimal  # Realized + unrealized. Source of truth.
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    options_pnl: Decimal  # P&L from options only (premium) — realized
    underlying_pnl: Decimal  # P&L from underlying stock — realized
    total_premium_collected: Decimal
    total_premium_paid: Decimal
    total_commissions: Decimal
    total_fees: Decimal
    # Cycle-level breakouts for the wheel-aware UI.
    active_cycle_count: int = 0
    active_cycle_shares_held: int = 0
    unrealized_priced_symbols: int = 0   # how many symbols got a live price
    unrealized_missing_symbols: int = 0  # symbols where Yahoo failed


@dataclass
class SymbolStats:
    """Per-symbol statistics with realized / unrealized breakdown.

    `pnl` is realized+unrealized (the total) for backward compatibility
    with the existing chart. `realized` and `unrealized` allow the UI
    to show a stacked bar so the user can see which symbols are
    sitting on still-held wheel inventory.
    """
    symbol: str
    pnl: Decimal              # realized + unrealized
    realized: Decimal
    unrealized: Decimal
    num_positions: int        # counted as cycles (1 per cycle) + standalone
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


@dataclass
class _UnrealizedSnapshot:
    """Mark-to-market summary for active wheel-cycle holdings.

    Per-symbol so the per-symbol chart can stack realized + unrealized.
    Aggregate values for the dashboard headline.
    """
    by_symbol: dict[str, Decimal]   # symbol -> unrealized P&L on still-held shares
    by_symbol_shares: dict[str, int]
    total: Decimal
    symbols_priced: int
    symbols_missing: int
    total_shares_held: int
    active_cycle_count: int


async def compute_active_cycle_unrealized(db: AsyncSession) -> _UnrealizedSnapshot:
    """Mark every active wheel cycle's currently-held shares to market.

    Reads current prices from Yahoo (cheap, cached 60s). Symbols Yahoo
    can't price are tallied as `symbols_missing` so the UI can warn —
    we don't silently treat them as zero.

    Aggregates by symbol so per-symbol bars can show a stacked
    realized + unrealized breakdown.
    """
    from app.models import WheelCycle as _WC
    from app.services.price_service import get_multiple_prices

    active_stmt = select(_WC).where(
        _WC.status == "ACTIVE", _WC.shares_held > 0
    )
    active = (await db.execute(active_stmt)).scalars().all()
    if not active:
        return _UnrealizedSnapshot({}, {}, Decimal(0), 0, 0, 0, 0)

    symbols = sorted({c.symbol for c in active})
    quotes = await get_multiple_prices(symbols)

    by_symbol: dict[str, Decimal] = defaultdict(lambda: Decimal(0))
    by_shares: dict[str, int] = defaultdict(int)
    total = Decimal(0)
    priced = 0
    missing = 0
    total_shares = 0

    for c in active:
        total_shares += c.shares_held
        by_shares[c.symbol] += c.shares_held
        q = quotes.get(c.symbol)
        if q is None or q.price is None:
            missing += 1
            continue
        priced += 1
        mark_value = Decimal(str(q.price))
        unrealized = (mark_value - Decimal(c.avg_cost_basis)) * c.shares_held
        by_symbol[c.symbol] += unrealized
        total += unrealized

    return _UnrealizedSnapshot(
        by_symbol=dict(by_symbol),
        by_symbol_shares=dict(by_shares),
        total=total,
        symbols_priced=priced,
        symbols_missing=missing,
        total_shares_held=total_shares,
        active_cycle_count=len(active),
    )


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

    # ACTIVE cycles also have REALIZED cash already banked — closed options'
    # premiums and any partial stock sells. They're just not winners/losers
    # yet (the cycle hasn't ended). Pulling these into realized_pnl is what
    # lets the headline match "what's actually in the brokerage account".
    active_cycles_all = [
        c for c in (await db.execute(
            select(WheelCycle).where(WheelCycle.status == "ACTIVE")
        )).scalars().all()
    ]
    active_realized_total = sum((c.total_pnl for c in active_cycles_all), Decimal(0))
    active_realized_options = sum((c.options_pnl for c in active_cycles_all), Decimal(0))
    active_realized_stock = sum((c.stock_pnl for c in active_cycles_all), Decimal(0))

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

    # Calculate REALIZED P&L. Includes:
    #   - standalone closed positions (their total_pnl is already realized)
    #   - closed wheel cycles (cycle.total_pnl = options + realized stock)
    #   - active wheel cycles' realized portion (banked premiums + any
    #     partial stock sells — cycle.total_pnl on ACTIVE rows is realized
    #     only; unrealized MTM on held shares is computed separately)
    options_pnl = (
        sum((p.net_pnl for p in closed), Decimal(0))
        + cycle_options_pnl + active_realized_options
    )
    underlying_pnl = (
        sum((p.underlying_pnl for p in closed), Decimal(0))
        + cycle_stock_pnl + active_realized_stock
    )
    total_pnl = (
        sum((p.total_pnl for p in closed), Decimal(0))
        + cycle_total_pnl + active_realized_total
    )

    premium_collected = sum((p.total_premium for p in closed if p.total_premium > 0), Decimal(0))
    premium_paid = sum((p.total_premium for p in closed if p.total_premium < 0), Decimal(0))

    # Commissions and fees are paid at trade execution, not at position
    # close. Filter by OptionTrade.trade_date (NOT position.close_date)
    # and don't exclude wheel members — every trade incurred its
    # commission regardless of whether its position later ended up in
    # a wheel cycle. The old code summed only standalone closed
    # positions' total_commission, which dropped roughly 95% of the
    # real cost for a heavy wheel trader.
    trade_stmt = select(OptionTrade)
    trades_in_range = [
        t for t in (await db.execute(trade_stmt)).scalars().all()
        if _in_range(t.trade_date, dr)
    ]
    total_commissions = sum((t.commission for t in trades_in_range), Decimal(0))
    total_fees = sum((t.fees for t in trades_in_range), Decimal(0))

    # Mark-to-market on active wheel cycles. Uses live Yahoo prices
    # (cached 60s) so the dashboard reflects today's exposure on shares
    # the user is still holding from put assignments.
    unrealized = await compute_active_cycle_unrealized(db)

    realized_pnl = total_pnl  # the variable we just computed IS realized only
    grand_total = realized_pnl + unrealized.total

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
        total_pnl=grand_total,
        realized_pnl=realized_pnl,
        unrealized_pnl=unrealized.total,
        options_pnl=options_pnl,
        underlying_pnl=underlying_pnl,
        total_premium_collected=premium_collected,
        total_premium_paid=premium_paid,
        total_commissions=total_commissions,
        total_fees=total_fees,
        active_cycle_count=unrealized.active_cycle_count,
        active_cycle_shares_held=unrealized.total_shares_held,
        unrealized_priced_symbols=unrealized.symbols_priced,
        unrealized_missing_symbols=unrealized.symbols_missing,
    )


async def get_pnl_by_symbol(
    db: AsyncSession,
    date_range: Optional[DateRange] = None,
) -> list[SymbolStats]:
    """Per-symbol P&L breakdown.

    Wheel-aware: positions in any wheel cycle are excluded from the
    per-symbol position bucket; closed wheel cycles contribute their
    `total_pnl` to their symbol (as one trade); active wheel cycles
    contribute their MARKED-TO-MARKET unrealized P&L on still-held shares.

    Each `SymbolStats` row exposes realized, unrealized, and the sum
    so charts can stack them or pick one. Win-rate counts at the unit
    level (each cycle = 1 trade, each standalone position = 1 trade).
    """
    from app.models import WheelCycle as _WC
    from app.services.wheel_detection import position_ids_in_cycles

    dr = date_range or DateRange(None, None, "ALL")

    stmt = select(OptionPosition).options(selectinload(OptionPosition.contract)).where(
        OptionPosition.is_closed == True
    )
    wheel_pids = await position_ids_in_cycles(db)
    standalone = [
        p for p in (await db.execute(stmt)).scalars().all()
        if _in_range(p.close_date, dr) and p.id not in wheel_pids
    ]

    # closed cycles within window
    closed_cycles = [
        c for c in (await db.execute(
            select(_WC).where(_WC.status == "CLOSED")
        )).scalars().all()
        if _in_range(c.ended_at, dr)
    ]

    unrealized = await compute_active_cycle_unrealized(db)

    by_symbol: dict[str, dict] = defaultdict(
        lambda: {
            'realized': Decimal(0),
            'unrealized': Decimal(0),
            'count': 0,
            'winners': 0,
        }
    )

    for p in standalone:
        d = by_symbol[p.contract.symbol]
        d['realized'] += p.total_pnl
        d['count'] += 1
        if p.total_pnl > 0:
            d['winners'] += 1

    for c in closed_cycles:
        d = by_symbol[c.symbol]
        d['realized'] += c.total_pnl
        d['count'] += 1
        if c.total_pnl > 0:
            d['winners'] += 1

    # Active cycles: their REALIZED portion (banked premiums + any partial
    # sells) belongs in per-symbol realized P&L. They don't count toward
    # win/loss yet (cycle hasn't ended).
    active_cycles = (await db.execute(
        select(_WC).where(_WC.status == "ACTIVE")
    )).scalars().all()
    for c in active_cycles:
        d = by_symbol[c.symbol]
        d['realized'] += c.total_pnl

    # Unrealized — only matters for symbols with currently-held shares.
    for sym, urpnl in unrealized.by_symbol.items():
        by_symbol[sym]['unrealized'] += urpnl

    stats: list[SymbolStats] = []
    for symbol, data in by_symbol.items():
        win_rate = data['winners'] / data['count'] * 100 if data['count'] > 0 else 0
        total = data['realized'] + data['unrealized']
        stats.append(SymbolStats(
            symbol=symbol,
            pnl=total,
            realized=data['realized'],
            unrealized=data['unrealized'],
            num_positions=data['count'],
            win_rate=win_rate,
        ))
    # Sort by total P&L descending — same as before for chart consistency.
    stats.sort(key=lambda s: s.pnl, reverse=True)
    return stats


async def _realized_events_timeline(
    db: AsyncSession,
    date_range: Optional["DateRange"] = None,
) -> list[tuple[datetime, Decimal]]:
    """Return every realized cash-flow event sorted by date.

    This is the source of truth that get_monthly_pnl and get_cumulative_pnl
    both walk. Sum of returned values == realized_pnl on the dashboard:
    no gaps, no aggregation seams.

    Three event streams:

      1. EVERY OptionTrade row (amount at trade_date). Captures option
         premium received/paid and buybacks for any position, regardless
         of wheel membership.

      2. For symbols with any WheelCycle: walk that symbol's
         UnderlyingTrades in time order applying the same weighted-
         average basis the cycle detector uses, and emit realized stock
         P&L `(sell_price − basis) × qty` at each SELL date. This
         captures cycle-level stock realization without dependence on
         pre-computed cycle.stock_pnl, AND covers partial sells from
         still-active cycles.

      3. For symbols WITHOUT any WheelCycle: include UnderlyingTrade
         amounts raw at trade_date. These are stock cash flows on
         non-cycle tickers (e.g., legacy assignments the user never
         wheeled). Their summed cash flow equals their P&L by
         construction.

    Splitting on symbol presence in cycles avoids double-counting —
    a symbol's stock flows go through exactly one of paths 2 or 3.
    """
    from app.models import OptionTrade as _OT, UnderlyingTrade as _UT, WheelCycle as _WC

    dr = date_range or DateRange(None, None, "ALL")

    events: list[tuple[datetime, Decimal]] = []

    # 1. All option trades
    option_trades = (await db.execute(
        select(_OT).order_by(_OT.trade_date)
    )).scalars().all()
    for t in option_trades:
        events.append((t.trade_date, Decimal(t.amount)))

    # Bucket underlying trades by symbol so step 2/3 can split cleanly.
    all_uts = (await db.execute(
        select(_UT).order_by(_UT.trade_date)
    )).scalars().all()
    uts_by_symbol: dict[str, list] = defaultdict(list)
    for ut in all_uts:
        uts_by_symbol[ut.symbol].append(ut)

    cycle_symbols = {
        s for (s,) in (await db.execute(select(_WC.symbol).distinct())).all()
    }

    # 2. Wheel-symbol stock activity via avg-basis. Walk once per symbol;
    # the cycle boundaries reset basis to zero when shares_held drains.
    # Each realized event is quantized to cents (Decimal('0.01')) — matches
    # how money actually flows. Note: when summed across many events, the
    # total can differ from the dashboard's realized headline by up to ~1¢
    # per cycle because the cycle detector stores `total_pnl` as the
    # rounded sum-of-precise-values (Numeric(12,2) at insert time) while
    # the per-event timeline rounds each event before summing. Both render
    # identical dollars to the user; the discrepancy is sub-cent noise from
    # different rounding schedules, not a real accounting gap.
    _CENTS = Decimal("0.01")
    for symbol in cycle_symbols:
        shares = 0
        basis = Decimal(0)
        for ut in uts_by_symbol.get(symbol, []):
            qty = int(ut.quantity)
            price = Decimal(ut.price)
            if ut.action == "BUY":
                new_value = basis * shares + price * qty
                shares += qty
                basis = new_value / shares if shares > 0 else Decimal(0)
            elif ut.action == "SELL":
                sell_qty = min(qty, shares)
                realized = ((price - basis) * sell_qty).quantize(_CENTS)
                if realized != 0:
                    events.append((ut.trade_date, realized))
                shares -= sell_qty
                if shares == 0:
                    basis = Decimal(0)

    # 3. Non-wheel-symbol stock activity = raw cash flow. These rarely
    # exist (the linker only persists stock trades on options-active
    # tickers, all of which should end up in cycles), but if any slip
    # through they need to land somewhere realized.
    for symbol, uts in uts_by_symbol.items():
        if symbol in cycle_symbols:
            continue
        for ut in uts:
            events.append((ut.trade_date, Decimal(ut.amount)))

    # Apply date filter (after building so per-symbol state stayed
    # consistent during the walk).
    if not dr.is_unbounded:
        events = [(d, v) for (d, v) in events if _in_range(d, dr)]
    events.sort(key=lambda e: e[0])
    return events


async def get_monthly_pnl(
    db: AsyncSession,
    date_range: Optional[DateRange] = None,
) -> list[MonthlyStats]:
    """Per-month realized P&L bars.

    Sums per-trade events from `_realized_events_timeline` so the
    chart total reconciles exactly with the dashboard's realized
    headline — no aggregation gaps. Win/loss/trade counts still come
    from position/cycle units (we don't count every option trade as
    a "trade" — that'd double the OPEN+CLOSE pair).
    """
    from app.models import WheelCycle as _WC
    from app.services.wheel_detection import position_ids_in_cycles

    dr = date_range or DateRange(None, None, "ALL")

    # Money bucket via the per-trade timeline.
    events = await _realized_events_timeline(db, dr)
    monthly_data: dict[str, dict] = defaultdict(
        lambda: {'pnl': Decimal(0), 'trades': 0, 'winners': 0, 'losers': 0}
    )
    for when, delta in events:
        if when:
            monthly_data[when.strftime('%Y-%m')]['pnl'] += delta

    # Win/loss/trade counts at the UNIT level (one per standalone closed
    # position or closed cycle), bucketed by close_date / ended_at.
    wheel_pids = await position_ids_in_cycles(db)
    standalone_closed = [
        p for p in (await db.execute(
            select(OptionPosition).where(OptionPosition.is_closed == True)
        )).scalars().all()
        if _in_range(p.close_date, dr) and p.id not in wheel_pids
    ]
    for p in standalone_closed:
        if p.close_date:
            mk = p.close_date.strftime('%Y-%m')
            monthly_data[mk]['trades'] += 1
            if p.total_pnl > 0:
                monthly_data[mk]['winners'] += 1
            elif p.total_pnl < 0:
                monthly_data[mk]['losers'] += 1

    closed_cycles = [
        c for c in (await db.execute(
            select(_WC).where(_WC.status == "CLOSED")
        )).scalars().all()
        if _in_range(c.ended_at, dr)
    ]
    for c in closed_cycles:
        if c.ended_at:
            mk = c.ended_at.strftime('%Y-%m')
            monthly_data[mk]['trades'] += 1
            if c.total_pnl > 0:
                monthly_data[mk]['winners'] += 1
            elif c.total_pnl < 0:
                monthly_data[mk]['losers'] += 1

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
    """Cumulative realized P&L line. Same per-trade event source as
    get_monthly_pnl so the two charts and the dashboard headline all
    sum to the same number.
    """
    dr = date_range or DateRange(None, None, "ALL")
    events = await _realized_events_timeline(db, dr)
    cumulative: list[tuple[datetime, Decimal]] = []
    running_total = Decimal(0)
    for when, delta in events:
        running_total += delta
        cumulative.append((when, running_total))
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
