"""Contract history ingest — parsing, upsert, and sync orchestration.

Deliberately free of Playwright imports so the parsing and upsert logic
can be tested without a browser. Browser work lives on StockNearScraper;
see docs/superpowers/specs/2026-08-19-contract-history-download-design.md.
"""

import asyncio
import csv
import logging
import re
import shutil
import tempfile
from dataclasses import dataclass, field, fields
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import ContractHistory, ContractHistorySync, OptionContract, OptionPosition
from app.stocknear_models import ContractHistoryError

logger = logging.getLogger(__name__)


@dataclass
class HistoryRow:
    """One trading day of history for one contract.

    Field names match the ContractHistory columns. `open_` and `lambda_`
    carry trailing underscores because `open` is a builtin and `lambda` is
    a keyword; the CSV headers are the bare names.
    """

    date: date

    # Prices — exact, so Decimal.
    open_: Optional[Decimal] = None
    high: Optional[Decimal] = None
    low: Optional[Decimal] = None
    close: Optional[Decimal] = None
    bid: Optional[Decimal] = None
    ask: Optional[Decimal] = None
    mark: Optional[Decimal] = None

    # Counts.
    volume: Optional[int] = None
    open_interest: Optional[int] = None
    change_oi: Optional[int] = None
    dte: Optional[int] = None

    # Volatility and rates of change — float is fine, these are not money.
    implied_volatility: Optional[float] = None
    changes_percentage_oi: Optional[float] = None

    # Greeks.
    delta: Optional[float] = None
    gamma: Optional[float] = None
    theta: Optional[float] = None
    vega: Optional[float] = None
    rho: Optional[float] = None
    epsilon: Optional[float] = None
    lambda_: Optional[float] = None
    charm: Optional[float] = None
    vanna: Optional[float] = None
    vomma: Optional[float] = None
    veta: Optional[float] = None
    vera: Optional[float] = None
    speed: Optional[float] = None
    zomma: Optional[float] = None
    color: Optional[float] = None
    ultima: Optional[float] = None

    # Aggregates — exact.
    gex: Optional[Decimal] = None
    dex: Optional[Decimal] = None
    total_premium: Optional[Decimal] = None


# CSV header -> HistoryRow field. Anything not listed is ignored, so a new
# column appearing upstream does not break the parser.
_COLUMN_MAP = {
    "open": "open_",
    "high": "high",
    "low": "low",
    "close": "close",
    "bid": "bid",
    "ask": "ask",
    "mark": "mark",
    "volume": "volume",
    "open_interest": "open_interest",
    "changeOI": "change_oi",
    "dte": "dte",
    "implied_volatility": "implied_volatility",
    "changesPercentageOI": "changes_percentage_oi",
    "delta": "delta",
    "gamma": "gamma",
    "theta": "theta",
    "vega": "vega",
    "rho": "rho",
    "epsilon": "epsilon",
    "lambda": "lambda_",
    "charm": "charm",
    "vanna": "vanna",
    "vomma": "vomma",
    "veta": "veta",
    "vera": "vera",
    "speed": "speed",
    "zomma": "zomma",
    "color": "color",
    "ultima": "ultima",
    "gex": "gex",
    "dex": "dex",
    "total_premium": "total_premium",
}

_DECIMAL_FIELDS = {
    "open_", "high", "low", "close", "bid", "ask", "mark",
    "gex", "dex", "total_premium",
}
_INT_FIELDS = {"volume", "open_interest", "change_oi", "dte"}


class ContractHistoryParseError(Exception):
    """The downloaded file was not a parseable contract-history CSV."""


def _dec(raw: Optional[str]) -> Optional[Decimal]:
    if raw is None or raw.strip() == "":
        return None
    try:
        return Decimal(raw.strip())
    except InvalidOperation:
        logger.warning("Could not parse decimal value %r", raw)
        return None


