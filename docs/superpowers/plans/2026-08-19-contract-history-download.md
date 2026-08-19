# Contract History Download Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Download per-contract options history CSVs from Stocknear for every open position and store them full-fidelity, so option pricing can be analyzed over time.

**Architecture:** Browser work stays in `app/stocknear.py` (one new method on the existing `StockNearScraper`). Parsing, upsert, and orchestration live in a new `app/services/contract_history.py` that does not import Playwright. A user-triggered endpoint syncs all open positions in a single browser session and returns a summary.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.0 + aiosqlite, Alembic, Playwright 1.49.1 (Firefox), pytest + pytest-asyncio, Jinja2.

**Spec:** `docs/superpowers/specs/2026-08-19-contract-history-download-design.md`

## Global Constraints

- `app/stocknear_models.py`, `app/stocknear_cookies.py`, and `app/services/contract_history.py` MUST NOT import Playwright. Tests for them must run with no browser installed.
- `app/stocknear.py` MUST NOT import from `app/services/` — the existing direction is `services → stocknear` (`app/services/stocknear_service.py:125`), and reversing it creates a cycle.
- Python keywords and builtins get a trailing underscore in Python, mapped to the bare name in SQL: `open_` → column `open`, `lambda_` → column `lambda`.
- Absent CSV values parse to `None`, never `0`. A zero delta is a real value for a deep OTM contract.
- `implied_volatility` in this CSV is **already a decimal** (`0.5812`, displayed on the page as `58.12%`). Do NOT divide by 100. The MCP path in `app/services/stocknear_mcp.py` does divide, because that source reports percentages — these two sources differ and the difference is easy to get wrong.
- Test DB pattern is in-memory aiosqlite, per `tests/test_wheel_detection.py:25-36`. Copy that fixture; do not invent another.
- No repair of source data. Rows store exactly as received.

---

### Task 1: CSV parser

Pure parsing, no DB and no browser. This is where most of the risk lives, so it goes first and gets the most tests.

**Files:**
- Create: `app/services/contract_history.py`
- Create: `tests/fixtures/contract_history/HITI261016P00002500.csv`
- Test: `tests/test_contract_history_parse.py`

**Interfaces:**
- Consumes: nothing
- Produces: `HistoryRow` dataclass (34 fields, listed below); `parse_history_csv(path: Path) -> list[HistoryRow]`

- [ ] **Step 1: Copy the fixture CSV into the repo**

A real download already exists at `~/Downloads/HITI261016P00002500_contract_history.csv` (123 data rows, Feb 19 2026 → Aug 19 2026). If it is missing, any contract-history CSV downloaded from Stocknear works, but the row-count assertions below must be updated to match.

```bash
mkdir -p tests/fixtures/contract_history
cp ~/Downloads/HITI261016P00002500_contract_history.csv \
   tests/fixtures/contract_history/HITI261016P00002500.csv
head -1 tests/fixtures/contract_history/HITI261016P00002500.csv
```

Expected header:

```
date,open,high,low,close,volume,open_interest,bid,ask,mark,implied_volatility,delta,gamma,theta,vega,rho,epsilon,lambda,charm,vanna,vomma,veta,vera,speed,zomma,color,ultima,changeOI,changesPercentageOI,gex,dex,total_premium,dte
```

- [ ] **Step 2: Write the failing tests**

Create `tests/test_contract_history_parse.py`:

```python
"""Parser tests for Stocknear contract-history CSVs.

The None-vs-zero and IV-scale tests are the important ones. Both failure
modes produce plausible numbers that no downstream layer can detect.
"""

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app.services.contract_history import HistoryRow, parse_history_csv

FIXTURE = Path(__file__).parent / "fixtures" / "contract_history" / "HITI261016P00002500.csv"


class TestParseShape:
    def test_row_count(self):
        rows = parse_history_csv(FIXTURE)
        assert len(rows) == 123

    def test_returns_history_rows(self):
        rows = parse_history_csv(FIXTURE)
        assert all(isinstance(r, HistoryRow) for r in rows)

    def test_dates_parse(self):
        rows = parse_history_csv(FIXTURE)
        by_date = {r.date: r for r in rows}
        assert date(2026, 8, 19) in by_date
        assert date(2026, 2, 19) in by_date


class TestFullyPopulatedRow:
    """The most recent row has every column populated."""

    def _row(self):
        return {r.date: r for r in parse_history_csv(FIXTURE)}[date(2026, 8, 19)]

    def test_prices_are_decimal(self):
        r = self._row()
        assert r.close == Decimal("0.3")
        assert r.bid == Decimal("0.15")
        assert r.ask == Decimal("0.35")
        assert r.mark == Decimal("0.25")

    def test_counts_are_int(self):
        r = self._row()
        assert r.volume == 1
        assert r.open_interest == 3784
        assert r.dte == 58

    def test_greeks_are_float(self):
        r = self._row()
        assert r.delta == pytest.approx(-0.4856)
        assert r.gamma == pytest.approx(0.7051)
        assert r.ultima == pytest.approx(-0.0372)

    def test_iv_is_decimal_not_percent(self):
        # The page renders this as 58.12%. The CSV already gives 0.5812.
        # Dividing by 100 here would silently shrink every IV 100x.
        r = self._row()
        assert r.implied_volatility == pytest.approx(0.5812)


class TestSparseRows:
    """Older rows omit the second-order greeks entirely."""

    def _feb20(self):
        return {r.date: r for r in parse_history_csv(FIXTURE)}[date(2026, 2, 20)]

    def test_absent_greeks_are_none_not_zero(self):
        r = self._feb20()
        assert r.charm is None
        assert r.vanna is None
        assert r.ultima is None

    def test_present_greeks_still_parse(self):
        r = self._feb20()
        assert r.delta == pytest.approx(-0.3666)
        assert r.vega == pytest.approx(0.0076)

    def test_absent_quotes_are_none(self):
        r = self._feb20()
        assert r.bid is None
        assert r.ask is None


class TestSourceAnomalyPinned:
    """Feb 19 carries 4 in volume with open_interest empty; Feb 20 carries
    4 in open_interest with volume empty. This is either sparse data or a
    column shift at the source — we do not know which, so we store what we
    were given. This test exists so that changing that is deliberate.
    """

    def test_stored_as_received(self):
        by_date = {r.date: r for r in parse_history_csv(FIXTURE)}
        feb19 = by_date[date(2026, 2, 19)]
        feb20 = by_date[date(2026, 2, 20)]

        assert feb19.volume == 4
        assert feb19.open_interest is None

        assert feb20.volume is None
        assert feb20.open_interest == 4
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `pytest tests/test_contract_history_parse.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.contract_history'`

- [ ] **Step 4: Implement the parser**

Create `app/services/contract_history.py`:

```python
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
        return int(float(raw.strip()))
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
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `pytest tests/test_contract_history_parse.py -v`
Expected: PASS, 12 tests

- [ ] **Step 6: Verify no Playwright dependency leaked in**

Run: `python -c "import sys; import app.services.contract_history; assert 'playwright' not in sys.modules; print('clean')"`
Expected: `clean`

- [ ] **Step 7: Commit**

```bash
git add app/services/contract_history.py tests/test_contract_history_parse.py \
        tests/fixtures/contract_history/HITI261016P00002500.csv
git commit -m "feat: parse Stocknear contract-history CSVs"
```

---

### Task 2: Database models and migration

**Files:**
- Modify: `app/models.py` (append after `StockNearCache`)
- Create: `migrations/versions/<generated>_add_contract_history.py`
- Test: `tests/test_contract_history_models.py`

**Interfaces:**
- Consumes: `HistoryRow` field names from Task 1
- Produces: `ContractHistory` and `ContractHistorySync` ORM models

- [ ] **Step 1: Write the failing test**

Create `tests/test_contract_history_models.py`:

```python
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
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_contract_history_models.py -v`
Expected: FAIL — `ImportError: cannot import name 'ContractHistory' from 'app.models'`

- [ ] **Step 3: Add the models**

Append to `app/models.py`:

```python
class ContractHistory(Base):
    """One trading day of history for one option contract.

    Sourced from Stocknear's contract-lookup CSV export. Every column but
    the identity pair is nullable: the source omits second-order greeks on
    older rows and quotes on days with no market, and rejecting those rows
    would discard real data.
    """
    __tablename__ = "contract_history"

    id: Mapped[int] = mapped_column(primary_key=True)
    contract_id: Mapped[int] = mapped_column(
        ForeignKey("option_contracts.id"), index=True
    )
    date: Mapped[date] = mapped_column(Date, index=True)

    # Prices. `open` is a Python builtin, so the attribute carries a
    # trailing underscore while the column keeps the source's name.
    open_: Mapped[Optional[Decimal]] = mapped_column("open", Numeric(12, 4), nullable=True)
    high: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 4), nullable=True)
    low: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 4), nullable=True)
    close: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 4), nullable=True)
    bid: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 4), nullable=True)
    ask: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 4), nullable=True)
    mark: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 4), nullable=True)

    # Counts.
    volume: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    open_interest: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    change_oi: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    dte: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # Volatility. Stored as a decimal fraction (0.5812), matching the source
    # and every other implied_volatility in this schema.
    implied_volatility: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    changes_percentage_oi: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Greeks. Float rather than Numeric — these are not money, and exact
    # decimal semantics buy nothing for a vanna value.
    delta: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    gamma: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    theta: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    vega: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    rho: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    epsilon: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    lambda_: Mapped[Optional[float]] = mapped_column("lambda", Float, nullable=True)
    charm: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    vanna: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    vomma: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    veta: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    vera: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    speed: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    zomma: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    color: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    ultima: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Aggregates.
    gex: Mapped[Optional[Decimal]] = mapped_column(Numeric(16, 4), nullable=True)
    dex: Mapped[Optional[Decimal]] = mapped_column(Numeric(16, 4), nullable=True)
    total_premium: Mapped[Optional[Decimal]] = mapped_column(Numeric(16, 4), nullable=True)

    __table_args__ = (
        Index("ix_contract_history_unique", "contract_id", "date", unique=True),
    )


