"""Fetch + parse company fundamentals from StockNear's financials pages.

StockNear is a SvelteKit app. Each financials page exposes its server-loaded
data at a sibling ``__data.json`` URL, serialized with **devalue** (a flattened
array format where values reference each other by integer index). We fetch two
of these per ticker — income statement and balance sheet — decode the devalue
envelope, pull the annual statement series, and reduce them to the inputs the
Reality Gap math needs (app/services/rg_math.py).

Why ``__data.json`` instead of Playwright DOM scraping:
  - It's a plain authenticated GET (cookies from app/stocknear_cookies.py) —
    no browser launch, ~300ms vs ~5s.
  - It returns the full annual history for premium accounts (anonymous /
    free accounts are capped to the most recent ~5 fiscal years by the
    server; the older years simply aren't in the payload).

The ``parse_*`` functions are pure (str -> data) so they can be unit-tested
against captured fixtures without any network or auth.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from app.stocknear_models import Fundamentals

logger = logging.getLogger(__name__)

# Path suffixes for the two SvelteKit __data.json endpoints we read per ticker.
INCOME_DATA_PATH = "/stocks/{sym}/financials/__data.json"
BALANCE_DATA_PATH = "/stocks/{sym}/financials/balance-sheet/__data.json"


# ---------------------------------------------------------------------------
# devalue decoding
# ---------------------------------------------------------------------------

def _unflatten(values: list) -> Any:
    """Decode one devalue-flattened array into a normal nested structure.

    Mirrors devalue's reference scheme: each element is either a literal or an
    integer index pointing elsewhere in the array. Negative sentinels encode
    undefined/NaN/±Infinity. A plain list is a list of index references; a list
    whose first element is a string is a tagged special type (Date/Set/Map/…)
    which we keep as-is (we never need them here). Objects map keys -> indices.
    """
    n = len(values)
    hydrated: dict[int, Any] = {}

    def hydrate(index: int) -> Any:
        if not isinstance(index, int):
            return index
        if index == -1:
            return None       # undefined
        if index == -3:
            return float("nan")
        if index == -4:
            return float("inf")
        if index == -5:
            return float("-inf")
        if index < 0 or index >= n:
            return None
        if index in hydrated:
            return hydrated[index]
        value = values[index]
        if not isinstance(value, (list, dict)):
            hydrated[index] = value
            return value
        if isinstance(value, list):
            if value and isinstance(value[0], str):
                hydrated[index] = value     # tagged special type — leave raw
                return value
            arr: list = []
            hydrated[index] = arr
            for i in value:
                arr.append(hydrate(i))
            return arr
        obj: dict = {}
        hydrated[index] = obj
        for k, i in value.items():
            obj[k] = hydrate(i)
        return obj

    return hydrate(0)


def _decode_data_json(raw_text: str) -> list:
    """Decode every ``type:"data"`` node in a SvelteKit __data.json envelope,
    returning the list of decoded roots (later nodes carry the page's load
    data; earlier ones carry layout data)."""
    payload = json.loads(raw_text)
    roots: list = []
    for node in payload.get("nodes", []):
        if isinstance(node, dict) and node.get("type") == "data" and isinstance(node.get("data"), list):
            try:
                roots.append(_unflatten(node["data"]))
            except Exception as exc:  # one bad node shouldn't kill the parse
                logger.debug("devalue decode failed for a node: %s", exc)
    return roots


# ---------------------------------------------------------------------------
# statement extraction
# ---------------------------------------------------------------------------

def _year_of(row: dict) -> Optional[int]:
    for key in ("fiscalYear", "calendarYear"):
        val = row.get(key)
        if val not in (None, ""):
            try:
                return int(str(val)[:4])
            except (ValueError, TypeError):
                pass
    date = row.get("date") or row.get("fiscalDate")
    if date:
        try:
            return int(str(date)[:4])
        except (ValueError, TypeError):
            pass
    return None


def _collect_candidate_arrays(roots: list, required_key: str) -> list[list[dict]]:
    """Find every array of dict-rows that look like a financial statement
    (rows carry `required_key` and a year-ish field)."""
    found: list[list[dict]] = []
    seen: set[int] = set()

    def walk(node: Any) -> None:
        if id(node) in seen:
            return
        seen.add(id(node))
        if isinstance(node, list):
            if node and isinstance(node[0], dict) and required_key in node[0] and _year_of(node[0]) is not None:
                found.append(node)
            for item in node:
                walk(item)
        elif isinstance(node, dict):
            for value in node.values():
                walk(value)

    for root in roots:
        walk(root)
    return found


def _pick_annual_series(roots: list, required_key: str) -> dict[int, dict]:
    """Select the annual statement and return {fiscal_year: row}.

    Quarterly arrays have several rows per year; annual arrays have one. We
    prefer rows explicitly tagged ``period == "FY"``; absent that tag, we pick
    the candidate array that is most "annual-shaped" (row count ≈ distinct
    years) and the longest such. Returns {} when nothing matches.
    """
    candidates = _collect_candidate_arrays(roots, required_key)
    if not candidates:
        return {}

    def score(arr: list[dict]) -> tuple[int, int]:
        years = [_year_of(r) for r in arr if isinstance(r, dict)]
        years = [y for y in years if y is not None]
        distinct = len(set(years))
        # annual-shaped: at most one extra row (e.g. a TTM row) beyond distinct years
        is_annual = 1 if years and len(years) <= distinct + 1 else 0
        return (is_annual, distinct)

    # Filter to FY rows first if any candidate uses period tags.
    fy_filtered: list[list[dict]] = []
    for arr in candidates:
        fy_rows = [r for r in arr if isinstance(r, dict) and str(r.get("period", "")).upper() in ("", "FY")]
        if fy_rows:
            fy_filtered.append(fy_rows)
    pool = fy_filtered or candidates

    best = max(pool, key=score)
    series: dict[int, dict] = {}
    for row in best:
        if not isinstance(row, dict):
            continue
        year = _year_of(row)
        if year is None:
            continue
        # Last write wins; rows are typically chronological so this keeps the
        # most recent restatement for a given year.
        series[year] = row
    return series


def _find_scalar(roots: list, key: str) -> Optional[float]:
    """Depth-first search for the first numeric occurrence of `key`."""
    seen: set[int] = set()
    stack: list[Any] = list(roots)
    while stack:
        node = stack.pop()
        if id(node) in seen:
            continue
        seen.add(id(node))
        if isinstance(node, dict):
            val = node.get(key)
            if isinstance(val, (int, float)) and not isinstance(val, bool):
                return float(val)
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return None


def _num(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# pure parsers (str -> data) — unit-testable without network
# ---------------------------------------------------------------------------

def parse_income_statement(raw_text: str) -> dict[int, float]:
    """Return {fiscal_year: net_income} from an income-statement __data.json."""
    roots = _decode_data_json(raw_text)
    series = _pick_annual_series(roots, "netIncome")
    out: dict[int, float] = {}
    for year, row in series.items():
        ni = _num(row.get("netIncome"))
        if ni is not None:
            out[year] = ni
    return out


def parse_balance_sheet(raw_text: str) -> dict[str, Any]:
    """Return latest-year balance-sheet figures for tangible equity:
    {year, book_equity, goodwill, intangibles}."""
    roots = _decode_data_json(raw_text)
    series = _pick_annual_series(roots, "totalStockholdersEquity")
    if not series:
        series = _pick_annual_series(roots, "totalEquity")
    if not series:
        return {}
    year = max(series)
    row = series[year]

    book_equity = _num(row.get("totalStockholdersEquity"))
    if book_equity is None:
        book_equity = _num(row.get("totalEquity"))

    goodwill = _num(row.get("goodwill"))
    intangibles = _num(row.get("intangibleAssets"))
    combined = _num(row.get("goodwillAndIntangibleAssets"))
    # Backfill whichever of goodwill / intangibles is missing from the combined
    # line so TE = equity − goodwill − intangibles never double-counts.
    if goodwill is None and combined is not None and intangibles is not None:
        goodwill = combined - intangibles
    if intangibles is None and combined is not None and goodwill is not None:
        intangibles = combined - goodwill
    if goodwill is None and intangibles is None and combined is not None:
        goodwill, intangibles = combined, 0.0

    return {
        "year": year,
        "book_equity": book_equity,
        "goodwill": goodwill if goodwill is not None else 0.0,
        "intangibles": intangibles if intangibles is not None else 0.0,
    }


def parse_market_cap(raw_text: str) -> Optional[float]:
    """Pull marketCap from any __data.json payload (it rides along in the
    stock-deck section of the financials pages)."""
    roots = _decode_data_json(raw_text)
    return _find_scalar(roots, "marketCap")


# ---------------------------------------------------------------------------
# assembly (pure)
# ---------------------------------------------------------------------------

def build_fundamentals(
    symbol: str,
    income_text: Optional[str],
    balance_text: Optional[str],
) -> Fundamentals:
    """Assemble a Fundamentals from the two raw ``__data.json`` payloads.

    Pure (no network): the caller is responsible for fetching the two texts
    through the authenticated StockNear Playwright session. Either text may be
    None (fetch failed) and we degrade to whatever inputs we have.
    """
    net_income_by_year = parse_income_statement(income_text) if income_text else {}
    balance = parse_balance_sheet(balance_text) if balance_text else {}
    market_cap = None
    for text in (income_text, balance_text):
        if text:
            market_cap = parse_market_cap(text)
            if market_cap is not None:
                break

    return Fundamentals(
        symbol=symbol.upper(),
        market_cap=market_cap,
        book_equity=balance.get("book_equity"),
        goodwill=balance.get("goodwill"),
        intangibles=balance.get("intangibles"),
        net_income_by_year=net_income_by_year,
        balance_sheet_year=balance.get("year"),
    )