def _int(raw: Optional[str]) -> Optional[int]:
    if raw is None or raw.strip() == "":
        return None
    try:
        # Some counts arrive as "4.0"; int("4.0") raises.
        cleaned = raw.strip()
        parsed = float(cleaned)
        truncated = int(parsed)
        if parsed != truncated:
            logger.warning(
                "Truncating non-integral count value %r to %d",
                cleaned, truncated
            )
        return truncated
    except ValueError:
        logger.warning("Could not parse integer value %r", raw)
        return None


def _flt(raw: Optional[str]) -> Optional[float]:
    if raw is None or raw.strip() == "":
        return None
    try:
        return float(raw.strip())
    except ValueError:
        logger.warning("Could not parse float value %r", raw)
        return None


def parse_history_csv(path: Path) -> list[HistoryRow]:
    """Parse a Stocknear contract-history CSV into HistoryRow objects.

    Empty cells become None, never 0 — the source omits second-order greeks
    on older rows, and a zero greek is a meaningful value.
    """
    rows: list[HistoryRow] = []

    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None or "date" not in reader.fieldnames:
            raise ContractHistoryParseError(
                f"{path} has no 'date' column; headers={reader.fieldnames}"
            )

        for lineno, raw_row in enumerate(reader, start=2):
            raw_date = (raw_row.get("date") or "").strip()
            if not raw_date:
                logger.warning("Skipping row %d in %s: empty date", lineno, path)
                continue
            try:
                parsed_date = datetime.strptime(raw_date, "%Y-%m-%d").date()
            except ValueError:
                logger.warning(
                    "Skipping row %d in %s: unparseable date %r", lineno, path, raw_date
                )
                continue

            values: dict = {"date": parsed_date}
            for header, field_name in _COLUMN_MAP.items():
                raw = raw_row.get(header)
                if field_name in _DECIMAL_FIELDS:
                    values[field_name] = _dec(raw)
                elif field_name in _INT_FIELDS:
                    values[field_name] = _int(raw)
                else:
                    values[field_name] = _flt(raw)

            rows.append(HistoryRow(**values))

    if not rows:
        raise ContractHistoryParseError(f"{path} contained no parseable rows")

    return rows


# Field name on HistoryRow -> attribute on ContractHistory. They are
# identical by construction; this is derived rather than repeated so the
# two cannot drift.
_ROW_FIELDS = [f.name for f in fields(HistoryRow) if f.name != "date"]


async def upsert_history(
    db: AsyncSession, contract_id: int, rows: list[HistoryRow]
) -> int:
    """Insert or update history rows for one contract, keyed on (contract, date).

    Each download is a complete history, so this runs against overlapping
    data on every sync. Does not commit; caller controls the transaction.

    Rows present in the DB but absent from `rows` are left unchanged — a
    shorter download is treated as a partial fetch, never as evidence that
    the source has deleted data. That "never treated as deletion" guarantee
    is at date granularity only, not column granularity: for a date that
    IS present in `rows`, every column on that row is overwritten from the
    incoming data, including columns that come back empty. A cell that
    goes empty upstream (e.g. bid/ask on a day with no quotes) will null
    out a previously-stored non-empty value for that same cell. This is
    deliberate — the row is treated as the current source of truth for
    that date — but it means the guarantee above applies to whole missing
    dates, not to individual cells within a date that IS present.

    Returns the number of rows written (inserted + updated), counting all
    rows processed regardless of whether their values changed. On a normal
    re-sync with unchanged data, this returns the batch size, not zero.

    Assumes the caller supplies at most one row per date. Duplicate dates
    within a single batch will cause IntegrityError on flush due to the
    (contract_id, date) unique index.
    """
    if not rows:
        return 0

    existing_stmt = select(ContractHistory).where(
        ContractHistory.contract_id == contract_id,
        ContractHistory.date.in_([r.date for r in rows]),
    )
    existing = {
        h.date: h for h in (await db.execute(existing_stmt)).scalars().all()
    }

    for row in rows:
        target = existing.get(row.date)
        if target is None:
            target = ContractHistory(contract_id=contract_id, date=row.date)
            db.add(target)
        for name in _ROW_FIELDS:
            setattr(target, name, getattr(row, name))

    return len(rows)


