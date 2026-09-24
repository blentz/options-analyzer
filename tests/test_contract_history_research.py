"""Syncing history for contracts that are only being researched.

The speculation page builds strategies from contracts the user does not
hold. Those have no option_contracts row, and ContractHistory.contract_id is
a foreign key — so the sync has to create the row on demand. That widens
what option_contracts means, from "contracts you have traded" to "contracts
you have traded or researched", which is why these tests pin the shape
explicitly rather than leaving it implied.
"""

from datetime import date, datetime
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import (
    Base, ContractHistory, ContractHistorySync, OptionContract, OptionPosition,
)
from app.services.contract_history import ContractRef, sync_contract_history

from tests.history_fixture import FIXTURE_ROWS, fixture_fetcher


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(text("PRAGMA foreign_keys=ON"))
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as session:
        yield session
    await engine.dispose()


async def _held(db, symbol="HITI", strike="2.50"):
    c = OptionContract(
        symbol=symbol, expiration=date(2026, 10, 16),
        strike=Decimal(strike), option_type="PUT",
    )
    db.add(c)
    await db.flush()
    db.add(OptionPosition(
        contract_id=c.id, is_closed=False,
        open_date=datetime(2026, 2, 19), strategy="SHORT PUT",
    ))
    await db.flush()
    return c


@pytest.mark.asyncio
async def test_researched_contract_is_created_and_synced(db):
    """A leg the user does not hold still gets history."""
    await db.commit()
    ref = ContractRef(symbol="AAPL", expiration=date(2026, 12, 18),
                      strike=Decimal("200.00"), option_type="CALL")

    summary = await sync_contract_history(db, extra=[ref], fetcher=fixture_fetcher)

    assert summary.synced == 1
    assert summary.failed == 0

    created = (await db.execute(
        select(OptionContract).where(OptionContract.symbol == "AAPL")
    )).scalars().one()
    assert created.strike == Decimal("200.00")

    rows = (await db.execute(
        select(func.count()).select_from(ContractHistory)
        .where(ContractHistory.contract_id == created.id)
    )).scalar()
    assert rows == FIXTURE_ROWS


@pytest.mark.asyncio
async def test_researched_contract_creates_no_position(db):
    """Researching is not holding — no OptionPosition may appear."""
    await db.commit()
    ref = ContractRef(symbol="AAPL", expiration=date(2026, 12, 18),
                      strike=Decimal("200.00"), option_type="CALL")

    await sync_contract_history(db, extra=[ref], fetcher=fixture_fetcher)

    positions = (await db.execute(select(func.count()).select_from(OptionPosition))).scalar()
    assert positions == 0


@pytest.mark.asyncio
async def test_existing_contract_is_reused_not_duplicated(db):
    """A researched leg matching a held contract must not create a second row."""
    held = await _held(db)
    await db.commit()
    ref = ContractRef(symbol="HITI", expiration=date(2026, 10, 16),
                      strike=Decimal("2.50"), option_type="PUT")

    summary = await sync_contract_history(db, extra=[ref], fetcher=fixture_fetcher)

    contracts = (await db.execute(select(func.count()).select_from(OptionContract))).scalar()
    assert contracts == 1
    # Counted once, not once as held and again as researched.
    assert summary.contracts_total == 1
    assert summary.synced == 1

    statuses = (await db.execute(select(func.count()).select_from(ContractHistorySync))).scalar()
    assert statuses == 1


@pytest.mark.asyncio
async def test_held_and_researched_are_unioned(db):
    await _held(db)
    await db.commit()
    ref = ContractRef(symbol="AAPL", expiration=date(2026, 12, 18),
                      strike=Decimal("200.00"), option_type="CALL")

    summary = await sync_contract_history(db, extra=[ref], fetcher=fixture_fetcher)

    assert summary.contracts_total == 2
    assert summary.synced == 2


@pytest.mark.asyncio
async def test_no_extra_still_syncs_open_positions(db):
    """The old behaviour must survive the signature change."""
    await _held(db)
    await db.commit()

    summary = await sync_contract_history(db, fetcher=fixture_fetcher)

    assert summary.contracts_total == 1
    assert summary.synced == 1


@pytest.mark.asyncio
async def test_unbuildable_researched_contract_fails_alone(db):
    """A leg whose symbol cannot form an OCC symbol fails itself, not the batch."""
    await _held(db)
    await db.commit()
    bad = ContractRef(symbol="BRK.B", expiration=date(2026, 12, 18),
                      strike=Decimal("200.00"), option_type="CALL")

    summary = await sync_contract_history(db, extra=[bad], fetcher=fixture_fetcher)

    assert summary.synced == 1
    assert summary.failed == 1
    assert any("BRK.B" in e.contract or "OCC" in e.error for e in summary.errors)
