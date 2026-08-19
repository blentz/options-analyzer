"""Contract history ingest — parsing, upsert, and sync orchestration.

Deliberately free of Playwright imports so the parsing and upsert logic
can be tested without a browser. Browser work lives on StockNearScraper;
see docs/superpowers/specs/2026-08-19-contract-history-download-design.md.
"""

import csv
import logging
from dataclasses import dataclass, fields
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ContractHistory

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


def _dec(raw: str) -> Optional[Decimal]:
    if raw is None or raw.strip() == "":
        return None
    try:
        return Decimal(raw.strip())
    except InvalidOperation:
        return None


def _int(raw: str) -> Optional[int]:
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
        return None


def _flt(raw: str) -> Optional[float]:
    if raw is None or raw.strip() == "":
        return None
    try:
        return float(raw.strip())
    except ValueError:
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
    data on every sync. Returns the number of rows written. Does not commit.
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