@dataclass
class SyncError:
    contract: str          # OCC symbol
    error: str


@dataclass
class SyncSummary:
    contracts_total: int = 0
    synced: int = 0
    skipped: int = 0
    failed: int = 0
    rows_upserted: int = 0
    errors: list[SyncError] = field(default_factory=list)


_OCC_SYMBOL_PATTERN = re.compile(r"^[A-Z]{1,6}\d{6}[CP]\d{8}$")


def occ_symbol(contract: OptionContract) -> str:
    """Build the OCC symbol Stocknear's contract-lookup URL expects.

    Mirrors StockNearScraper._build_contract_id, reimplemented here rather
    than imported because that module pulls in Playwright.

    Validates the result against the OCC shape before returning it. This
    value flows unvalidated into a filesystem path in
    StockNearScraper.download_contract_history (`dest_dir /
    f"{occ_symbol}.csv"`) and into a URL query parameter; a symbol
    containing `/` or `..` could write outside the intended temp
    directory. occ_symbol() is the sole real producer of this string, so
    validating here closes that off for every caller.
    """
    type_char = "P" if contract.option_type.upper() == "PUT" else "C"
    strike_int = int(Decimal(str(contract.strike)) * 1000)
    symbol = (
        f"{contract.symbol.upper()}"
        f"{contract.expiration.strftime('%y%m%d')}"
        f"{type_char}{strike_int:08d}"
    )
    if not _OCC_SYMBOL_PATTERN.match(symbol):
        raise ContractHistoryError(
            f"Built OCC symbol {symbol!r} does not match the expected "
            f"OCC format; refusing to use it as a filename/URL component."
        )
    return symbol


def _retain_failed_download(path: Path, occ: str) -> Optional[Path]:
    """Copy a file that failed to parse somewhere it will survive cleanup.

    Best-effort: a failure to preserve the evidence must not mask the
    original parse error, so this never raises.
    """
    try:
        keep_dir = Path(tempfile.gettempdir()) / "contract-history-failures"
        keep_dir.mkdir(parents=True, exist_ok=True)
        dest = keep_dir / f"{occ}.csv"
        shutil.copy(path, dest)
        return dest
    except Exception:
        logger.warning("Could not retain failed download for %s", occ, exc_info=True)
        return None


def _download_batch(jobs: list[tuple[str, str]], dest_dir: Path) -> dict:
    """Download every job in ONE browser session. Runs on a worker thread.

    jobs: list of (underlying_symbol, occ_symbol).
    Returns {occ_symbol: Path | Exception}.

    No database access happens here: sync_playwright() cannot run on a
    thread with a running event loop, and the AsyncSession belongs to the
    loop's thread.

    Deliberately does not use `with StockNearScraper() as scraper:`.
    `start()` does a real page.goto auth probe, and two things go wrong
    if it raises inside a `with`: `__exit__` never runs (entry never
    completed), leaking a Playwright driver process per call; and the
    exception propagates straight out of this function before `results`
    is returned, discarding every contract's failure with it and letting
    the whole batch surface as a bare 500 with nothing recorded anywhere.
    """
    from app.stocknear import StockNearScraper  # deferred: keeps Playwright
                                                # out of this module's import
    results: dict = {}
    scraper = StockNearScraper()
    try:
        scraper.start()
        for symbol, occ in jobs:
            try:
                results[occ] = scraper.download_contract_history(symbol, occ, dest_dir)
            except Exception as e:  # recorded per contract; batch continues
                logger.warning("Download failed for %s: %s", occ, e)
                results[occ] = e
    except Exception as e:
        # start() failed (missing Firefox, bad profile path, slow site,
        # ...) before or during the loop above. Record the real cause
        # against every due contract that doesn't already have a result,
        # so a batch-level failure still leaves something for the user to
        # see and act on, per contract, exactly like a per-contract one.
        logger.warning("Batch-level scraper failure: %s", e)
        for _, occ in jobs:
            results.setdefault(occ, e)
    finally:
        # Always attempt teardown, even when start() itself raised.
        # `results` is fully built by this point, so a raising close()
        # cannot discard it -- unlike returning from inside a `with`
        # block, where the return happens only after __exit__ completes.
        try:
            scraper.close()
        except Exception:
            logger.warning("Scraper close() failed after batch", exc_info=True)

    return results


