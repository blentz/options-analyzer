"""CSV import service for Fidelity account history exports."""

import csv
import re
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from io import StringIO
from typing import Optional
from dataclasses import dataclass


# Number of days past expiration to wait before auto-marking an OPEN position
# as EXPIRED. Brokers can take 1-3 business days to post assignment trades
# after expiration weekend; if we flip outcome=EXPIRED immediately, a late-
# arriving assignment trade either silently misses the underlying-linker
# (because the position is already considered closed/expired) or flips
# outcome=ASSIGNED at a time when downstream code may have already cached
# the wrong state. 5 calendar days covers a Friday expiration plus the
# following weekend and a settlement lag.
AUTO_EXPIRY_GRACE_DAYS = 5

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import OptionContract, OptionTrade, OptionPosition, ImportLog, UnderlyingTrade


@dataclass
class ParsedContract:
    """Parsed options contract data."""
    symbol: str
    expiration: datetime
    strike: Decimal
    option_type: str


@dataclass
class ParsedTrade:
    """Parsed trade data from CSV."""
    contract: ParsedContract
    trade_date: datetime
    settlement_date: Optional[datetime]
    action: str
    quantity: int
    price: Decimal
    commission: Decimal
    fees: Decimal
    amount: Decimal
    raw_symbol: str


@dataclass
class ParsedUnderlyingTrade:
    """Parsed underlying stock trade from assignment/exercise."""
    symbol: str
    trade_date: datetime
    action: str  # BUY or SELL
    quantity: int
    price: Decimal
    amount: Decimal
    trade_type: str  # ASSIGNMENT, COVER
    linked_option_symbol: Optional[str]  # To link back to option position


def parse_option_symbol(symbol: str, description: str) -> Optional[ParsedContract]:
    """
    Parse Fidelity option symbol format.
    Symbol: -AEYE251121P13
    Description: PUT (AEYE) AUDIOEYE INC COM NEW NOV 21 25 $13 (100 SHS)
    """
    if not symbol or not symbol.startswith('-'):
        return None

    # Parse from description which is more readable
    match = re.search(r'(PUT|CALL)\s+\((\w+)\).*?(\w{3})\s+(\d{2})\s+(\d{2})\s+\$([0-9.]+)', description)
    if match:
        option_type = match.group(1)
        underlying = match.group(2)
        month_str = match.group(3)
        day = int(match.group(4))
        year = int(match.group(5)) + 2000
        strike = Decimal(match.group(6))

        months = {'JAN': 1, 'FEB': 2, 'MAR': 3, 'APR': 4, 'MAY': 5, 'JUN': 6,
                  'JUL': 7, 'AUG': 8, 'SEP': 9, 'OCT': 10, 'NOV': 11, 'DEC': 12}
        month = months.get(month_str.upper(), 1)

        expiration = datetime(year, month, day)
        return ParsedContract(underlying, expiration, strike, option_type)

    return None


