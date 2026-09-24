"""Orchestration tests. The fetcher is injected, so none of this touches
the network.
"""

import json
import tempfile
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import (
    Base, ContractHistory, ContractHistorySync, OptionContract, OptionPosition,
)
from app.services import contract_history
from app.services.contract_history import occ_symbol, sync_contract_history
from app.services.stocknear_contract_api import StockNearAPIError

from tests.history_fixture import FIXTURE_ROWS, fixture_fetcher as _fixture_fetcher, payload_for as _payload_for


@pytest.fixture(autouse=True)
def private_tempdir(monkeypatch, tmp_path):
    """Keep retained failure payloads out of the real /tmp."""
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))


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


async def _open_position(db, symbol="HITI", strike="2.50", exp=date(2026, 10, 16)):
    c = OptionContract(
        symbol=symbol, expiration=exp,
        strike=Decimal(strike), option_type="PUT",
    )
    db.add(c)
    await db.flush()
    # open_date and strategy are NOT NULL with no default (app/models.py:67-71).
    db.add(OptionPosition(
        contract_id=c.id,
        is_closed=False,
        open_date=datetime(2026, 2, 19),
        strategy="SHORT PUT",
    ))
    await db.flush()
    return c


async def _failing_fetcher(symbol, occ):
    raise StockNearAPIError(f"Contract request failed for {occ}: 403 Forbidden")


@pytest.mark.asyncio
async def test_syncs_open_position(db):
    await _open_position(db)
    await db.commit()

    summary = await sync_contract_history(db, fetcher=_fixture_fetcher)

    assert summary.contracts_total == 1
    assert summary.synced == 1
    assert summary.failed == 0
    assert summary.rows_upserted == FIXTURE_ROWS

    count = (await db.execute(select(func.count()).select_from(ContractHistory))).scalar()
    assert count == FIXTURE_ROWS


@pytest.mark.asyncio
async def test_skips_closed_positions(db):
    c = await _open_position(db)
    pos = (await db.execute(select(OptionPosition))).scalars().one()
    pos.is_closed = True
    await db.commit()

    summary = await sync_contract_history(db, fetcher=_fixture_fetcher)
    assert summary.contracts_total == 0
    assert summary.synced == 0


@pytest.mark.asyncio
async def test_records_success_status(db):
    c = await _open_position(db)
    await db.commit()

    await sync_contract_history(db, fetcher=_fixture_fetcher)

    status = (await db.execute(select(ContractHistorySync))).scalars().one()
    assert status.last_success_at is not None
    assert status.last_error is None
    assert status.row_count == FIXTURE_ROWS


@pytest.mark.asyncio
async def test_records_failure_without_raising(db):
    await _open_position(db)
    await db.commit()

    summary = await sync_contract_history(db, fetcher=_failing_fetcher)

    assert summary.failed == 1
    assert summary.synced == 0
    assert len(summary.errors) == 1
    assert "403" in summary.errors[0].error

    status = (await db.execute(select(ContractHistorySync))).scalars().one()
    assert status.last_success_at is None
    assert "403" in status.last_error


@pytest.mark.asyncio
async def test_download_failure_with_no_message_keeps_exception_type(db):
    """N4: an exception constructed without arguments renders as an empty
    string via str(e). The fetch-failure branch must use the same
    "TypeName: message" shape as the parse-failure branch, so a bare
    `raise StockNearAPIError()` still names the exception type instead of
    leaving last_error / errors[] blank.
    """
    await _open_position(db)
    await db.commit()

    async def empty_message_fetcher(symbol, occ):
        raise StockNearAPIError()

    summary = await sync_contract_history(db, fetcher=empty_message_fetcher)

    assert summary.failed == 1
    assert summary.errors[0].error == "StockNearAPIError: "

    status = (await db.execute(select(ContractHistorySync))).scalars().one()
    assert status.last_error == "StockNearAPIError: "


@pytest.mark.asyncio
async def test_one_failure_does_not_abort_batch(db):
    good = await _open_position(db)
    bad = await _open_position(db, strike="5.00")
    await db.commit()

    async def mixed(symbol, occ):
        if "00005000" in occ:
            raise StockNearAPIError("503")
        return _payload_for(occ)

    summary = await sync_contract_history(db, fetcher=mixed)

    assert summary.contracts_total == 2
    assert summary.synced == 1
    assert summary.failed == 1