async def _get_or_create_status(db: AsyncSession, contract_id: int) -> ContractHistorySync:
    """Fetch (or create) a contract's sync-status row.

    Used to re-establish the status object after a rollback: a row added
    via db.add() earlier in the same transaction does not exist once that
    transaction is rolled back, and an existing row's in-memory state is
    stale, so the caller must not keep using the object it already had.
    """
    status = (
        await db.execute(
            select(ContractHistorySync).where(
                ContractHistorySync.contract_id == contract_id
            )
        )
    ).scalar_one_or_none()
    if status is None:
        status = ContractHistorySync(contract_id=contract_id)
        db.add(status)
    return status


async def sync_open_positions(
    db: AsyncSession, force: bool = False, downloader=None
) -> SyncSummary:
    """Download and store history for every contract with an open position.

    downloader: optional callable (symbol, occ_symbol, dest_dir) -> Path,
        used per contract instead of a real browser session. Tests inject
        this; production leaves it None.
    """
    stmt = (
        select(OptionContract)
        .join(OptionPosition, OptionPosition.contract_id == OptionContract.id)
        .where(OptionPosition.is_closed == False)  # noqa: E712
    )
    contracts = (await db.execute(stmt)).scalars().all()

    summary = SyncSummary(contracts_total=len(contracts))
    if not contracts:
        return summary

    status_stmt = select(ContractHistorySync).where(
        ContractHistorySync.contract_id.in_([c.id for c in contracts])
    )
    statuses = {
        s.contract_id: s for s in (await db.execute(status_stmt)).scalars().all()
    }

    ttl = timedelta(seconds=settings.stocknear_history_ttl_seconds)
    now = datetime.utcnow()

    due: list[OptionContract] = []
    for c in contracts:
        status = statuses.get(c.id)
        fresh = (
            status is not None
            and status.last_success_at is not None
            and now - status.last_success_at < ttl
        )
        if fresh and not force:
            summary.skipped += 1
        else:
            due.append(c)

    if not due:
        return summary

    with tempfile.TemporaryDirectory(prefix="contract-history-") as tmpdir:
        dest_dir = Path(tmpdir)

        # Resolve each due contract's identity into plain values now,
        # before any commit/rollback happens. A mid-loop rollback expires
        # every object still tracked by the session -- including primary
        # keys -- and touching an expired attribute outside of an active
        # await raises MissingGreenlet under the async driver. Plain
        # (id, symbol, occ) tuples sidestep that entirely: nothing below
        # ever reads an attribute off a `due` contract again.
        #
        # occ_symbol(c) can raise (e.g. a dotted ticker like "BRK.B" does
        # not fit the OCC shape) -- and does so BEFORE any download is
        # attempted, so this failure must be isolated per contract just
        # like a download or parse failure is. A bare list comprehension
        # here would let one bad symbol raise out of the comprehension and
        # abort the whole sync call, leaving every other due contract with
        # nothing recorded -- the same failure shape N1 fixed for a
        # batch-level browser failure, reintroduced through a different
        # door. commit()ing this contract's status immediately (rather
        # than batching it with the loop below) keeps it consistent with
        # every other per-contract failure path in this function.
        job_meta: list[tuple[int, str, str]] = []
        for c in due:
            try:
                occ = occ_symbol(c)
            except Exception as e:
                contract_label = c.contract_id  # e.g. "BRK.B 10/16/26 $250.00 PUT"
                status = await _get_or_create_status(db, c.id)
                status.last_attempt_at = now
                status.last_error = (
                    f"Could not build a valid OCC symbol for {contract_label}: "
                    f"{type(e).__name__}: {e}"
                )
                await db.commit()
                summary.failed += 1
                summary.errors.append(SyncError(contract=contract_label, error=status.last_error))
                continue
            job_meta.append((c.id, c.symbol, occ))

        if not job_meta:
            # Every due contract failed symbol construction -- nothing
            # left to download. Skip the (otherwise wasted) browser launch.
            return summary

        jobs = [(symbol, occ) for _, symbol, occ in job_meta]

        if downloader is None:
            downloaded = await asyncio.to_thread(_download_batch, jobs, dest_dir)
        else:
            downloaded = {}
            for symbol, occ in jobs:
                try:
                    downloaded[occ] = downloader(symbol, occ, dest_dir)
                except Exception as e:
                    downloaded[occ] = e

        # Each contract commits (or rolls back) its own transaction. A
        # batch-wide atomic commit is incompatible with per-contract
        # isolation: upsert_history only db.add()s and never flushes, so
        # an IntegrityError from the (contract_id, date) unique index
        # would otherwise surface later via autoflush -- misattributed to
        # whichever contract queries the session next -- and poison the
        # session so the final commit raises, discarding every contract's
        # successful writes along with it. Flushing inside each contract's
        # own try/except, and rolling back before touching the session
        # again, keeps one bad download from taking down the batch.
        #
        # The pre-fetched `statuses` dict is not reused here: any status
        # object cached there may have been expired (or, if newly added
        # and never persisted, discarded outright) by an earlier
        # iteration's rollback. `_get_or_create_status` re-queries fresh
        # every time, which is the only way to avoid working with a stale
        # or invalid object after a rollback anywhere earlier in the loop.
        for contract_id, symbol, occ in job_meta:
            status = await _get_or_create_status(db, contract_id)
            status.last_attempt_at = now

            result = downloaded.get(occ)
            if isinstance(result, Exception) or result is None:
                # Same shape as the parse-failure branch below
                # (type name + message) -- str(e) alone renders as an
                # empty string for an exception constructed with no args.
                message = (
                    f"{type(result).__name__}: {result}"
                    if result is not None
                    else "no download produced"
                )
                status.last_error = message
                await db.commit()
                summary.failed += 1
                summary.errors.append(SyncError(contract=occ, error=message))
                continue

            try:
                rows = parse_history_csv(result)
                written = await upsert_history(db, contract_id, rows)
                # Surface constraint violations HERE, attributed to this
                # contract, rather than letting them wait for the next
                # contract's autoflush.
                await db.flush()
            except Exception as e:
                # The enclosing TemporaryDirectory is about to delete the
                # file, so copy it somewhere durable first — a malformed
                # download is exactly what you need in hand to diagnose a
                # parser failure, and it is unreproducible once discarded.
                kept = _retain_failed_download(result, occ)
                logger.exception("Parse/upsert failed for %s (kept at %s)", occ, kept)
                # The session is unusable until rolled back, and the
                # rollback discards `status` if it was newly added this
                # transaction (never persisted) or expires it otherwise --
                # so it must be re-fetched (or re-created) rather than
                # reused.
                await db.rollback()
                status = await _get_or_create_status(db, contract_id)
                status.last_attempt_at = now
                status.last_error = f"{type(e).__name__}: {e} (file kept at {kept})"
                await db.commit()
                summary.failed += 1
                # Use status.last_error, not str(e): str(e) for a parse
                # failure names the temp-directory path, which is about to
                # be deleted when the enclosing TemporaryDirectory exits.
                # status.last_error carries the durable
                # /tmp/contract-history-failures/... path instead, so the
                # errors[] the endpoint returns points somewhere that
                # still exists.
                summary.errors.append(SyncError(contract=occ, error=status.last_error))
                continue

            status.last_success_at = now
            status.last_error = None
            status.row_count = written
            await db.commit()
            summary.synced += 1
            summary.rows_upserted += written

    return summary