def parse_csv_content(content: str, account_filter: str = "INDIVIDUAL") -> tuple[list[ParsedTrade], list[ParsedUnderlyingTrade]]:
    """Parse Fidelity CSV export content and extract options trades and underlying stock trades."""
    trades = []
    underlying_trades = []
    lines = content.splitlines()

    # Find header row
    header_idx = None
    for i, line in enumerate(lines):
        if line.startswith('Run Date,'):
            header_idx = i
            break

    if header_idx is None:
        return [], []

    csv_content = '\n'.join(lines[header_idx:])
    reader = csv.DictReader(StringIO(csv_content))

    for row in reader:
        # Only filter by account if the Account column exists in the CSV
        if 'Account' in row:
            account = (row.get('Account') or '').strip()
            if account_filter and account != account_filter:
                continue

        action = row.get('Action') or ''
        symbol = (row.get('Symbol') or '').strip()
        description = row.get('Description') or ''

        # Check for underlying stock trades from assignments
        # Pattern: "YOU BOUGHT ASSIGNED PUTS AS OF..." or "YOU SOLD ... (Cash)"
        if not symbol.startswith('-') and symbol:
            underlying_trade = _parse_underlying_trade(row, action, symbol, description)
            if underlying_trade:
                underlying_trades.append(underlying_trade)
            continue

        # Only process options transactions
        if not symbol.startswith('-'):
            continue

        # Skip non-trade actions
        if not any(keyword in action for keyword in ['SOLD', 'BOUGHT', 'EXPIRED', 'ASSIGNED']):
            continue

        contract = parse_option_symbol(symbol, description)
        if not contract:
            continue

        try:
            trade_date = datetime.strptime(row['Run Date'], '%m/%d/%Y')

            settlement_str = row.get('Settlement Date', '').strip()
            settlement_date = None
            if settlement_str:
                try:
                    settlement_date = datetime.strptime(settlement_str, '%m/%d/%Y')
                except ValueError:
                    pass

            # Fidelity CSV column mapping
            quantity = int(float(row.get('Quantity', '0') or '0'))
            price = Decimal(row.get('Price', '0') or '0')
            commission = Decimal(row.get('Commission', '0') or '0')
            fees = Decimal(row.get('Fees', '0') or '0')
            amount_str = (row.get('Amount', '0') or '0').replace(',', '')
            amount = Decimal(amount_str)

        except (ValueError, InvalidOperation):
            continue

        trades.append(ParsedTrade(
            contract=contract,
            trade_date=trade_date,
            settlement_date=settlement_date,
            action=action,
            quantity=quantity,
            price=price,
            commission=commission,
            fees=fees,
            amount=amount,
            raw_symbol=symbol
        ))

    return trades, underlying_trades


def _parse_underlying_trade(row: dict, action: str, symbol: str, description: str) -> Optional[ParsedUnderlyingTrade]:
    """Parse underlying stock trade from assignment or covering sale."""
    # Skip non-stock transactions (interest, dividends, etc.)
    if 'DIVIDEND' in action or 'INTEREST' in action or 'REINVESTMENT' in action:
        return None
    if 'ELECTRONIC FUNDS' in action.upper():
        return None
    if 'CONTRIBUTION' in action.upper():
        return None
    if 'LOAN' in action.upper():
        return None
    if 'COLLATERAL' in action.upper():
        return None

    # Check for assignment-related stock trades
    # Pattern: "YOU BOUGHT ASSIGNED PUTS AS OF 10-17-25 WEBULL..."
    assignment_match = re.search(r'ASSIGNED\s+(PUTS|CALLS)\s+AS\s+OF\s+(\d{1,2}-\d{1,2}-\d{2,4})', action, re.IGNORECASE)

    # Check for regular stock buy/sell that might be covering an assigned position
    is_stock_trade = ('YOU BOUGHT' in action or 'YOU SOLD' in action) and not assignment_match

    if not assignment_match and not is_stock_trade:
        return None

    try:
        trade_date = datetime.strptime(row['Run Date'], '%m/%d/%Y')

        # Fidelity CSV column mapping
        quantity_raw = row.get('Quantity', '0') or '0'
        quantity = int(float(quantity_raw)) if quantity_raw else 0

        price_str = row.get('Price', '0') or '0'
        price = Decimal(price_str) if price_str else Decimal(0)

        amount_str = (row.get('Amount', '0') or '0').replace(',', '')
        amount = Decimal(amount_str)

        if quantity == 0:
            return None

    except (ValueError, InvalidOperation):
        return None

    # Determine trade type and action
    if assignment_match:
        trade_type = 'ASSIGNMENT'
        # Assigned puts = you bought stock, assigned calls = you sold stock
        option_type = assignment_match.group(1).upper()
        if option_type == 'PUTS':
            trade_action = 'BUY'
        else:
            trade_action = 'SELL'
    else:
        # Regular stock trade - could be covering an assigned position
        trade_type = 'COVER'
        if 'SOLD' in action:
            trade_action = 'SELL'
        else:
            trade_action = 'BUY'

    return ParsedUnderlyingTrade(
        symbol=symbol,
        trade_date=trade_date,
        action=trade_action,
        quantity=abs(quantity),
        price=abs(price),
        amount=amount,
        trade_type=trade_type,
        linked_option_symbol=None  # Will be linked during position update
    )


