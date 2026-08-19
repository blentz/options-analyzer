"""Upsert tests. Each download is a complete history, so re-syncing must
converge rather than duplicate or drift.
"""

from datetime import date
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import Base, ContractHistory, OptionContract
from app.services.contract_history import HistoryRow, upsert_history


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


async def _contract(db):
    c = OptionContract(
        symbol="HITI", expiration=date(2026, 10, 16),
        strike=Decimal("2.50"), option_type="PUT",
    )
    db.add(c)
    await db.flush()
    return c


def _row(d, close="0.30", oi=3784):
    return HistoryRow(date=d, close=Decimal(close), open_interest=oi)


@pytest.mark.asyncio
async def test_inserts_rows(db):
    c = await _contract(db)
    written = await upsert_history(db, c.id, [
        _row(date(2026, 8, 18)), _row(date(2026, 8, 19)),
    ])
    await db.commit()
    assert written == 2
    count = (await db.execute(select(func.count()).select_from(ContractHistory))).scalar()
    assert count == 2


@pytest.mark.asyncio
async def test_idempotent(db):
    """Re-syncing the same history must not duplicate rows."""
    c = await _contract(db)
    rows = [_row(date(2026, 8, 18)), _row(date(2026, 8, 19))]
    await upsert_history(db, c.id, rows)
    await db.commit()
    await upsert_history(db, c.id, rows)
    await db.commit()

    count = (await db.execute(select(func.count()).select_from(ContractHistory))).scalar()
    assert count == 2


@pytest.mark.asyncio
async def test_restated_value_overwrites(db):
    """A later fetch correcting an earlier value must win."""
    c = await _contract(db)
    await upsert_history(db, c.id, [_row(date(2026, 8, 19), close="0.30", oi=3784)])
    await db.commit()
    await upsert_history(db, c.id, [_row(date(2026, 8, 19), close="0.35", oi=3800)])
    await db.commit()

    got = (await db.execute(select(ContractHistory))).scalars().one()
    assert got.close == Decimal("0.35")
    assert got.open_interest == 3800


@pytest.mark.asyncio
async def test_extends_tail(db):
    """The common case: yesterday's history plus one new day."""
    c = await _contract(db)
    await upsert_history(db, c.id, [_row(date(2026, 8, 18))])
    await db.commit()
    await upsert_history(db, c.id, [_row(date(2026, 8, 18)), _row(date(2026, 8, 19))])
    await db.commit()

    dates = (await db.execute(select(ContractHistory.date).order_by(ContractHistory.date))).scalars().all()
    assert dates == [date(2026, 8, 18), date(2026, 8, 19)]


@pytest.mark.asyncio
async def test_contracts_are_isolated(db):
    c1 = await _contract(db)
    c2 = OptionContract(
        symbol="HITI", expiration=date(2027, 1, 15),
        strike=Decimal("2.50"), option_type="PUT",
    )
    db.add(c2)
    await db.flush()

    await upsert_history(db, c1.id, [_row(date(2026, 8, 19), close="0.30")])
    await upsert_history(db, c2.id, [_row(date(2026, 8, 19), close="0.43")])
    await db.commit()

    count = (await db.execute(select(func.count()).select_from(ContractHistory))).scalar()
    assert count == 2