class ContractHistorySync(Base):
    """Ingest bookkeeping for contract history, one row per contract.

    Kept out of OptionContract so sync state does not accumulate on the
    domain model.
    """
    __tablename__ = "contract_history_sync"

    id: Mapped[int] = mapped_column(primary_key=True)
    contract_id: Mapped[int] = mapped_column(
        ForeignKey("option_contracts.id"), unique=True, index=True
    )
    last_attempt_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_success_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    row_count: Mapped[int] = mapped_column(Integer, default=0)
```

Check the imports at the top of `app/models.py`. Add whichever of `Float`, `Text`, `Integer`, `Numeric`, `Date`, `DateTime`, `Index`, `ForeignKey` are not already imported from `sqlalchemy`.

- [ ] **Step 4: Run the test to verify it passes**

Run: `pytest tests/test_contract_history_models.py -v`
Expected: PASS, 5 tests

- [ ] **Step 5: Generate the migration**

Alembic sets `down_revision` to the current head automatically. Do not hand-write it.

```bash
alembic revision --autogenerate -m "add contract history"
```

- [ ] **Step 6: Inspect the generated migration**

Open the new file in `migrations/versions/`. Confirm it creates `contract_history` and `contract_history_sync` with the unique index, and that it does **not** contain unrelated drops or alters of existing tables. Autogenerate sometimes emits spurious diffs; delete any operation that does not belong to these two tables.

- [ ] **Step 7: Verify the migration applies and reverses**

```bash
alembic upgrade head
alembic downgrade -1
alembic upgrade head
```

Expected: no errors on any of the three.

- [ ] **Step 8: Commit**

```bash
git add app/models.py migrations/versions/ tests/test_contract_history_models.py
git commit -m "feat: add contract_history and contract_history_sync tables"
```

---

### Task 3: Upsert

**Files:**
- Modify: `app/services/contract_history.py`
- Test: `tests/test_contract_history_upsert.py`

**Interfaces:**
- Consumes: `HistoryRow` (Task 1), `ContractHistory` (Task 2)
- Produces: `async def upsert_history(db, contract_id: int, rows: list[HistoryRow]) -> int` returning the number of rows written (inserted + updated)

- [ ] **Step 1: Write the failing test**

Create `tests/test_contract_history_upsert.py`:

```python
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
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_contract_history_upsert.py -v`
Expected: FAIL — `ImportError: cannot import name 'upsert_history'`

- [ ] **Step 3: Implement the upsert**

Add to `app/services/contract_history.py` (imports at top of file):

```python
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ContractHistory

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
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `pytest tests/test_contract_history_upsert.py -v`
Expected: PASS, 5 tests

- [ ] **Step 5: Commit**

```bash
git add app/services/contract_history.py tests/test_contract_history_upsert.py
git commit -m "feat: upsert contract history keyed on (contract, date)"
```

---

### Task 4: Substitution guard and error types

