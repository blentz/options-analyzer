"""CSV import service for Fidelity account history exports."""

import csv
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from io import StringIO
from typing import Optional
from dataclasses import dataclass

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

    # Check if contract has expired (past expiration date)
    contract_expired = contract.expiration < datetime.now().date()
    if contract_expired and outcome == 'OPEN':
        outcome = 'EXPIRED'

    # Position is closed if quantity is zero OR if it expired/was assigned
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
    num_contracts = max(abs(t.quantity) for t in trades)

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


async def link_underlying_trades(
    db: AsyncSession,
    underlying_trades: list[ParsedUnderlyingTrade],
    assigned_positions: dict[str, OptionPosition]
) -> int:
    """Link underlying stock trades to their corresponding option positions."""
    linked_count = 0

    for ut in underlying_trades:
        # Find a matching assigned position for this symbol
        position = assigned_positions.get(ut.symbol)

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

    # Update positions for affected contracts
    assigned_positions = {}  # symbol -> position (for linking underlying trades)
    for contract in affected_contracts:
        position = await update_position(db, contract)
        if position and position.outcome == 'ASSIGNED':
            assigned_positions[contract.symbol] = position

    # Also find any previously assigned positions for linking
    stmt = select(OptionPosition).options(
        selectinload(OptionPosition.contract)
    ).where(OptionPosition.outcome == 'ASSIGNED')
    result = await db.execute(stmt)
    for pos in result.scalars().all():
        if pos.contract.symbol not in assigned_positions:
            assigned_positions[pos.contract.symbol] = pos

    # Link underlying trades to assigned positions
    if parsed_underlying and assigned_positions:
        linked = await link_underlying_trades(db, parsed_underlying, assigned_positions)
        imported += linked

        # Update underlying P&L for affected positions
        await db.flush()
        for symbol, position in assigned_positions.items():
            # Reload position with underlying trades
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