async def get_or_create_contract(db: AsyncSession, parsed: ParsedContract) -> OptionContract:
    """Get existing contract or create new one."""
    stmt = select(OptionContract).where(
        OptionContract.symbol == parsed.symbol,
        OptionContract.expiration == parsed.expiration.date(),
        OptionContract.strike == parsed.strike,
        OptionContract.option_type == parsed.option_type
    )
    result = await db.execute(stmt)
    contract = result.scalar_one_or_none()

    if not contract:
        contract = OptionContract(
            symbol=parsed.symbol,
            expiration=parsed.expiration.date(),
            strike=parsed.strike,
            option_type=parsed.option_type
        )
        db.add(contract)
        await db.flush()

    return contract


async def trade_exists(db: AsyncSession, contract_id: int, trade_date: datetime, action: str, amount: Decimal) -> bool:
    """Check if a trade already exists to prevent duplicates."""
    stmt = select(OptionTrade).where(
        OptionTrade.contract_id == contract_id,
        OptionTrade.trade_date == trade_date,
        OptionTrade.action == action,
        OptionTrade.amount == amount
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none() is not None


def determine_strategy(trades: list[OptionTrade], option_type: str) -> str:
    """Determine position strategy from trades."""
    opening_trades = [t for t in trades if 'OPENING' in t.action]
    if opening_trades:
        if 'SOLD' in opening_trades[0].action:
            return f"SHORT {option_type}"
        else:
            return f"LONG {option_type}"
    return "UNKNOWN"


def determine_outcome(trades: list[OptionTrade]) -> str:
    """Determine how position was closed."""
    close_actions = [t.action for t in trades if 'OPENING' not in t.action]
    if any('EXPIRED' in a for a in close_actions):
        return 'EXPIRED'
    elif any('ASSIGNED' in a for a in close_actions):
        return 'ASSIGNED'
    elif any('CLOSING' in a for a in close_actions):
        return 'CLOSED'
    return 'OPEN'


async def update_position(db: AsyncSession, contract: OptionContract) -> OptionPosition:
    """Update or create position record for a contract."""
    from sqlalchemy.orm import selectinload

    stmt = select(OptionTrade).where(OptionTrade.contract_id == contract.id).order_by(OptionTrade.trade_date)
    result = await db.execute(stmt)
    trades = list(result.scalars().all())

    if not trades:
        return None

    total_qty = sum(t.quantity for t in trades)

    total_premium = sum(t.amount for t in trades)
    total_commission = sum(t.commission for t in trades)
    total_fees = sum(t.fees for t in trades)

    strategy = determine_strategy(trades, contract.option_type)
    outcome = determine_outcome(trades)

    # Auto-EXPIRED only after a grace period. If the contract is past its
    # expiration date but within the grace window, leave outcome=OPEN so a
    # late-arriving assignment trade in a subsequent import can still flip
    # the outcome to ASSIGNED and trigger underlying-trade linking. Without
    # the grace period, we'd lock in EXPIRED on day 1 and any real assignment
    # arriving on day 2-3 would silently mis-attribute P&L.
    today = datetime.now().date()
    contract_expired = contract.expiration < today
    past_grace = contract.expiration < (today - timedelta(days=AUTO_EXPIRY_GRACE_DAYS))
    if contract_expired and outcome == 'OPEN' and past_grace:
        outcome = 'EXPIRED'

    # Position is closed if quantity is zero OR if it expired/was assigned.
    # During the grace window a quantity-still-open position stays is_closed=False
    # so re-imports can amend it; the risk page already filters past-expiration
    # positions out of risk display.
    is_closed = total_qty == 0 or outcome in ('EXPIRED', 'ASSIGNED')

    # Get or create position
    stmt = select(OptionPosition).options(
        selectinload(OptionPosition.underlying_trades)
    ).where(OptionPosition.contract_id == contract.id)
    result = await db.execute(stmt)
    position = result.scalar_one_or_none()

    open_date = min(t.trade_date for t in trades)
    # For expired positions without explicit closing trade, use expiration date
    if is_closed:
        if contract_expired and outcome == 'EXPIRED':
            close_date = datetime.combine(contract.expiration, datetime.min.time())
        else:
            close_date = max(t.trade_date for t in trades)
    else:
        close_date = None
    
    # Calculate num_contracts: largest absolute position size ever held.
    # Walk the trades in date order, tracking running net quantity, and take
    # the max absolute. This correctly accounts for scaling in/out (e.g.,
    # sold 5 then sold 5 more should be 10, not max(5,5)=5). Fidelity sells
    # are negative quantities and buys positive, so signed sums work directly.
    # Trades are already ordered by trade_date from the query above.
    running_qty = 0
    max_abs_qty = 0
    for t in trades:
        running_qty += t.quantity
        if abs(running_qty) > max_abs_qty:
            max_abs_qty = abs(running_qty)
    num_contracts = max_abs_qty if max_abs_qty > 0 else max(abs(t.quantity) for t in trades)

    if not position:
        position = OptionPosition(
            contract_id=contract.id,
            is_closed=is_closed,
            open_date=open_date,
            close_date=close_date,
            strategy=strategy,
            outcome=outcome,
            total_premium=total_premium,
            total_commission=total_commission,
            total_fees=total_fees,
            net_pnl=total_premium,
            num_contracts=num_contracts,
            underlying_pnl=Decimal(0),
            total_pnl=total_premium
        )
        db.add(position)
        await db.flush()
    else:
        position.is_closed = is_closed
        position.open_date = open_date
        position.close_date = close_date
        position.strategy = strategy
        position.outcome = outcome
        position.total_premium = total_premium
        position.total_commission = total_commission
        position.total_fees = total_fees
        position.net_pnl = total_premium
        position.num_contracts = num_contracts

        # Calculate underlying P&L from linked trades
        underlying_pnl = sum(t.amount for t in position.underlying_trades) if position.underlying_trades else Decimal(0)
        position.underlying_pnl = underlying_pnl
        position.total_pnl = total_premium + underlying_pnl

    return position


async def underlying_trade_exists(db: AsyncSession, position_id: int, trade_date: datetime, amount: Decimal) -> bool:
    """Check if an underlying trade already exists."""
    stmt = select(UnderlyingTrade).where(
        UnderlyingTrade.position_id == position_id,
        UnderlyingTrade.trade_date == trade_date,
        UnderlyingTrade.amount == amount
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none() is not None


def _pick_position_for_underlying(
    ut: ParsedUnderlyingTrade,
    candidates: list[OptionPosition]
) -> Optional[OptionPosition]:
    """
    Pick the best assigned-option position to attach an underlying trade to.

    Order of disambiguation:
    1. Direction: PUT assignment ⇒ user BOUGHT stock; CALL assignment ⇒ SOLD.
       Drop candidates whose option type contradicts the trade direction so
       we never attribute a put-assignment buy to a call-assignment leg on
       the same symbol.
    2. Strike vs trade price: assignments happen at the option's strike, so
       among same-direction candidates the strike closest to the actual
       per-share trade price is overwhelmingly likely to be the right one.
       This catches the multi-strike same-symbol case (e.g., two short
       AAPL puts at $90 and $95 — only one was assigned).
    3. Expiration vs trade date: tertiary tiebreaker. Assignments usually
       process within a day of expiration.
    """
    if not candidates:
        return None

    if ut.trade_type == 'ASSIGNMENT':
        if ut.action == 'BUY':
            matched = [p for p in candidates if p.contract.option_type == 'PUT']
        else:  # SELL
            matched = [p for p in candidates if p.contract.option_type == 'CALL']
    else:
        # Cover trades (manual buy/sell to flatten): match either direction
        matched = list(candidates)

    if not matched:
        # No directional match; bail rather than attribute to the wrong leg
        return None

    if len(matched) == 1:
        return matched[0]

    trade_price = float(ut.price) if ut.price else 0.0

    def _score(p: OptionPosition) -> tuple[float, int]:
        strike_dist = abs(float(p.contract.strike) - trade_price) if trade_price > 0 else 0.0
        exp_dist = abs((p.contract.expiration - ut.trade_date.date()).days)
        # Sort by strike-distance first (primary), expiration-distance second.
        return (strike_dist, exp_dist)

    return min(matched, key=_score)


async def link_underlying_trades(
    db: AsyncSession,
    underlying_trades: list[ParsedUnderlyingTrade],
    assigned_positions: dict[str, list[OptionPosition]]
) -> int:
    """Link underlying stock trades to their corresponding option positions.

    `assigned_positions` is now a dict of symbol -> list of candidate positions
    (one symbol can have multiple simultaneous assignments). The picker uses
    trade direction + nearest expiration to choose the right one.
    """
    linked_count = 0

    for ut in underlying_trades:
        candidates = assigned_positions.get(ut.symbol, [])
        position = _pick_position_for_underlying(ut, candidates)

        if not position:
            continue

        # Check for duplicates
        if await underlying_trade_exists(db, position.id, ut.trade_date, ut.amount):
            continue

        underlying_trade = UnderlyingTrade(
            position_id=position.id,
            symbol=ut.symbol,
            trade_date=ut.trade_date,
            action=ut.action,
            quantity=ut.quantity,
            price=ut.price,
            amount=ut.amount,
            trade_type=ut.trade_type
        )
        db.add(underlying_trade)
        linked_count += 1

    return linked_count


async def import_csv(db: AsyncSession, content: str, filename: str) -> tuple[int, int]:
    """
    Import trades from CSV content.
    Returns (imported_count, skipped_count).
    """
    from sqlalchemy.orm import selectinload

    parsed_trades, parsed_underlying = parse_csv_content(content)

    imported = 0
    skipped = 0
    affected_contracts = set()

    for parsed in parsed_trades:
        contract = await get_or_create_contract(db, parsed.contract)

        if await trade_exists(db, contract.id, parsed.trade_date, parsed.action, parsed.amount):
            skipped += 1
            continue

        trade = OptionTrade(
            contract_id=contract.id,
            trade_date=parsed.trade_date,
            settlement_date=parsed.settlement_date,
            action=parsed.action,
            quantity=parsed.quantity,
            price=parsed.price,
            commission=parsed.commission,
            fees=parsed.fees,
            amount=parsed.amount,
            raw_symbol=parsed.raw_symbol
        )
        db.add(trade)
        imported += 1
        affected_contracts.add(contract)

    await db.flush()

    # Update positions for affected contracts.
    # Build symbol -> [positions] so the linker can pick by direction (PUT vs
    # CALL) when multiple assignments exist on the same underlying.
    assigned_positions: dict[str, list[OptionPosition]] = {}
    for contract in affected_contracts:
        position = await update_position(db, contract)
        if position and position.outcome == 'ASSIGNED':
            assigned_positions.setdefault(contract.symbol, []).append(position)

    # Also find any previously assigned positions for linking
    stmt = select(OptionPosition).options(
        selectinload(OptionPosition.contract)
    ).where(OptionPosition.outcome == 'ASSIGNED')
    result = await db.execute(stmt)
    for pos in result.scalars().all():
        bucket = assigned_positions.setdefault(pos.contract.symbol, [])
        if pos not in bucket:
            bucket.append(pos)

    # Link underlying trades to assigned positions
    if parsed_underlying and assigned_positions:
        linked = await link_underlying_trades(db, parsed_underlying, assigned_positions)
        imported += linked

        # Update underlying P&L for every potentially affected position
        await db.flush()
        for symbol, positions in assigned_positions.items():
            for position in positions:
                stmt = select(OptionPosition).options(
                    selectinload(OptionPosition.underlying_trades)
                ).where(OptionPosition.id == position.id)
                result = await db.execute(stmt)
                pos = result.scalar_one_or_none()
                if pos:
                    underlying_pnl = sum(t.amount for t in pos.underlying_trades) if pos.underlying_trades else Decimal(0)
                    pos.underlying_pnl = underlying_pnl
                    pos.total_pnl = pos.net_pnl + underlying_pnl

    # Log the import
    log = ImportLog(
        filename=filename,
        records_imported=imported,
        records_skipped=skipped
    )
    db.add(log)

    await db.commit()
    return imported, skipped