The single most important correctness check in this feature. Stocknear answers a request for a Pro-gated expiration with HTTP 200 and a *different contract's* valid CSV. Nothing downstream can detect that, so it must be caught at the source.

**Files:**
- Modify: `app/stocknear_models.py`
- Test: `tests/test_contract_history_guard.py`

**Interfaces:**
- Consumes: nothing
- Produces: exceptions `ContractHistoryError`, `ProGatedError`, `AuthExpiredError`, `DownloadTimeoutError`; and `verify_contract_served(requested_occ: str, page_url: str, page_text: str) -> None`

These live in `app/stocknear_models.py` because `app/stocknear.py` must import them and must not import from `app/services/` (see Global Constraints).

- [ ] **Step 1: Write the failing test**

Create `tests/test_contract_history_guard.py`:

```python
"""Guard tests for silent contract substitution.

Observed live: requesting HITI261016P00002500 while unauthenticated
returned HITI260821P00002500 — HTTP 200, well-formed CSV, different
contract. Without this guard an expired cookie writes another contract's
prices into your history and nothing downstream can tell.
"""

import pytest

from app.stocknear_models import (
    AuthExpiredError,
    ProGatedError,
    verify_contract_served,
)

WANTED = "HITI261016P00002500"
GOOD_URL = f"https://www.stocknear.com/stocks/HITI/options/contract-lookup?contract={WANTED}"
BANNER = (
    "The requested expiration date requires a Pro subscription. "
    "Showing the nearest available date."
)


class TestAccepts:
    def test_matching_contract_passes(self):
        verify_contract_served(WANTED, GOOD_URL, "Contract History  History  Download")

    def test_case_insensitive_url(self):
        verify_contract_served(WANTED, GOOD_URL.lower(), "Contract History")


class TestRejects:
    def test_substituted_contract_raises(self):
        substituted = GOOD_URL.replace(WANTED, "HITI260821P00002500")
        with pytest.raises(ProGatedError) as exc:
            verify_contract_served(WANTED, substituted, "Contract History")
        assert "HITI260821P00002500" in str(exc.value)

    def test_banner_raises_even_if_url_looks_right(self):
        """The URL is not always rewritten before the banner renders."""
        with pytest.raises(ProGatedError):
            verify_contract_served(WANTED, GOOD_URL, f"Option Contract Lookup {BANNER}")

    def test_login_redirect_raises(self):
        with pytest.raises(AuthExpiredError):
            verify_contract_served(WANTED, "https://www.stocknear.com/login", "Log in")

    def test_oauth_redirect_raises(self):
        with pytest.raises(AuthExpiredError):
            verify_contract_served(
                WANTED, "https://accounts.google.com/v3/signin/identifier?x=1", "Sign in"
            )

    def test_auth_checked_before_substitution(self):
        """A login redirect has no contract in the URL; report the real cause."""
        with pytest.raises(AuthExpiredError):
            verify_contract_served(WANTED, "https://www.stocknear.com/login", BANNER)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_contract_history_guard.py -v`
Expected: FAIL — `ImportError: cannot import name 'ProGatedError'`

- [ ] **Step 3: Implement the guard**

Append to `app/stocknear_models.py`:

```python
class ContractHistoryError(Exception):
    """Base for contract-history download failures."""


class ProGatedError(ContractHistoryError):
    """Stocknear served a different contract than the one requested.

    Requesting an expiration outside the current subscription tier does not
    error: the URL is rewritten to a nearer expiration and that contract's
    history is served with HTTP 200. Treating it as success would write the
    wrong contract's prices into the database.
    """


class AuthExpiredError(ContractHistoryError):
    """Session cookies are no longer valid; the page bounced to login."""


class DownloadTimeoutError(ContractHistoryError):
    """The download menu, CSV item, or download event never arrived."""


_PRO_BANNER = "requires a pro subscription"
_LOGIN_MARKERS = ("/login", "accounts.google.com")


def verify_contract_served(
    requested_occ: str, page_url: str, page_text: str
) -> None:
    """Raise unless the page is serving the contract we asked for.

    Checks auth first: a login redirect carries no contract in its URL, so
    testing for substitution first would misreport an expired cookie as a
    subscription problem.
    """
    url = (page_url or "").lower()
    text = (page_text or "").lower()
    wanted = requested_occ.lower()

    if any(marker in url for marker in _LOGIN_MARKERS):
        raise AuthExpiredError(
            f"Requesting {requested_occ} redirected to {page_url!r}; "
            "session cookies are expired or missing."
        )

    if _PRO_BANNER in text:
        raise ProGatedError(
            f"{requested_occ} requires a higher subscription tier; "
            "Stocknear substituted the nearest available expiration."
        )

    if wanted not in url:
        raise ProGatedError(
            f"Requested {requested_occ} but the page served {page_url!r}. "
            "Refusing to ingest a different contract's history."
        )
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `pytest tests/test_contract_history_guard.py -v`
Expected: PASS, 7 tests

- [ ] **Step 5: Commit**

```bash
git add app/stocknear_models.py tests/test_contract_history_guard.py
git commit -m "feat: guard against Stocknear serving a substituted contract"
```

---

### Task 5: Scraper download method

**Files:**
- Modify: `app/stocknear.py` (`start()` around line 74; new method after `get_contract_quotes_via_api`)

**Interfaces:**
- Consumes: `verify_contract_served`, `DownloadTimeoutError` (Task 4); `_build_contract_id` (existing, line 374)
- Produces: `download_contract_history(self, symbol: str, occ_symbol: str, dest_dir: Path) -> Path`

No unit test — this step is pure browser interaction, and its logic (the guard) is already tested in Task 4. Task 8 covers it with a live integration test.

- [ ] **Step 1: Enable downloads on the browser context**

In `app/stocknear.py`, `start()`, change:

```python
self.context = browser.new_context(viewport={"width": 1280, "height": 800})
```

to:

```python
# accept_downloads is required for expect_download() to fire; without it
# Playwright cancels the download and the event never arrives.
self.context = browser.new_context(
    viewport={"width": 1280, "height": 800},
    accept_downloads=True,
)
```

- [ ] **Step 2: Add the import**

In the import block of `app/stocknear.py`, extend the existing `from app.stocknear_models import (...)` to include the new names:

```python
from app.stocknear_models import (
    OptionContract,
    ContractQuote,
    OptionsChain,
    OptionsData,
    StockData,
    DownloadTimeoutError,
    verify_contract_served,
)
```

- [ ] **Step 3: Add the download method**

Add to `StockNearScraper`, after `get_contract_quotes_via_api`:

```python
    def download_contract_history(
        self, symbol: str, occ_symbol: str, dest_dir: "Path"
    ) -> "Path":
        """Download one contract's full history CSV.

        Args:
            symbol: Underlying ticker, e.g. "HITI". Taken from the contract
                row rather than parsed off occ_symbol — splitting an OCC
                symbol on its date component is ambiguous for tickers
                containing digits.
            occ_symbol: e.g. "HITI261016P00002500"
            dest_dir: Directory to save into. Must exist.

        Returns the saved file path.

        Raises ProGatedError, AuthExpiredError, or DownloadTimeoutError.
        """
        self._rate_limit()
        url = (
            f"{self.base_url}/stocks/{symbol.upper()}"
            f"/options/contract-lookup?contract={occ_symbol}"
        )
        logger.info("Downloading contract history for %s", occ_symbol)
        self.page.goto(url, wait_until="networkidle")

        # Must run before any data is read. A substituted contract returns
        # HTTP 200 with a valid CSV for the wrong contract.
        verify_contract_served(occ_symbol, self.page.url, self.page.inner_text("body"))

        try:
            # The Download control opens a menu; the CSV item is inside it.
            self.page.get_by_role("button", name="Download").click()
            with self.page.expect_download(timeout=30000) as download_info:
                self.page.get_by_role("menuitem", name="Download to CSV").click()
            download = download_info.value
        except Exception as e:
            raise DownloadTimeoutError(
                f"Download failed for {occ_symbol}: {e}"
            ) from e

        dest = dest_dir / f"{occ_symbol}.csv"
        download.save_as(str(dest))
        logger.info("Saved %s", dest)
        return dest
