"""Orchestration tests. The downloader is injected, so none of this needs
a browser.
"""

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
from app.services.contract_history import occ_symbol, sync_open_positions
from app.stocknear_models import ProGatedError

FIXTURE = Path(__file__).parent / "fixtures" / "contract_history" / "HITI261016P00002500.csv"


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


def _fixture_downloader(symbol, occ_symbol, dest_dir):
    return FIXTURE


def _failing_downloader(symbol, occ_symbol, dest_dir):
    raise ProGatedError(f"{occ_symbol} requires a higher subscription tier")


@pytest.mark.asyncio
async def test_syncs_open_position(db):
    await _open_position(db)
    await db.commit()

    summary = await sync_open_positions(db, downloader=_fixture_downloader)

    assert summary.contracts_total == 1
    assert summary.synced == 1
    assert summary.failed == 0
    assert summary.rows_upserted == 124

    count = (await db.execute(select(func.count()).select_from(ContractHistory))).scalar()
    assert count == 124


@pytest.mark.asyncio
async def test_skips_closed_positions(db):
    c = await _open_position(db)
    pos = (await db.execute(select(OptionPosition))).scalars().one()
    pos.is_closed = True
    await db.commit()

    summary = await sync_open_positions(db, downloader=_fixture_downloader)
    assert summary.contracts_total == 0
    assert summary.synced == 0


@pytest.mark.asyncio
async def test_records_success_status(db):
    c = await _open_position(db)
    await db.commit()

    await sync_open_positions(db, downloader=_fixture_downloader)

    status = (await db.execute(select(ContractHistorySync))).scalars().one()
    assert status.last_success_at is not None
    assert status.last_error is None
    assert status.row_count == 124


@pytest.mark.asyncio
async def test_records_failure_without_raising(db):
    await _open_position(db)
    await db.commit()

    summary = await sync_open_positions(db, downloader=_failing_downloader)

    assert summary.failed == 1
    assert summary.synced == 0
    assert len(summary.errors) == 1
    assert "subscription" in summary.errors[0].error

    status = (await db.execute(select(ContractHistorySync))).scalars().one()
    assert status.last_success_at is None
    assert "subscription" in status.last_error


@pytest.mark.asyncio
async def test_one_failure_does_not_abort_batch(db):
    good = await _open_position(db)
    bad = await _open_position(db, strike="5.00")
    await db.commit()

    def mixed(symbol, occ_symbol, dest_dir):
        if "00005000" in occ_symbol:
            raise ProGatedError("gated")
        return FIXTURE

    summary = await sync_open_positions(db, downloader=mixed)

    assert summary.contracts_total == 2
    assert summary.synced == 1
    assert summary.failed == 1


@pytest.mark.asyncio
async def test_unparseable_download_is_recorded_and_retained(db):
    """A malformed CSV must fail that contract, not the batch, and the file
    must survive for diagnosis.

    The fake downloader writes its junk file into the dest_dir the
    orchestrator hands it (as a real downloader would), so this exercises
    the actual "copy before the TemporaryDirectory is cleaned" behavior
    rather than a file living outside it the whole time.
    """
    await _open_position(db)
    await db.commit()

    def bad_downloader(symbol, occ, dest_dir):
        junk = Path(dest_dir) / "junk.csv"
        junk.write_text("this is not a contract history csv\n")
        return junk

    summary = await sync_open_positions(db, downloader=bad_downloader)

    assert summary.failed == 1
    assert summary.synced == 0

    status = (await db.execute(select(ContractHistorySync))).scalars().one()
    assert status.last_success_at is None
    assert "kept at" in status.last_error

    kept = Path(tempfile.gettempdir()) / "contract-history-failures" / "HITI261016P00002500.csv"
    assert kept.exists()
    kept.unlink()


@pytest.mark.asyncio
async def test_duplicate_date_failure_is_isolated_and_does_not_abort_batch(db, tmp_path):
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

    # A rollback inside sync_open_positions expires every object this
    # session tracks -- including `good` and `bad` -- so anything needed
    # after the call must be captured as a plain value now, not read off
    # the ORM objects later (that would raise MissingGreenlet under the
    # async driver).
    good_id = good.id
    bad_id = bad.id
    bad_occ = occ_symbol(bad)

    header_fields = [
        "date", "open", "high", "low", "close", "bid", "ask", "mark",
        "volume", "open_interest", "changeOI", "dte",
        "implied_volatility", "changesPercentageOI",
        "delta", "gamma", "theta", "vega", "rho", "epsilon", "lambda",
        "charm", "vanna", "vomma", "veta", "vera", "speed", "zomma",
        "color", "ultima", "gex", "dex", "total_premium",
    ]
    row_values = ["2026-02-19"] + ["1"] * (len(header_fields) - 1)
    row = ",".join(row_values)
    dup_csv = tmp_path / "dup.csv"
    dup_csv.write_text(",".join(header_fields) + "\n" + row + "\n" + row + "\n")

    def mixed(symbol, occ, dest_dir):
        return dup_csv if symbol == "BBB" else FIXTURE

    summary = await sync_open_positions(db, downloader=mixed)

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
    assert good_history_count == 124


@pytest.mark.asyncio
async def test_ttl_skips_recent_sync(db):
    c = await _open_position(db)
    db.add(ContractHistorySync(
        contract_id=c.id,
        last_success_at=datetime.utcnow() - timedelta(hours=1),
        row_count=124,
    ))
    await db.commit()

    summary = await sync_open_positions(db, downloader=_fixture_downloader)
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

    summary = await sync_open_positions(db, force=True, downloader=_fixture_downloader)
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

    summary = await sync_open_positions(db, downloader=_fixture_downloader)
    assert summary.skipped == 0
    assert summary.synced == 1
