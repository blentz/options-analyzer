"""Tests for the wheel-cycle state machine.

End-to-end: feeds a real (in-memory SQLite) DB through detect_wheel_cycles_
for_symbol and asserts on the resulting WheelCycle rows. This is the most
useful coverage shape for the detector — its job is to walk events in time
order and produce correct P&L, so unit-testing the state machine in
isolation would mostly re-test the harness.
"""

import asyncio
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.database import Base
from app.models import (
    OptionContract, OptionPosition, OptionTrade, UnderlyingTrade,
    WheelCycle, WheelCycleMember,
)
from app.services.wheel_detection import detect_wheel_cycles_for_symbol


@pytest_asyncio.fixture
async def db():
    """In-memory SQLite session for one test."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(text("PRAGMA foreign_keys=ON"))
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as session:
        yield session
    await engine.dispose()


async def _make_contract(db, symbol, exp_date, strike, kind):
    c = OptionContract(symbol=symbol, expiration=exp_date, strike=Decimal(str(strike)), option_type=kind)
    db.add(c)
    await db.flush()
    return c


async def _make_position(db, contract, *, strategy, premium, opened, closed=None, outcome="OPEN", n=1):
    """Create an OptionPosition with one OPENING trade and (optionally) a
    closing event. Returns the position object."""
    pos = OptionPosition(
        contract_id=contract.id,
        is_closed=closed is not None,
        open_date=opened,
        close_date=closed,
        strategy=strategy,
        outcome=outcome if closed else "OPEN",
        total_premium=Decimal(str(premium)),
        net_pnl=Decimal(str(premium)),
        num_contracts=n,
    )
    db.add(pos)
    await db.flush()
    # Trades aren't strictly required for the detector (it walks positions
    # + underlying_trades), but adding the opening trade keeps the row
    # realistic if other tests share this helper.
    db.add(OptionTrade(
        contract_id=contract.id,
        trade_date=opened,
        action="SOLD OPENING" if "SHORT" in strategy else "BOUGHT OPENING",
        quantity=-n if "SHORT" in strategy else n,
        price=Decimal("1.0"),
        amount=Decimal(str(premium)),
        raw_symbol="-XYZ",
    ))
    await db.flush()
    return pos


async def _attach_underlying(db, position, *, action, qty, price, when):
    db.add(UnderlyingTrade(
        position_id=position.id,
        symbol=position.contract.symbol,
        trade_date=when,
        action=action,
        quantity=qty,
        price=Decimal(str(price)),
        amount=Decimal(str(qty * price * (-1 if action == "BUY" else 1))),
        trade_type="ASSIGNMENT",
    ))
    await db.flush()


@pytest.mark.asyncio
async def test_simple_winning_wheel(db):
    """Sell $95 CSP (+$200) → assigned at $95 → sell $100 CC (+$150) →
    called away at $100. True cycle P&L = +$850 (200 premium + 500 stock
    gain + 150 premium). This is the canonical case the dashboard used to
    show as ONE LOSS + ONE WIN.
    """
    exp1 = datetime(2025, 1, 17)
    exp2 = datetime(2025, 2, 21)
    put_c = await _make_contract(db, "XYZ", exp1.date(), 95, "PUT")
    call_c = await _make_contract(db, "XYZ", exp2.date(), 100, "CALL")

    put = await _make_position(
        db, put_c, strategy="SHORT PUT", premium=200,
        opened=datetime(2025, 1, 2), closed=exp1, outcome="ASSIGNED", n=1,
    )
    # Assignment: bought 100 shares at $95
    await _attach_underlying(db, put, action="BUY", qty=100, price=95, when=exp1)

    call = await _make_position(
        db, call_c, strategy="SHORT CALL", premium=150,
        opened=datetime(2025, 1, 20), closed=exp2, outcome="ASSIGNED", n=1,
    )
    # Called away: sold 100 shares at $100
    await _attach_underlying(db, call, action="SELL", qty=100, price=100, when=exp2)

    n = await detect_wheel_cycles_for_symbol(db, "XYZ")
    assert n == 1

    cycles = (await db.execute(select(WheelCycle))).scalars().all()
    assert len(cycles) == 1
    c = cycles[0]
    assert c.status == "CLOSED"
    assert c.num_puts == 1 and c.num_calls == 1
    assert c.options_pnl == Decimal("350")   # 200 + 150
    assert c.stock_pnl == Decimal("500")     # (100 - 95) * 100
    assert c.total_pnl == Decimal("850")
    assert c.is_winner


@pytest.mark.asyncio
async def test_losing_wheel_called_away_below_basis(db):
    """Edge case that shouldn't actually happen in normal wheel mechanics
    (call strike >= cost basis would be required), but exercise the
    math: ensure stock_pnl can be negative."""
    put_c = await _make_contract(db, "ZZZ", datetime(2025, 3, 21).date(), 100, "PUT")
    put = await _make_position(
        db, put_c, strategy="SHORT PUT", premium=100,
        opened=datetime(2025, 3, 1), closed=datetime(2025, 3, 21), outcome="ASSIGNED",
    )
    await _attach_underlying(db, put, action="BUY", qty=100, price=100, when=datetime(2025, 3, 21))
    # Manually sold at a loss (no covered call in this test — just verify math).
    await _attach_underlying(db, put, action="SELL", qty=100, price=90, when=datetime(2025, 3, 25))

    await detect_wheel_cycles_for_symbol(db, "ZZZ")
    c = (await db.execute(select(WheelCycle))).scalar_one()
    assert c.options_pnl == Decimal("100")
    assert c.stock_pnl == Decimal("-1000")   # (90 - 100) * 100
    assert c.total_pnl == Decimal("-900")
    assert not c.is_winner


@pytest.mark.asyncio
async def test_csp_expires_worthless_no_cycle_formed(db):
    """A SHORT PUT that expires OTM (no assignment, no shares acquired)
    forms a degenerate cycle of just one position. Win/loss attributed
    correctly at cycle level."""
    put_c = await _make_contract(db, "AAA", datetime(2025, 4, 18).date(), 80, "PUT")
    await _make_position(
        db, put_c, strategy="SHORT PUT", premium=50,
        opened=datetime(2025, 4, 1), closed=datetime(2025, 4, 18), outcome="EXPIRED",
    )

    await detect_wheel_cycles_for_symbol(db, "AAA")
    c = (await db.execute(select(WheelCycle))).scalar_one()
    assert c.status == "CLOSED"
    assert c.num_puts == 1 and c.num_calls == 0
    assert c.options_pnl == Decimal("50")
    assert c.stock_pnl == Decimal("0")
    assert c.total_pnl == Decimal("50")
    assert c.is_winner


@pytest.mark.asyncio
async def test_two_sequential_cycles_same_symbol(db):
    """User runs the wheel on XYZ twice with a gap. Should produce two
    independent WheelCycle rows, NOT one super-cycle."""
    sym = "XYZ"
    # Cycle 1: CSP expires worthless ($50 win)
    p1c = await _make_contract(db, sym, datetime(2025, 1, 17).date(), 90, "PUT")
    await _make_position(
        db, p1c, strategy="SHORT PUT", premium=50,
        opened=datetime(2025, 1, 1), closed=datetime(2025, 1, 17), outcome="EXPIRED",
    )
    # Gap of 30+ days — but cycle 1 closed cleanly so a new CSP starts cycle 2 regardless.
    # Cycle 2: CSP expires worthless ($75 win)
    p2c = await _make_contract(db, sym, datetime(2025, 4, 18).date(), 92, "PUT")
    await _make_position(
        db, p2c, strategy="SHORT PUT", premium=75,
        opened=datetime(2025, 3, 1), closed=datetime(2025, 4, 18), outcome="EXPIRED",
    )

    await detect_wheel_cycles_for_symbol(db, sym)
    cycles = (await db.execute(select(WheelCycle).order_by(WheelCycle.started_at))).scalars().all()
    assert len(cycles) == 2
    assert cycles[0].total_pnl == Decimal("50")
    assert cycles[1].total_pnl == Decimal("75")


@pytest.mark.asyncio
async def test_active_cycle_in_progress(db):
    """CSP assigned, currently holding shares, no CC yet. Cycle should be
    ACTIVE with shares_held populated and stock_pnl == 0 (no realisation)."""
    put_c = await _make_contract(db, "PEND", datetime(2025, 5, 16).date(), 50, "PUT")
    put = await _make_position(
        db, put_c, strategy="SHORT PUT", premium=100,
        opened=datetime(2025, 5, 1), closed=datetime(2025, 5, 16), outcome="ASSIGNED",
    )
    await _attach_underlying(db, put, action="BUY", qty=100, price=50, when=datetime(2025, 5, 16))

    await detect_wheel_cycles_for_symbol(db, "PEND")
    c = (await db.execute(select(WheelCycle))).scalar_one()
    assert c.status == "ACTIVE"
    assert c.shares_held == 100
    assert c.avg_cost_basis == Decimal("50")
    assert c.stock_pnl == Decimal("0")
    assert c.options_pnl == Decimal("100")
    assert c.total_pnl == Decimal("100")  # Just the premium so far


@pytest.mark.asyncio
async def test_idempotent_rebuild(db):
    """Running detection twice should yield the same final state — no
    duplicate cycles, no doubled P&L."""
    put_c = await _make_contract(db, "IDEM", datetime(2025, 6, 20).date(), 75, "PUT")
    await _make_position(
        db, put_c, strategy="SHORT PUT", premium=80,
        opened=datetime(2025, 6, 1), closed=datetime(2025, 6, 20), outcome="EXPIRED",
    )

    await detect_wheel_cycles_for_symbol(db, "IDEM")
    await detect_wheel_cycles_for_symbol(db, "IDEM")  # second run
    cycles = (await db.execute(select(WheelCycle))).scalars().all()
    assert len(cycles) == 1
    assert cycles[0].total_pnl == Decimal("80")
    # Members were re-created cleanly, not accumulated.
    members = (await db.execute(select(WheelCycleMember))).scalars().all()
    assert len(members) == 1
