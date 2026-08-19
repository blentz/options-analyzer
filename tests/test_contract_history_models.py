"""Schema tests for contract history storage."""

from datetime import date, datetime
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import Base, ContractHistory, ContractHistorySync, OptionContract


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


async def _contract(db):
    c = OptionContract(
        symbol="HITI",
        expiration=date(2026, 10, 16),
        strike=Decimal("2.50"),
        option_type="PUT",
    )
    db.add(c)
    await db.flush()
    return c


@pytest.mark.asyncio
async def test_history_row_persists(db):
    c = await _contract(db)
    db.add(ContractHistory(
        contract_id=c.id,
        date=date(2026, 8, 19),
        close=Decimal("0.30"),
        volume=1,
        open_interest=3784,
        implied_volatility=0.5812,
        delta=-0.4856,
    ))
    await db.commit()

    got = (await db.execute(select(ContractHistory))).scalars().one()
    assert got.close == Decimal("0.30")
    assert got.open_interest == 3784


@pytest.mark.asyncio
async def test_nulls_allowed_everywhere_but_identity(db):
    """February rows have almost nothing populated; the schema must accept them."""
    c = await _contract(db)
    db.add(ContractHistory(contract_id=c.id, date=date(2026, 2, 19)))
    await db.commit()

    got = (await db.execute(select(ContractHistory))).scalars().one()
    assert got.close is None
    assert got.ultima is None


@pytest.mark.asyncio
async def test_one_row_per_contract_per_date(db):
    c = await _contract(db)
    db.add(ContractHistory(contract_id=c.id, date=date(2026, 8, 19)))
    await db.commit()
    db.add(ContractHistory(contract_id=c.id, date=date(2026, 8, 19)))
    with pytest.raises(IntegrityError):
        await db.commit()


@pytest.mark.asyncio
async def test_keyword_columns_named_bare_in_sql(db):
    """open_ and lambda_ map to `open` and `lambda` in SQL."""
    cols = {c.name for c in ContractHistory.__table__.columns}
    assert "open" in cols and "open_" not in cols
    assert "lambda" in cols and "lambda_" not in cols


@pytest.mark.asyncio
async def test_sync_status_persists(db):
    c = await _contract(db)
    db.add(ContractHistorySync(
        contract_id=c.id,
        last_attempt_at=datetime(2026, 8, 19, 12, 0, 0),
        last_error="ProGatedError: served HITI260821P00002500",
    ))
    await db.commit()

    got = (await db.execute(select(ContractHistorySync))).scalars().one()
    assert got.last_success_at is None
    assert "ProGated" in got.last_error
