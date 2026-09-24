"""Contract history ingest — parsing, upsert, and sync orchestration.

History comes from StockNear's contract JSON API
(stocknear_contract_api.fetch_contract_history). It replaced a Playwright
flow that clicked through the contract-lookup page's Download menu for a
CSV; the JSON carries the same columns plus bid/ask on days the CSV left
blank. See docs/superpowers/specs/2026-08-19-contract-history-download-design.md
for the original design.
"""

import asyncio
import json
import logging
import re
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
    a keyword; the JSON keys are the bare names.
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


# JSON key -> HistoryRow field. Anything not listed is ignored, so a new
# key appearing upstream does not break the parser. `dte` is not sent; it
# is derived from the payload's expiration.
_COLUMN_MAP = {
    "open": "open_",
    "high": "high",
    "low": "low",
    "close": "close",
    "close_bid": "bid",
    "close_ask": "ask",
    "mark": "mark",
    "volume": "volume",
    "open_interest": "open_interest",
    "changeOI": "change_oi",
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
_INT_FIELDS = {"volume", "open_interest", "change_oi"}

_OCC_PARTS = re.compile(r"^([A-Z]{1,6})(\d{6})([CP])(\d{8})$")


class ContractHistoryParseError(ContractHistoryError):
    """The payload was not a parseable contract history."""


def _blank(raw) -> bool:
    return raw is None or (isinstance(raw, str) and raw.strip() == "")


def _dec(raw) -> Optional[Decimal]:
    if _blank(raw):
        return None
    try:
        # str() first: Decimal(0.3) is 0.2999999999999999888..., the
        # value the JSON actually carried is "0.3".
        return Decimal(str(raw).strip())
    except InvalidOperation:
        logger.warning("Could not parse decimal value %r", raw)
        return None


def _int(raw) -> Optional[int]:
    if _blank(raw):
        return None
    try:
        # Some counts arrive as 4.0.
        parsed = float(raw)
        truncated = int(parsed)
        if parsed != truncated:
            logger.warning("Truncating non-integral count value %r to %d", raw, truncated)
        return truncated
    except (TypeError, ValueError):
        logger.warning("Could not parse integer value %r", raw)
        return None


def _flt(raw) -> Optional[float]:
    if _blank(raw):
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        logger.warning("Could not parse float value %r", raw)
        return None


def _occ_parts(occ: str) -> tuple[date, Decimal, str]:
    match = _OCC_PARTS.match(occ)
    if not match:
        raise ContractHistoryError(f"Not an OCC symbol: {occ!r}")
    _, yymmdd, type_char, strike = match.groups()
    return (
        datetime.strptime(yymmdd, "%y%m%d").date(),
        Decimal(strike) / 1000,
        "put" if type_char == "P" else "call",
    )


def parse_history_json(payload, occ: str) -> list[HistoryRow]:
    """Parse a contract-history API payload into HistoryRow objects.

    Refuses a payload for a different contract than `occ`: the API echoes
    the expiration, strike and type it served, and storing another
    contract's history under this one is the corruption the old page-URL
    guard existed to prevent. Null values become None, never 0 — the source
    omits second-order greeks on older rows, and a zero greek is a
    meaningful value. Rows are returned oldest first, one per date (the
    last row wins if the source repeats a date).
    """
    if isinstance(payload, list) and not payload:
        raise ContractHistoryError(f"StockNear has no history for {occ} (unknown contract)")
    if not isinstance(payload, dict) or not isinstance(payload.get("history"), list):
        raise ContractHistoryParseError(f"{occ}: payload has no history list")

    expiration, strike, option_type = _occ_parts(occ)
    try:
        served = (
            date.fromisoformat(str(payload.get("expiration"))),
            Decimal(str(payload.get("strike"))),
            str(payload.get("optionType")).lower(),
        )
    except (ValueError, InvalidOperation):
        raise ContractHistoryParseError(
            f"{occ}: payload does not identify its contract "
            f"(expiration={payload.get('expiration')!r}, strike={payload.get('strike')!r})"
        )
    if served != (expiration, strike, option_type):
        raise ContractHistoryError(
            f"StockNear served {served[0]} {served[1]} {served[2]} for {occ}; refusing to store it"
        )

    by_date: dict[date, HistoryRow] = {}
    for index, raw_row in enumerate(payload["history"]):
        raw_date = raw_row.get("date") if isinstance(raw_row, dict) else None
        try:
            parsed_date = date.fromisoformat(str(raw_date).strip()[:10])
        except ValueError:
            logger.warning("Skipping row %d for %s: unparseable date %r", index, occ, raw_date)
            continue

        values: dict = {"date": parsed_date, "dte": (expiration - parsed_date).days}
        for key, field_name in _COLUMN_MAP.items():
            raw = raw_row.get(key)
            if field_name in _DECIMAL_FIELDS:
                values[field_name] = _dec(raw)
            elif field_name in _INT_FIELDS:
                values[field_name] = _int(raw)
            else:
                values[field_name] = _flt(raw)

        if parsed_date in by_date:
            logger.warning("Duplicate date %s for %s; keeping the later row", parsed_date, occ)
        by_date[parsed_date] = HistoryRow(**values)

    if not by_date:
        raise ContractHistoryParseError(f"{occ}: payload contained no parseable rows")

    return [by_date[d] for d in sorted(by_date)]


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
    """Build the OCC symbol StockNear's contract API expects.

    Must agree with stocknear_contract_api.build_contract_id (tested).

    Validates the result against the OCC shape before returning it. This
    value flows into the API request and into a filesystem path when a
    failed payload is retained for diagnosis; a symbol containing `/` or
    `..` could write outside the intended directory. occ_symbol() is the
    sole real producer of this string, so validating here closes that off
    for every caller.
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


def _retain_failed_payload(payload, occ: str) -> Optional[Path]:
    """Save a payload that failed to parse somewhere it can be inspected.

    Best-effort: a failure to preserve the evidence must not mask the
    original parse error, so this never raises.
    """
    try:
        keep_dir = Path(tempfile.gettempdir()) / "contract-history-failures"
        keep_dir.mkdir(parents=True, exist_ok=True)
        dest = keep_dir / f"{occ}.json"
        dest.write_text(json.dumps(payload, default=str))
        return dest
    except Exception:
        logger.warning("Could not retain failed payload for %s", occ, exc_info=True)
        return None


async def _fetch_all(jobs: list[tuple[str, str]], fetcher) -> dict:
    """Fetch every job concurrently. Returns {occ_symbol: payload | Exception}.

    Failures are recorded per contract; one bad contract never aborts the
    batch. Concurrency is bounded inside the API client.
    """
    async def one(symbol: str, occ: str):
        try:
            return await fetcher(symbol, occ)
        except Exception as e:  # recorded per contract; batch continues
            logger.warning("History fetch failed for %s: %s", occ, e)
            return e

    results = await asyncio.gather(*(one(symbol, occ) for symbol, occ in jobs))
    return {occ: result for (_, occ), result in zip(jobs, results)}


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


@dataclass(frozen=True)
class ContractRef:
    """A contract named by its parts rather than by database identity.

    The speculation page builds strategies from contracts the user may not
    hold, so they have no option_contracts row to reference yet.
    """

    symbol: str
    expiration: date
    strike: Decimal
    option_type: str  # "CALL" or "PUT"


async def _get_or_create_contract(db: AsyncSession, ref: ContractRef) -> OptionContract:
    """Resolve a ContractRef to an OptionContract row, creating it if absent.

    Creating a row here widens what option_contracts means: from "contracts
    you have traded" to "contracts you have traded or researched". That is
    deliberate and safe — update_position() returns early for a contract
    with no trades, and get_positions() joins OptionPosition, so a
    researched contract never surfaces as a phantom position.
    """
    stmt = select(OptionContract).where(
        OptionContract.symbol == ref.symbol.upper(),
        OptionContract.expiration == ref.expiration,
        OptionContract.strike == ref.strike,
        OptionContract.option_type == ref.option_type.upper(),
    )
    existing = (await db.execute(stmt)).scalars().first()
    if existing is not None:
        return existing

    created = OptionContract(
        symbol=ref.symbol.upper(),
        expiration=ref.expiration,
        strike=ref.strike,
        option_type=ref.option_type.upper(),
    )
    db.add(created)
    await db.flush()
    # Log the parts, not occ_symbol(created) — that validates and raises for
    # a symbol shape it cannot build, which would abort the whole sync from
    # inside a log statement, outside the per-contract error isolation below.
    logger.info(
        "Created contract row for researched contract %s %s %s %s",
        created.symbol, created.expiration, created.strike, created.option_type,
    )
    return created


async def sync_contract_history(
    db: AsyncSession,
    force: bool = False,
    fetcher=None,
    extra: Optional[list[ContractRef]] = None,
) -> SyncSummary:
    """Fetch and store history for open positions, plus any named contracts.

    extra: contracts named by parts rather than by id — typically the legs
        currently loaded in the speculation strategy builder. Rows are
        created for any that do not exist yet. Contracts that are both held
        and named appear once, not twice.

    fetcher: optional async callable (symbol, occ_symbol) -> payload, used
        per contract instead of the StockNear API. Tests inject this;
        production leaves it None.
    """
    stmt = (
        select(OptionContract)
        .join(OptionPosition, OptionPosition.contract_id == OptionContract.id)
        .where(OptionPosition.is_closed == False)  # noqa: E712
    )
    contracts = list((await db.execute(stmt)).scalars().all())

    if extra:
        # Deduplicate by id: a leg the user also holds must be synced once.
        seen = {c.id for c in contracts}
        for ref in extra:
            resolved = await _get_or_create_contract(db, ref)
            if resolved.id not in seen:
                seen.add(resolved.id)
                contracts.append(resolved)
        await db.commit()

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
        # left to fetch.
        return summary

    jobs = [(symbol, occ) for _, symbol, occ in job_meta]

    if fetcher is None:
        from app.services.stocknear_contract_api import fetch_contract_history
        fetcher = fetch_contract_history
    downloaded = await _fetch_all(jobs, fetcher)

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
                else "no payload returned"
            )
            status.last_error = message
            await db.commit()
            summary.failed += 1
            summary.errors.append(SyncError(contract=occ, error=message))
            continue

        try:
            rows = parse_history_json(result, occ)
            written = await upsert_history(db, contract_id, rows)
            # Surface constraint violations HERE, attributed to this
            # contract, rather than letting them wait for the next
            # contract's autoflush.
            await db.flush()
        except Exception as e:
            # Save the payload first — a malformed response is exactly
            # what you need in hand to diagnose a parser failure, and it
            # is unreproducible once discarded.
            # An empty list is StockNear's "unknown contract" answer; there
            # is nothing in it to diagnose.
            kept = _retain_failed_payload(result, occ) if result != [] else None
            if result == []:
                logger.warning("No history for %s: StockNear does not know the contract", occ)
            else:
                logger.exception("Parse/upsert failed for %s (kept at %s)", occ, kept)
            # The session is unusable until rolled back, and the
            # rollback discards `status` if it was newly added this
            # transaction (never persisted) or expires it otherwise --
            # so it must be re-fetched (or re-created) rather than
            # reused.
            await db.rollback()
            status = await _get_or_create_status(db, contract_id)
            status.last_attempt_at = now
            status.last_error = f"{type(e).__name__}: {e}" + (
                f" (payload kept at {kept})" if kept else ""
            )
            await db.commit()
            summary.failed += 1
            # status.last_error, not str(e): it names where the payload
            # was kept.
            summary.errors.append(SyncError(contract=occ, error=status.last_error))
            continue

        status.last_success_at = now
        status.last_error = None
        status.row_count = written
        await db.commit()
        summary.synced += 1
        summary.rows_upserted += written

    return summary