```

- [ ] **Step 4: Verify the module still imports and types check**

```bash
python -c "from app.stocknear import StockNearScraper; print('ok')"
mypy app/stocknear.py app/stocknear_models.py
```

Expected: `ok`, and no new mypy errors beyond any already present on these files.

- [ ] **Step 5: Verify the Playwright-free modules stay clean**

```bash
pytest tests/test_contract_history_parse.py tests/test_contract_history_guard.py -v
```

Expected: PASS. These must not have acquired a browser dependency.

- [ ] **Step 6: Commit**

```bash
git add app/stocknear.py
git commit -m "feat: download contract history CSV via Playwright"
```

---

### Task 6: Sync orchestrator

**Files:**
- Modify: `app/services/contract_history.py`
- Modify: `app/config.py`
- Test: `tests/test_contract_history_sync.py`

**Interfaces:**
- Consumes: `parse_history_csv`, `upsert_history`, `ContractHistorySync`, the error types
- Produces: `SyncSummary` dataclass; `async def sync_open_positions(db, force: bool = False, downloader=None) -> SyncSummary`

`downloader` is injectable so the orchestrator is testable without a browser. It is a callable `(symbol, occ_symbol, dest_dir) -> Path`. When `None`, a real scraper session is used.

- [ ] **Step 1: Add the TTL setting**

In `app/config.py`, after `stocknear_cache_ttl_seconds`:

```python
    # Contract-history sync. The source updates once per trading day; 12
    # hours fetches each day's data about once without needing a market
    # calendar. Sync is user-triggered, so this only suppresses redundant
    # re-downloads within a session of clicking.
    stocknear_history_ttl_seconds: int = 43200
```

- [ ] **Step 2: Write the failing test**

Create `tests/test_contract_history_sync.py`:

```python
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
from app.services.contract_history import sync_open_positions
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
    assert summary.rows_upserted == 123

    count = (await db.execute(select(func.count()).select_from(ContractHistory))).scalar()
    assert count == 123


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
    assert status.row_count == 123


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
async def test_unparseable_download_is_recorded_and_retained(db, tmp_path):
    """A malformed CSV must fail that contract, not the batch, and the file
    must survive for diagnosis."""
    await _open_position(db)
    await db.commit()

    junk = tmp_path / "junk.csv"
    junk.write_text("this is not a contract history csv\n")

    summary = await sync_open_positions(db, downloader=lambda s, o, d: junk)

    assert summary.failed == 1
    assert summary.synced == 0

    status = (await db.execute(select(ContractHistorySync))).scalars().one()
    assert status.last_success_at is None
    assert "kept at" in status.last_error

    kept = Path(tempfile.gettempdir()) / "contract-history-failures" / "HITI261016P00002500.csv"
    assert kept.exists()
    kept.unlink()