@pytest.mark.asyncio
async def test_unparseable_payload_is_recorded_and_retained(db):
    """A malformed payload must fail that contract, not the batch, and the
    payload must be saved for diagnosis.
    """
    await _open_position(db)
    await db.commit()

    async def bad_fetcher(symbol, occ):
        return {"message": "this is not a contract history"}

    summary = await sync_contract_history(db, fetcher=bad_fetcher)

    assert summary.failed == 1
    assert summary.synced == 0

    status = (await db.execute(select(ContractHistorySync))).scalars().one()
    assert status.last_success_at is None
    assert "kept at" in status.last_error

    # N5: errors[] carries the same message as last_error, including where
    # the payload was kept.
    assert summary.errors[0].error == status.last_error
    assert "contract-history-failures" in summary.errors[0].error

    kept = Path(tempfile.gettempdir()) / "contract-history-failures" / "HITI261016P00002500.json"
    assert json.loads(kept.read_text()) == {"message": "this is not a contract history"}


@pytest.mark.asyncio
async def test_unknown_contract_is_reported_without_a_kept_payload(db):
    await _open_position(db)
    await db.commit()

    async def unknown(symbol, occ):
        return []

    summary = await sync_contract_history(db, fetcher=unknown)

    assert summary.failed == 1
    assert summary.errors[0].error == (
        "ContractHistoryError: StockNear has no history for HITI261016P00002500 (unknown contract)"
    )


@pytest.mark.asyncio
async def test_substituted_contract_is_refused(db):
    """StockNear answering with a different contract's history must fail
    that contract and store nothing for it."""
    await _open_position(db)
    await db.commit()

    async def wrong_contract(symbol, occ):
        return _payload_for("HITI261023P00002500")

    summary = await sync_contract_history(db, fetcher=wrong_contract)

    assert summary.failed == 1
    assert "refusing to store" in summary.errors[0].error
    count = (await db.execute(select(func.count()).select_from(ContractHistory))).scalar()
    assert count == 0


@pytest.mark.asyncio
async def test_duplicate_date_failure_is_isolated_and_does_not_abort_batch(db, monkeypatch):
    """upsert_history only db.add()s; it never flushes. Without an explicit
    flush inside the per-contract try/except, an IntegrityError from the
    (contract_id, date) unique index surfaces later via autoflush -- on the
    *next* contract's query -- misattributing the failure to that contract
    and poisoning the session so the batch-wide commit raises, discarding
    every contract's successful writes. This must not happen: the bad
    contract fails on its own, attributed to itself, and the good contract
    still succeeds and commits.
    """
    good = await _open_position(db, symbol="AAA", strike="1.00", exp=date(2026, 10, 16))
    bad = await _open_position(db, symbol="BBB", strike="2.00", exp=date(2026, 10, 16))
    await db.commit()

    # A rollback inside sync_contract_history expires every object this
    # session tracks -- including `good` and `bad` -- so anything needed
    # after the call must be captured as a plain value now, not read off
    # the ORM objects later (that would raise MissingGreenlet under the
    # async driver).
    good_id = good.id
    bad_id = bad.id
    bad_occ = occ_symbol(bad)

    # The parser dedupes dates, so inject the duplicate at upsert time to
    # exercise the flush-inside-try isolation this test exists for.
    real_upsert = contract_history.upsert_history

    async def duplicating_upsert(db, contract_id, rows):
        if contract_id == bad_id:
            rows = rows + rows[:1]
        return await real_upsert(db, contract_id, rows)

    monkeypatch.setattr(contract_history, "upsert_history", duplicating_upsert)

    summary = await sync_contract_history(db, fetcher=_fixture_fetcher)

    assert summary.contracts_total == 2
    assert summary.synced == 1
    assert summary.failed == 1
    assert len(summary.errors) == 1
    assert summary.errors[0].contract == bad_occ

    good_status = (
        await db.execute(
            select(ContractHistorySync).where(
                ContractHistorySync.contract_id == good_id
            )
        )
    ).scalar_one()
    assert good_status.last_success_at is not None
    assert good_status.last_error is None

    bad_status = (
        await db.execute(
            select(ContractHistorySync).where(
                ContractHistorySync.contract_id == bad_id
            )
        )
    ).scalar_one()
    assert bad_status.last_success_at is None
    assert bad_status.last_error is not None

    good_history_count = (
        await db.execute(
            select(func.count())
            .select_from(ContractHistory)
            .where(ContractHistory.contract_id == good_id)
        )
    ).scalar()
    assert good_history_count == FIXTURE_ROWS


