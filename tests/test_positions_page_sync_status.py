"""B1: /positions must display per-contract history sync status.

Before this fix, ContractHistorySync.last_success_at and last_error were
write-only -- nothing outside app/services/contract_history.py read either
column. The transient status span rendered by the sync button's JS clears
on page reload, so a contract permanently failing (e.g. ProGatedError)
became invisible the moment the user navigated away: its history silently
stopped being collected and nothing showed it.

Uses a real in-memory DB and FastAPI's TestClient (not a browser) so this
exercises the actual /positions route and Jinja template end to end.
"""

from datetime import date, datetime

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import get_db
from app.main import app
from app.models import Base, ContractHistorySync, OptionContract, OptionPosition


@pytest_asyncio.fixture
async def db_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(text("PRAGMA foreign_keys=ON"))
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async def _override_get_db():
        async with Session() as session:
            yield session

    app.dependency_overrides[get_db] = _override_get_db
    try:
        async with Session() as session:
            yield session
    finally:
        app.dependency_overrides.pop(get_db, None)
        await engine.dispose()


async def _open_position(db, symbol, strike, exp=date(2026, 10, 16)):
    c = OptionContract(
        symbol=symbol, expiration=exp, strike=strike, option_type="PUT",
    )
    db.add(c)
    await db.flush()
    db.add(OptionPosition(
        contract_id=c.id,
        is_closed=False,
        open_date=datetime(2026, 2, 19),
        strategy="SHORT PUT",
    ))
    await db.flush()
    return c


@pytest.mark.asyncio
async def test_positions_page_shows_last_success_and_error(db_session):
    failing = await _open_position(db_session, "HITI", 2.50)
    healthy = await _open_position(db_session, "AAPL", 200.00)
    no_sync_yet = await _open_position(db_session, "MSFT", 400.00)

    db_session.add(ContractHistorySync(
        contract_id=failing.id,
        last_attempt_at=datetime(2026, 8, 19, 10, 0),
        last_success_at=None,
        last_error="ProGatedError: HITI261016P00002500 requires a higher subscription tier",
    ))
    db_session.add(ContractHistorySync(
        contract_id=healthy.id,
        last_attempt_at=datetime(2026, 8, 19, 9, 0),
        last_success_at=datetime(2026, 8, 19, 9, 0),
        last_error=None,
        row_count=42,
    ))
    await db_session.commit()

    client = TestClient(app)
    response = client.get("/positions")

    assert response.status_code == 200
    html = response.text

    # Failed contract: the error (or a truncation of it) must be visible
    # on the page, not just recorded in the DB.
    assert "requires a higher subscription tier" in html

    # Healthy contract: the last successful sync time is shown.
    assert "2026-08-19 09:00" in html

    # A contract that has never been synced shows no stale/misleading
    # status -- just renders without raising (implicitly checked by the
    # 200 above) and without claiming a sync that never happened.
    assert "MSFT" in html


@pytest.mark.asyncio
async def test_positions_page_truncates_long_error(db_session):
    c = await _open_position(db_session, "HITI", 2.50)
    long_error = "ProGatedError: " + "x" * 300
    db_session.add(ContractHistorySync(
        contract_id=c.id,
        last_attempt_at=datetime(2026, 8, 19, 10, 0),
        last_error=long_error,
    ))
    await db_session.commit()

    client = TestClient(app)
    response = client.get("/positions")
    html = response.text

    assert response.status_code == 200
    # The full error must not be dumped verbatim into the visible cell
    # text (a truncated form is expected instead).
    assert long_error not in html