@pytest.mark.asyncio
async def test_ttl_skips_recent_sync(db):
    c = await _open_position(db)
    db.add(ContractHistorySync(
        contract_id=c.id,
        last_success_at=datetime.utcnow() - timedelta(hours=1),
        row_count=123,
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
        row_count=123,
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
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `pytest tests/test_contract_history_sync.py -v`
Expected: FAIL — `ImportError: cannot import name 'sync_open_positions'`

- [ ] **Step 4: Implement the orchestrator**

Add to `app/services/contract_history.py`:

```python
import asyncio
import shutil
import tempfile
from datetime import datetime, timedelta

from app.config import settings
from app.models import ContractHistorySync, OptionContract, OptionPosition
from app.stocknear_models import ContractHistoryError


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
    errors: list = None

    def __post_init__(self):
        if self.errors is None:
            self.errors = []


def occ_symbol(contract: OptionContract) -> str:
    """Build the OCC symbol Stocknear's contract-lookup URL expects.

    Mirrors StockNearScraper._build_contract_id, reimplemented here rather
    than imported because that module pulls in Playwright.
    """
    type_char = "P" if contract.option_type.upper() == "PUT" else "C"
    strike_int = int(Decimal(str(contract.strike)) * 1000)
    return (
        f"{contract.symbol.upper()}"
        f"{contract.expiration.strftime('%y%m%d')}"
        f"{type_char}{strike_int:08d}"
    )


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
    """
    from app.stocknear import StockNearScraper  # deferred: keeps Playwright
                                                # out of this module's import
    results: dict = {}
    with StockNearScraper() as scraper:
        for symbol, occ in jobs:
            try:
                results[occ] = scraper.download_contract_history(symbol, occ, dest_dir)
            except Exception as e:  # recorded per contract; batch continues
                logger.warning("Download failed for %s: %s", occ, e)
                results[occ] = e
    return results


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
        jobs = [(c.symbol, occ_symbol(c)) for c in due]

        if downloader is None:
            downloaded = await asyncio.to_thread(_download_batch, jobs, dest_dir)
        else:
            downloaded = {}
            for symbol, occ in jobs:
                try:
                    downloaded[occ] = downloader(symbol, occ, dest_dir)
                except Exception as e:
                    downloaded[occ] = e

        for contract in due:
            occ = occ_symbol(contract)
            status = statuses.get(contract.id)
            if status is None:
                status = ContractHistorySync(contract_id=contract.id)
                db.add(status)
                statuses[contract.id] = status
            status.last_attempt_at = now

            result = downloaded.get(occ)
            if isinstance(result, Exception) or result is None:
                message = str(result) if result is not None else "no download produced"
                status.last_error = message
                summary.failed += 1
                summary.errors.append(SyncError(contract=occ, error=message))
                continue

            try:
                rows = parse_history_csv(result)
                written = await upsert_history(db, contract.id, rows)
            except Exception as e:
                # The enclosing TemporaryDirectory is about to delete the
                # file, so copy it somewhere durable first — a malformed
                # download is exactly what you need in hand to diagnose a
                # parser failure, and it is unreproducible once discarded.
                kept = _retain_failed_download(result, occ)
                logger.exception("Parse/upsert failed for %s (kept at %s)", occ, kept)
                status.last_error = f"{type(e).__name__}: {e} (file kept at {kept})"
                summary.failed += 1
                summary.errors.append(SyncError(contract=occ, error=str(e)))
                continue

            status.last_success_at = now
            status.last_error = None
            status.row_count = written
            summary.synced += 1
            summary.rows_upserted += written

    await db.commit()
    return summary
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `pytest tests/test_contract_history_sync.py -v`
Expected: PASS, 9 tests

- [ ] **Step 6: Confirm the module is still Playwright-free at import time**

The scraper import inside `_download_batch` is deliberately deferred. Verify:

```bash
python -c "import sys; import app.services.contract_history; assert 'playwright' not in sys.modules; print('clean')"
```

Expected: `clean`

- [ ] **Step 7: Run the whole suite**

Run: `pytest -q`
Expected: PASS, no regressions.

- [ ] **Step 8: Commit**

```bash
git add app/services/contract_history.py app/config.py tests/test_contract_history_sync.py
git commit -m "feat: sync contract history for open positions"
```

---

### Task 7: Endpoint and UI button

**Files:**
- Modify: `app/main.py` (after `/api/cycles/rebuild`, around line 512)
- Modify: `templates/positions.html`

**Interfaces:**
- Consumes: `sync_open_positions`, `SyncSummary` (Task 6)
- Produces: `POST /api/contract-history/sync`

- [ ] **Step 1: Add the endpoint**

In `app/main.py`, after the `rebuild_cycles` handler:

```python
@app.post("/api/contract-history/sync")
async def sync_contract_history(force: bool = False, db: AsyncSession = Depends(get_db)):
    """Download and store price history for every open position.

    User-triggered and blocking, matching /api/positions/heal and
    /api/cycles/rebuild. One browser session covers the whole batch, so
    cost is roughly 15s of launch plus 2-4s per contract.

    Idempotent: each download is a complete history, so re-running
    converges rather than duplicating.
    """
    from app.services.contract_history import sync_open_positions

    summary = await sync_open_positions(db, force=force)
    return {
        "contracts_total": summary.contracts_total,
        "synced": summary.synced,
        "skipped": summary.skipped,
        "failed": summary.failed,
        "rows_upserted": summary.rows_upserted,
        "errors": [{"contract": e.contract, "error": e.error} for e in summary.errors],
    }
```

- [ ] **Step 2: Verify the endpoint is registered**

```bash
python -c "
from app.main import app
paths = [r.path for r in app.routes]
assert '/api/contract-history/sync' in paths, paths
print('registered')
"
```

Expected: `registered`

- [ ] **Step 3: Find the existing action-button markup**

The page needs a button matching whatever `positions.html` already uses. Locate the pattern first rather than inventing one:

```bash
grep -n "button\|fetch(\|/api/" templates/positions.html | head -30
```

- [ ] **Step 4: Add the sync button**

Add to `templates/positions.html`, following the markup and JS conventions found in Step 3. The behavior required:

- A button labelled "Sync History"
- On click: disable it, show a pending state (the request runs for a minute on a large book), `POST /api/contract-history/sync`
- On response: re-enable, and display `synced`, `skipped`, `failed`, and `rows_upserted`
- If `errors` is non-empty, list each `contract` and `error` — a `ProGatedError` on one contract must be visible, not silently folded into a count

If `positions.html` has no existing fetch-and-report pattern to copy, use this:

```html
<button id="sync-history-btn" onclick="syncContractHistory()">Sync History</button>
<span id="sync-history-status"></span>

<script>
async function syncContractHistory() {
  const btn = document.getElementById('sync-history-btn');
  const out = document.getElementById('sync-history-status');
  btn.disabled = true;
  out.textContent = 'Syncing — this can take a minute…';
  try {
    const res = await fetch('/api/contract-history/sync', {method: 'POST'});
    const data = await res.json();
    let msg = `${data.synced} synced, ${data.skipped} skipped, ` +
              `${data.failed} failed, ${data.rows_upserted} rows`;
    if (data.errors && data.errors.length) {
      msg += ' — ' + data.errors.map(e => `${e.contract}: ${e.error}`).join('; ');
    }
    out.textContent = msg;
  } catch (err) {
    out.textContent = 'Sync failed: ' + err;
  } finally {
    btn.disabled = false;
  }
}
</script>
```

- [ ] **Step 5: Verify the page renders**

Start the app and load `/positions`:

```bash
uvicorn app.main:app --port 8000 &
sleep 3
curl -s localhost:8000/positions | grep -c "sync-history-btn"
kill %1
```

Expected: `1`

- [ ] **Step 6: Commit**

```bash
git add app/main.py templates/positions.html
git commit -m "feat: add contract history sync endpoint and button"
```

---

### Task 8: Live integration test

One test that exercises the real browser path end to end. Skipped by default — it needs valid cookies and a live subscription.

**Files:**
- Create: `tests/test_contract_history_live.py`
- Modify: `pyproject.toml` (register the marker)

**Interfaces:**
- Consumes: everything above

- [ ] **Step 1: Register the marker**

In `pyproject.toml`, under `[tool.pytest.ini_options]`, add:

```toml
markers = [
    "live: requires network, valid Stocknear cookies, and a subscription (deselected by default)",
]
```

and change `addopts` to deselect it by default:

```toml
addopts = "-ra --strict-markers --tb=short -m 'not live'"
```

- [ ] **Step 2: Write the test**

Create `tests/test_contract_history_live.py`:

```python
"""Live end-to-end download against Stocknear.

Deselected by default. Run explicitly:
    pytest tests/test_contract_history_live.py -m live -v

Requires STOCKNEAR_BROWSER_PROFILE_PATH to point at a Firefox/LibreWolf
profile with a valid logged-in Stocknear session, and a subscription tier
covering the expiration below.
"""

import tempfile
from pathlib import Path

import pytest

from app.config import settings
from app.services.contract_history import parse_history_csv
from app.stocknear import StockNearScraper

CONTRACT = "HITI261016P00002500"
SYMBOL = "HITI"


@pytest.mark.live
def test_downloads_and_parses_real_contract():
    if not settings.stocknear_browser_profile_path:
        pytest.skip("STOCKNEAR_BROWSER_PROFILE_PATH not configured")

    with tempfile.TemporaryDirectory() as tmpdir:
        with StockNearScraper() as scraper:
            path = scraper.download_contract_history(SYMBOL, CONTRACT, Path(tmpdir))

        assert path.exists()
        rows = parse_history_csv(path)

        assert len(rows) > 50
        assert all(r.date is not None for r in rows)
        # IV is a decimal fraction, not a percentage.
        ivs = [r.implied_volatility for r in rows if r.implied_volatility is not None]
        assert ivs and all(0 < iv < 10 for iv in ivs)
```

- [ ] **Step 3: Verify it is deselected by default**

Run: `pytest -q`
Expected: PASS, and the live test does not run. Confirm with `pytest --collect-only -q | grep -c live` returning `0`.

- [ ] **Step 4: Run it explicitly (requires auth)**

Run: `pytest tests/test_contract_history_live.py -m live -v`
Expected: PASS if cookies are valid and the expiration is within your tier. A `ProGatedError` here means the subscription does not cover 2026-10-16 — that is the guard working, not a test bug.

- [ ] **Step 5: Commit**

```bash
git add tests/test_contract_history_live.py pyproject.toml
git commit -m "test: add live contract history download test"
```

---

## Verification

After all tasks:

```bash
pytest -q                      # full suite, live tests deselected
mypy app/                      # no new errors
alembic upgrade head           # migration applies cleanly
```

Then click **Sync History** on `/positions` with at least one open position and confirm the summary reports a non-zero `rows_upserted`.