@pytest.mark.asyncio
async def test_invalid_symbol_is_isolated_and_does_not_abort_batch(db):
    """occ_symbol() raises for a ticker it cannot turn into a valid OCC
    symbol (e.g. a dotted ticker like "BRK.B") -- and does so BEFORE any
    download is attempted, while building job_meta. Without its own
    per-contract isolation, that raise would escape the job_meta
    construction and abort the whole sync call, leaving every other due
    contract -- including ones that would have synced fine -- with
    nothing recorded. Same failure shape N1 fixed for a batch-level
    browser failure, reintroduced through a different door.
    """
    good = await _open_position(db, symbol="AAA", strike="1.00", exp=date(2026, 10, 16))
    bad = await _open_position(db, symbol="BRK.B", strike="2.00", exp=date(2026, 10, 16))
    await db.commit()

    # A rollback anywhere in sync_contract_history expires every object this
    # session tracks, so anything needed after the call must be captured
    # as a plain value now (see the identical note on the "duplicate
    # date" test above).
    good_id = good.id
    bad_id = bad.id

    async def only_good(symbol, occ):
        # The bad contract must never reach the fetcher at all -- it
        # fails before job_meta is even built.
        assert symbol == "AAA"
        return _payload_for(occ)

    summary = await sync_contract_history(db, fetcher=only_good)

    assert summary.contracts_total == 2
    assert summary.synced == 1
    assert summary.failed == 1
    assert len(summary.errors) == 1
    assert "BRK.B" in summary.errors[0].error

    good_status = (
        await db.execute(
            select(ContractHistorySync).where(
                ContractHistorySync.contract_id == good_id
            )
        )
    ).scalar_one()
    assert good_status.last_success_at is not None
    assert good_status.last_error is None

    bad_status = (
        await db.execute(
            select(ContractHistorySync).where(
                ContractHistorySync.contract_id == bad_id
            )
        )
    ).scalar_one()
    assert bad_status.last_success_at is None
    assert bad_status.last_error is not None
    assert "BRK.B" in bad_status.last_error

    good_history_count = (
        await db.execute(
            select(func.count())
            .select_from(ContractHistory)
            .where(ContractHistory.contract_id == good_id)
        )
    ).scalar()
    assert good_history_count == FIXTURE_ROWS


@pytest.mark.asyncio
async def test_ttl_skips_recent_sync(db):
    c = await _open_position(db)
    db.add(ContractHistorySync(
        contract_id=c.id,
        last_success_at=datetime.utcnow() - timedelta(hours=1),
        row_count=124,
    ))
    await db.commit()

    summary = await sync_contract_history(db, fetcher=_fixture_fetcher)
    assert summary.skipped == 1
    assert summary.synced == 0


@pytest.mark.asyncio
async def test_force_overrides_ttl(db):
    c = await _open_position(db)
    db.add(ContractHistorySync(
        contract_id=c.id,
        last_success_at=datetime.utcnow() - timedelta(hours=1),
        row_count=124,
    ))
    await db.commit()

    summary = await sync_contract_history(db, force=True, fetcher=_fixture_fetcher)
    assert summary.skipped == 0
    assert summary.synced == 1


@pytest.mark.asyncio
async def test_stale_sync_is_refreshed(db):
    c = await _open_position(db)
    db.add(ContractHistorySync(
        contract_id=c.id,
        last_success_at=datetime.utcnow() - timedelta(days=2),
        row_count=100,
    ))
    await db.commit()

    summary = await sync_contract_history(db, fetcher=_fixture_fetcher)
    assert summary.skipped == 0
    assert summary.synced == 1
