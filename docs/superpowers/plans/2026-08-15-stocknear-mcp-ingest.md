# StockNear MCP Ingest Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Playwright scraper with the StockNear MCP server for symbol-level market data, keeping Playwright only for contract-level quotes.

**Architecture:** A new module `app/services/stocknear_mcp.py` speaks JSON-RPC over HTTP to `https://mcp.stocknear.com/mcp` using `httpx`, and exposes four async fetchers returning the dataclasses the application already uses. `app/services/stocknear_service.py` swaps its scrape thunks for those fetchers while keeping every cache key, TTL, and fallback path byte-identical. The scraper's now-unused symbol-level methods are deleted.

**Tech Stack:** Python 3.12, FastAPI, `httpx` (already a dependency), pydantic-settings, pytest + pytest-asyncio. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-08-15-stocknear-mcp-ingest-design.md`

## Global Constraints

- **No new dependencies.** `httpx` is already in `pyproject.toml`. Do not add the `mcp` SDK.
- **Every outbound request must carry a non-empty `User-Agent` header.** Cloudflare rejects the httpx default with `403 error 1010`. Use `options-analyzer/1.0`.
- **The MCP server is stateless.** No `initialize` handshake, no `Mcp-Session-Id` header. `tools/call` works cold.
- **`implied_volatility` and `historical_volatility` are decimals throughout this repository** (`0.35` = 35%). The MCP server returns percentages (`33.65`). Divide by 100.
- **Cache keys, TTLs, and cached JSON shapes must not change.** Existing rows in the `stocknear_cache` table stay valid after this work.
- **The bearer token is a credential.** It goes in `.env` (gitignored) via `STOCKNEAR_MCP_TOKEN`. `.env.example` documents the name with an empty value. Never commit a real token.
- **Tests never touch the network.** All HTTP is mocked via `httpx.MockTransport`.
- Run tests with `uv run pytest`. Sync deps with `uv sync --extra dev`.

---

### Task 1: MCP transport client and configuration

**Files:**
- Create: `app/services/stocknear_mcp.py`
- Modify: `app/config.py:22-27` (StockNear configuration block)
- Modify: `.env.example` (StockNear Configuration section)
- Test: `tests/test_stocknear_mcp_transport.py`

**Interfaces:**
- Consumes: `app.config.settings`
- Produces:
  - `StockNearMCPError(Exception)` — base for all MCP failures
  - `StockNearMCPNoData(StockNearMCPError)` — server returned no data for the symbol
  - `USER_AGENT: str` — `"options-analyzer/1.0"`
  - `def _make_client() -> httpx.AsyncClient` — client factory; tests monkeypatch this seam
  - `async def call_tool(name: str, arguments: dict) -> Any` — returns the parsed tool payload
  - `settings.stocknear_mcp_url: str`, `settings.stocknear_mcp_token: str`, `settings.stocknear_mcp_timeout: int`

- [ ] **Step 1: Add configuration fields**

In `app/config.py`, inside the `Settings` class, extend the StockNear block (currently lines 22-27) by appending these three fields after `stocknear_cache_ttl_seconds`:

```python
    # StockNear MCP server. Symbol-level data (options overview, max pain,
    # stock quote, expirations) comes from here rather than the scraper.
    # The token is a credential — set it in .env, never commit it.
    stocknear_mcp_url: str = "https://mcp.stocknear.com/mcp"
    stocknear_mcp_token: str = ""
    stocknear_mcp_timeout: int = 30
```

- [ ] **Step 2: Document the configuration in `.env.example`**

Append to the `# StockNear Configuration` section of `.env.example`:

```bash
# StockNear MCP server (symbol-level data: options overview, max pain, quotes)
STOCKNEAR_MCP_URL=https://mcp.stocknear.com/mcp
# Bearer token for the MCP server. Required. Get one from stocknear.com.
# This is a credential — keep it in .env only, never commit a real value.
STOCKNEAR_MCP_TOKEN=
# Request timeout in seconds
STOCKNEAR_MCP_TIMEOUT=30
```

- [ ] **Step 3: Write the failing transport tests**

Create `tests/test_stocknear_mcp_transport.py`:

```python
"""Transport-layer tests for the StockNear MCP client.

All HTTP is mocked through httpx.MockTransport — these tests never touch
the network. The client factory `_make_client` is the monkeypatch seam.
"""

import json

import httpx
import pytest

from app.services import stocknear_mcp
from app.services.stocknear_mcp import (
    StockNearMCPError,
    call_tool,
)


def _install_transport(monkeypatch, handler):
    """Point the module's client factory at a MockTransport."""
    def factory():
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(stocknear_mcp, "_make_client", factory)


def _tool_response(payload: dict) -> httpx.Response:
    """Build the JSON-RPC envelope the real server returns."""
    return httpx.Response(
        200,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "isError": False,
            },
        },
    )


@pytest.mark.asyncio
async def test_call_tool_unwraps_text_content(monkeypatch):
    _install_transport(monkeypatch, lambda request: _tool_response({"AAPL": {"price": 305.93}}))

    result = await call_tool("get_ticker_quote", {"tickers": ["AAPL"]})

    assert result == {"AAPL": {"price": 305.93}}


@pytest.mark.asyncio
async def test_every_request_sends_non_empty_user_agent(monkeypatch):
    """Cloudflare returns 403 error 1010 on the httpx default UA."""
    seen = {}

    def handler(request):
        seen["ua"] = request.headers.get("user-agent")
        return _tool_response({})

    _install_transport(monkeypatch, handler)
    await call_tool("get_ticker_quote", {"tickers": ["AAPL"]})

    assert seen["ua"]
    assert seen["ua"] != ""
    assert "python-httpx" not in seen["ua"]


@pytest.mark.asyncio
async def test_request_sends_bearer_token_and_jsonrpc_envelope(monkeypatch):
    seen = {}

    def handler(request):
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return _tool_response({})

    monkeypatch.setattr(stocknear_mcp.settings, "stocknear_mcp_token", "sn_test_token")
    _install_transport(monkeypatch, handler)
    await call_tool("get_ticker_quote", {"tickers": ["AAPL"]})

    assert seen["auth"] == "Bearer sn_test_token"
    assert seen["body"]["jsonrpc"] == "2.0"
    assert seen["body"]["method"] == "tools/call"
    assert seen["body"]["params"] == {
        "name": "get_ticker_quote",
        "arguments": {"tickers": ["AAPL"]},
    }


@pytest.mark.asyncio
async def test_is_error_response_raises(monkeypatch):
    def handler(request):
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "content": [{"type": "text", "text": "rate limited"}],
                    "isError": True,
                },
            },
        )

    _install_transport(monkeypatch, handler)
    with pytest.raises(StockNearMCPError):
        await call_tool("get_ticker_quote", {"tickers": ["AAPL"]})


@pytest.mark.asyncio
async def test_jsonrpc_error_response_raises(monkeypatch):
    def handler(request):
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": 1, "error": {"code": -32601, "message": "no such tool"}},
        )

    _install_transport(monkeypatch, handler)
    with pytest.raises(StockNearMCPError):
        await call_tool("nope", {})


@pytest.mark.asyncio
async def test_http_error_raises(monkeypatch):
    _install_transport(monkeypatch, lambda request: httpx.Response(403, text="denied"))

    with pytest.raises(StockNearMCPError):
        await call_tool("get_ticker_quote", {"tickers": ["AAPL"]})


@pytest.mark.asyncio
async def test_missing_text_content_raises(monkeypatch):
    def handler(request):
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": 1, "result": {"content": [], "isError": False}}
        )

    _install_transport(monkeypatch, handler)
    with pytest.raises(StockNearMCPError):
        await call_tool("get_ticker_quote", {"tickers": ["AAPL"]})
```

- [ ] **Step 4: Run tests to verify they fail**

Run: `uv run pytest tests/test_stocknear_mcp_transport.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.stocknear_mcp'`

- [ ] **Step 5: Write the transport implementation**

Create `app/services/stocknear_mcp.py`:

```python
"""StockNear MCP client.

Symbol-level market data (options overview, max pain, stock quote,
expirations) comes from StockNear's MCP server rather than the Playwright
scraper. Contract-level quotes still require the scraper — the MCP server
exposes no bid, ask, or greeks for an arbitrary strike.

The server is stateless: `tools/call` succeeds cold, with no `initialize`
handshake and no session header. Responses are plain JSON, not SSE, and the
tool payload arrives as a JSON string inside `result.content[0].text`. That
is why this module needs no MCP SDK — httpx is enough.
"""

import json
import logging
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

# Cloudflare fronts the MCP server and rejects httpx's default User-Agent
# with `403 error 1010` ("Access denied ... based on your browser's
# signature"). Any non-empty value passes. Do not remove this header.
USER_AGENT = "options-analyzer/1.0"


class StockNearMCPError(Exception):
    """The MCP request failed: transport, protocol, or server-side error."""


class StockNearMCPNoData(StockNearMCPError):
    """The MCP server answered successfully but has no data for the symbol.

    An unknown ticker comes back as `{}` with `isError: false`, so the
    absence of data is not reported as an error and must be detected here.
    Kept distinct from the base class so callers can avoid overwriting a
    populated cache entry with nulls.
    """


def _make_client() -> httpx.AsyncClient:
    """Build the HTTP client.

    Factored out as the seam tests monkeypatch to install a MockTransport.
    """
    return httpx.AsyncClient(timeout=settings.stocknear_mcp_timeout)


async def _rpc(method: str, params: dict) -> dict:
    """Send one JSON-RPC request and return its `result` object."""
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    headers = {
        "Authorization": f"Bearer {settings.stocknear_mcp_token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "User-Agent": USER_AGENT,
    }

    try:
        async with _make_client() as client:
            response = await client.post(
                settings.stocknear_mcp_url, json=payload, headers=headers
            )
            response.raise_for_status()
            body = response.json()
    except httpx.HTTPError as exc:
        raise StockNearMCPError(f"MCP request failed for {method}: {exc}") from exc
    except ValueError as exc:
        raise StockNearMCPError(f"MCP returned malformed JSON for {method}: {exc}") from exc

    if "error" in body:
        raise StockNearMCPError(f"MCP error for {method}: {body['error']}")

    result = body.get("result")
    if not isinstance(result, dict):
        raise StockNearMCPError(f"MCP response for {method} has no result object")

    return result


async def call_tool(name: str, arguments: dict) -> Any:
    """Call an MCP tool and return its decoded payload."""
    result = await _rpc("tools/call", {"name": name, "arguments": arguments})

    if result.get("isError"):
        raise StockNearMCPError(f"MCP tool {name} reported an error: {result.get('content')}")

    for block in result.get("content", []):
        if block.get("type") == "text":
            try:
                return json.loads(block["text"])
            except (ValueError, KeyError) as exc:
                raise StockNearMCPError(
                    f"MCP tool {name} returned undecodable text content: {exc}"
                ) from exc

    raise StockNearMCPError(f"MCP tool {name} returned no text content block")
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/test_stocknear_mcp_transport.py -v`
Expected: PASS — 7 passed

- [ ] **Step 7: Commit**

```bash
git add app/services/stocknear_mcp.py app/config.py .env.example tests/test_stocknear_mcp_transport.py
git commit -m "feat: add StockNear MCP transport client"
```

---

### Task 2: Options overview and max pain fetchers

**Files:**
- Modify: `app/services/stocknear_mcp.py` (append)
- Create: `tests/fixtures/stocknear_mcp/options_overview_aapl.json`
- Test: `tests/test_stocknear_mcp_overview.py`

**Interfaces:**
- Consumes: `call_tool`, `StockNearMCPNoData` from Task 1; `OptionsData` from `app.stocknear_models`
- Produces:
  - `def _pct_to_decimal(value: float | None) -> float | None`
  - `def _select_max_pain(table: list[dict], today: date) -> float | None`
  - `async def fetch_options_overview(symbol: str) -> OptionsData`

**Why this shape:** `_select_max_pain` takes `today` as a parameter rather than calling `date.today()` internally so the test can pin a date and stay deterministic forever.

- [ ] **Step 1: Create the recorded fixture**

Create `tests/fixtures/stocknear_mcp/options_overview_aapl.json`. This is a real `get_ticker_options_overview_data` payload for AAPL, trimmed to four expiry rows (one of them with `maxPain: 0`, which the selector must skip):

```json
{
  "AAPL": {
    "overview": {
      "date": "August 15, 2026",
      "currentIV": 33.65,
      "ivRank": null,
      "totalVolume": 419997,
      "avgDailyVolume": 394665,
      "putCallRatio": 0.43,
      "sentiment": "bullish",
      "totalOpenInterest": 4719412
    },
    "impliedVolatility": {
      "current": 33.65,
      "ivRank": null,
      "ivPercentile": 0.0,
      "historicalVolatility": 32.06,
      "ivHigh": 53.14,
      "ivLow": 35.22
    },
    "openInterest": {
      "total": 4719412,
      "calls": 2743811,
      "puts": 1975601
    },
    "volume": {
      "total": 419997,
      "calls": 293010,
      "puts": 126987
    },
    "table": [
      {
        "expiration": "2026-08-17",
        "callVol": 95479.0,
        "putVol": 40331.0,
        "callOI": 24675.0,
        "putOI": 11810.0,
        "avgIV": 30.17,
        "maxPain": 300.0
      },
      {
        "expiration": "2026-08-19",
        "callVol": 24892.0,
        "putVol": 7253.0,
        "callOI": 8337.0,
        "putOI": 3029.0,
        "avgIV": 32.71,
        "maxPain": 302.5
      },
      {
        "expiration": "2026-10-02",
        "callVol": 1199.0,
        "putVol": 430.0,
        "callOI": 0,
        "putOI": 0,
        "avgIV": 25.29,
        "maxPain": 0
      },
      {
        "expiration": "2026-10-16",
        "callVol": 9828.0,
        "putVol": 3231.0,
        "callOI": 262350.0,
        "putOI": 143401.0,
        "avgIV": 34.41,
        "maxPain": 300.0
      }
    ]
  }
}
```

- [ ] **Step 2: Write the failing overview tests**

Create `tests/test_stocknear_mcp_overview.py`:

```python
"""Mapping tests for the options-overview fetcher.

The volatility conversion test is the important one: the MCP server reports
IV as a percentage (33.65) while every consumer in this repo treats
implied_volatility as a decimal (0.3365). A missing division would inflate
every Black-Scholes input 100x without raising anything.
"""

import json
from datetime import date
from pathlib import Path

import pytest

from app.services import stocknear_mcp
from app.services.stocknear_mcp import (
    StockNearMCPNoData,
    _pct_to_decimal,
    _select_max_pain,
    fetch_options_overview,
)

FIXTURES = Path(__file__).parent / "fixtures" / "stocknear_mcp"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def _stub_call_tool(monkeypatch, payload):
    async def fake(name, arguments):
        return payload
    monkeypatch.setattr(stocknear_mcp, "call_tool", fake)


def test_pct_to_decimal_divides_by_100():
    assert _pct_to_decimal(33.65) == pytest.approx(0.3365)


def test_pct_to_decimal_passes_none_through():
    assert _pct_to_decimal(None) is None


def test_pct_to_decimal_keeps_zero_as_zero():
    """0.0 is a real value, not a missing one — must not become None."""
    assert _pct_to_decimal(0.0) == 0.0


def test_select_max_pain_takes_nearest_future_expiry():
    table = [
        {"expiration": "2026-08-17", "maxPain": 300.0},
        {"expiration": "2026-08-19", "maxPain": 302.5},
    ]
    assert _select_max_pain(table, date(2026, 8, 15)) == 300.0


def test_select_max_pain_skips_past_expiries():
    table = [
        {"expiration": "2026-08-17", "maxPain": 300.0},
        {"expiration": "2026-10-16", "maxPain": 310.0},
    ]
    assert _select_max_pain(table, date(2026, 9, 1)) == 310.0


def test_select_max_pain_skips_zero_rows():
    """maxPain of 0 means the server has no figure, not a strike of $0."""
    table = [
        {"expiration": "2026-10-02", "maxPain": 0},
        {"expiration": "2026-10-16", "maxPain": 300.0},
    ]
    assert _select_max_pain(table, date(2026, 8, 15)) == 300.0


def test_select_max_pain_returns_none_when_nothing_usable():
    table = [{"expiration": "2026-08-17", "maxPain": 0}]
    assert _select_max_pain(table, date(2027, 1, 1)) is None


def test_select_max_pain_handles_unsorted_table():
    table = [
        {"expiration": "2026-10-16", "maxPain": 310.0},
        {"expiration": "2026-08-17", "maxPain": 300.0},
    ]
    assert _select_max_pain(table, date(2026, 8, 15)) == 300.0


@pytest.mark.asyncio
async def test_fetch_options_overview_converts_volatility_to_decimal(monkeypatch):
    _stub_call_tool(monkeypatch, _load("options_overview_aapl.json"))

    data = await fetch_options_overview("AAPL")

    assert data.implied_volatility == pytest.approx(0.3365)
    assert data.historical_volatility == pytest.approx(0.3206)


@pytest.mark.asyncio
async def test_fetch_options_overview_maps_remaining_fields(monkeypatch):
    _stub_call_tool(monkeypatch, _load("options_overview_aapl.json"))

    data = await fetch_options_overview("AAPL")

    assert data.symbol == "AAPL"
    assert data.iv_percentile == 0.0
    assert data.put_call_ratio == 0.43
    assert data.total_volume == 419997
    assert data.total_open_interest == 4719412


@pytest.mark.asyncio
async def test_fetch_options_overview_keeps_null_iv_rank_as_none(monkeypatch):
    """ivRank is frequently null upstream. It must not become 0."""
    _stub_call_tool(monkeypatch, _load("options_overview_aapl.json"))

    data = await fetch_options_overview("AAPL")

    assert data.iv_rank is None


@pytest.mark.asyncio
async def test_fetch_options_overview_stores_raw_payload(monkeypatch):
    payload = _load("options_overview_aapl.json")
    _stub_call_tool(monkeypatch, payload)

    data = await fetch_options_overview("AAPL")

    assert json.loads(data.raw_content) == payload["AAPL"]


@pytest.mark.asyncio
async def test_fetch_options_overview_uppercases_symbol(monkeypatch):
    _stub_call_tool(monkeypatch, _load("options_overview_aapl.json"))

    data = await fetch_options_overview("aapl")

    assert data.symbol == "AAPL"


@pytest.mark.asyncio
async def test_fetch_options_overview_raises_no_data_on_empty_payload(monkeypatch):
    """An unknown ticker returns {} with isError false."""
    _stub_call_tool(monkeypatch, {})

    with pytest.raises(StockNearMCPNoData):
        await fetch_options_overview("ZZZZNOTREAL")


@pytest.mark.asyncio
async def test_fetch_options_overview_raises_no_data_when_symbol_key_absent(monkeypatch):
    _stub_call_tool(monkeypatch, {"MSFT": {"overview": {}}})

    with pytest.raises(StockNearMCPNoData):
        await fetch_options_overview("AAPL")
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_stocknear_mcp_overview.py -v`
Expected: FAIL — `ImportError: cannot import name '_pct_to_decimal' from 'app.services.stocknear_mcp'`

- [ ] **Step 4: Write the overview implementation**

Append to `app/services/stocknear_mcp.py`. Add `from datetime import date` to the imports and `from app.stocknear_models import OptionsData`:

```python
def _pct_to_decimal(value: float | None) -> float | None:
    """Convert a percentage figure to a decimal fraction.

    The MCP server reports volatility as a percentage (33.65). Everything in
    this repo — risk_analysis, speculation_analysis, bs_math — expects a
    decimal (0.3365). Getting this wrong inflates every Black-Scholes input
    by 100x silently, so it lives in one named function with its own test.
    """
    if value is None:
        return None
    return value / 100


def _select_max_pain(table: list[dict], today: date) -> float | None:
    """Pick the max pain strike from the nearest expiry that still has one.

    Rows whose `maxPain` is 0 mean the server has no figure for that expiry,
    not that the strike is zero — skip them.
    """
    candidates = []
    for row in table or []:
        expiration = row.get("expiration")
        max_pain = row.get("maxPain")
        if not expiration or not max_pain:
            continue
        try:
            if date.fromisoformat(expiration) >= today:
                candidates.append((expiration, float(max_pain)))
        except ValueError:
            logger.debug("Skipping unparseable expiration %r", expiration)

    if not candidates:
        return None
    candidates.sort()
    return candidates[0][1]


def _require_symbol(payload: Any, symbol: str, tool: str) -> dict:
    """Pull one symbol's object out of a tool payload, or signal no data."""
    if not isinstance(payload, dict) or not payload.get(symbol):
        raise StockNearMCPNoData(f"{tool} returned no data for {symbol}")
    return payload[symbol]


async def fetch_options_overview(symbol: str) -> OptionsData:
    """Symbol-level options statistics: IV, HV, put/call ratio, volume, OI."""
    symbol = symbol.upper()
    payload = await call_tool("get_ticker_options_overview_data", {"tickers": [symbol]})
    data = _require_symbol(payload, symbol, "get_ticker_options_overview_data")

    overview = data.get("overview") or {}
    volatility = data.get("impliedVolatility") or {}

    return OptionsData(
        symbol=symbol,
        iv_rank=volatility.get("ivRank"),
        iv_percentile=volatility.get("ivPercentile"),
        implied_volatility=_pct_to_decimal(volatility.get("current")),
        historical_volatility=_pct_to_decimal(volatility.get("historicalVolatility")),
        put_call_ratio=overview.get("putCallRatio"),
        total_volume=overview.get("totalVolume"),
        total_open_interest=overview.get("totalOpenInterest"),
        max_pain=_select_max_pain(data.get("table"), date.today()),
        raw_content=json.dumps(data, default=str),
    )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_stocknear_mcp_overview.py -v`
Expected: PASS — 15 passed

- [ ] **Step 6: Commit**

```bash
git add app/services/stocknear_mcp.py tests/test_stocknear_mcp_overview.py tests/fixtures/stocknear_mcp/options_overview_aapl.json
git commit -m "feat: fetch options overview and max pain from StockNear MCP"
```

---

### Task 3: Stock quote and expirations fetchers

**Files:**
- Modify: `app/services/stocknear_mcp.py` (append)
- Create: `tests/fixtures/stocknear_mcp/quote_aapl.json`
- Test: `tests/test_stocknear_mcp_quote.py`

**Interfaces:**
- Consumes: `call_tool`, `_require_symbol`, `StockNearMCPNoData` from Tasks 1-2; `StockData` from `app.stocknear_models`
- Produces:
  - `async def fetch_stock_overview(symbol: str) -> StockData`
  - `async def fetch_expirations(symbol: str) -> list[str]`

- [ ] **Step 1: Create the quote fixture**

Create `tests/fixtures/stocknear_mcp/quote_aapl.json`, a real `get_ticker_quote` payload:

```json
{
  "AAPL": {
    "symbol": "AAPL",
    "name": "Apple Inc.",
    "price": 305.93,
    "changesPercentage": 0.21949,
    "change": 0.67,
    "dayLow": 304.3,
    "dayHigh": 307.49,
    "yearHigh": 344.57,
    "yearLow": 223.78,
    "marketCap": 4493302821080,
    "priceAvg50": 309.2774,
    "priceAvg200": 280.29807,
    "exchange": "NASDAQ",
    "volume": 26054077,
    "avgVolume": 56620916
  }
}
```

- [ ] **Step 2: Write the failing tests**

Create `tests/test_stocknear_mcp_quote.py`:

```python
"""Mapping tests for the stock-quote and expirations fetchers."""

import json
from pathlib import Path

import pytest

from app.services import stocknear_mcp
from app.services.stocknear_mcp import (
    StockNearMCPNoData,
    fetch_expirations,
    fetch_stock_overview,
)

FIXTURES = Path(__file__).parent / "fixtures" / "stocknear_mcp"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def _stub_call_tool(monkeypatch, payload):
    async def fake(name, arguments):
        return payload
    monkeypatch.setattr(stocknear_mcp, "call_tool", fake)


@pytest.mark.asyncio
async def test_fetch_stock_overview_maps_fields(monkeypatch):
    _stub_call_tool(monkeypatch, _load("quote_aapl.json"))

    data = await fetch_stock_overview("AAPL")

    assert data.symbol == "AAPL"
    assert data.price == 305.93
    assert data.change == 0.67
    assert data.change_percent == pytest.approx(0.21949)
    assert data.volume == 26054077


@pytest.mark.asyncio
async def test_fetch_stock_overview_stringifies_market_cap(monkeypatch):
    """StockData.market_cap is typed str; the MCP server sends an int."""
    _stub_call_tool(monkeypatch, _load("quote_aapl.json"))

    data = await fetch_stock_overview("AAPL")

    assert data.market_cap == "4493302821080"


@pytest.mark.asyncio
async def test_fetch_stock_overview_raises_no_data_on_empty_payload(monkeypatch):
    _stub_call_tool(monkeypatch, {})

    with pytest.raises(StockNearMCPNoData):
        await fetch_stock_overview("ZZZZNOTREAL")


@pytest.mark.asyncio
async def test_fetch_expirations_returns_sorted_dates(monkeypatch):
    _stub_call_tool(monkeypatch, _load("options_overview_aapl.json"))

    expirations = await fetch_expirations("AAPL")

    assert expirations == ["2026-08-17", "2026-08-19", "2026-10-02", "2026-10-16"]


@pytest.mark.asyncio
async def test_fetch_expirations_sorts_unordered_table(monkeypatch):
    _stub_call_tool(monkeypatch, {"AAPL": {"table": [
        {"expiration": "2026-10-16"},
        {"expiration": "2026-08-17"},
    ]}})

    assert await fetch_expirations("AAPL") == ["2026-08-17", "2026-10-16"]


@pytest.mark.asyncio
async def test_fetch_expirations_deduplicates(monkeypatch):
    _stub_call_tool(monkeypatch, {"AAPL": {"table": [
        {"expiration": "2026-08-17"},
        {"expiration": "2026-08-17"},
    ]}})

    assert await fetch_expirations("AAPL") == ["2026-08-17"]


@pytest.mark.asyncio
async def test_fetch_expirations_raises_no_data_on_empty_payload(monkeypatch):
    _stub_call_tool(monkeypatch, {})

    with pytest.raises(StockNearMCPNoData):
        await fetch_expirations("ZZZZNOTREAL")
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_stocknear_mcp_quote.py -v`
Expected: FAIL — `ImportError: cannot import name 'fetch_stock_overview'`

- [ ] **Step 4: Write the implementation**

Append to `app/services/stocknear_mcp.py`. Add `StockData` to the `app.stocknear_models` import:

```python
async def fetch_stock_overview(symbol: str) -> StockData:
    """Latest stock quote: price, change, market cap, volume."""
    symbol = symbol.upper()
    payload = await call_tool("get_ticker_quote", {"tickers": [symbol]})
    data = _require_symbol(payload, symbol, "get_ticker_quote")

    market_cap = data.get("marketCap")

    return StockData(
        symbol=symbol,
        price=data.get("price"),
        change=data.get("change"),
        change_percent=data.get("changesPercentage"),
        market_cap=str(market_cap) if market_cap is not None else None,
        volume=data.get("volume"),
        raw_content=json.dumps(data, default=str),
    )


async def fetch_expirations(symbol: str) -> list[str]:
    """Available option expiration dates, ascending ISO strings.

    Derived from the options-overview expiry table, which the server already
    filters to future expiries.
    """
    symbol = symbol.upper()
    payload = await call_tool("get_ticker_options_overview_data", {"tickers": [symbol]})
    data = _require_symbol(payload, symbol, "get_ticker_options_overview_data")

    expirations = {
        row["expiration"]
        for row in data.get("table") or []
        if row.get("expiration")
    }
    return sorted(expirations)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_stocknear_mcp_quote.py -v`
Expected: PASS — 7 passed

- [ ] **Step 6: Commit**

```bash
git add app/services/stocknear_mcp.py tests/test_stocknear_mcp_quote.py tests/fixtures/stocknear_mcp/quote_aapl.json
git commit -m "feat: fetch stock quote and expirations from StockNear MCP"
```

---

### Task 4: Rewire the service layer onto MCP

**Files:**
- Modify: `app/services/stocknear_service.py:227-247` (delete three scrape thunks)
- Modify: `app/services/stocknear_service.py:288` (options overview fetch call)
- Modify: `app/services/stocknear_service.py:352` (max pain fetch call)
- Modify: `app/services/stocknear_service.py:383` (stock data fetch call)
- Modify: `app/services/stocknear_service.py:524-527` (`_fetch_expirations_sync`)
- Test: `tests/test_stocknear_service_mcp.py`

**Interfaces:**
- Consumes: `fetch_options_overview`, `fetch_stock_overview`, `fetch_expirations` from Tasks 2-3
- Produces: no signature changes. `get_options_overview`, `get_max_pain`, `get_stock_data` keep their existing `(db, symbol, force_refresh)` signatures and return types.

**Why the cached shapes are preserved:** `get_max_pain` currently caches `asdict(OptionsData)` under `max_pain:{symbol}` and reads only `["max_pain"]` back. Reusing `fetch_options_overview` and calling `asdict` on it keeps that shape byte-identical, so rows written by the scraper stay readable. `get_stock_data` likewise caches `asdict(StockData)`.

- [ ] **Step 1: Write the failing service tests**

Create `tests/test_stocknear_service_mcp.py`:

```python
"""Service-layer tests: the MCP fetchers are wired in and failures degrade
to cached data rather than propagating.

These use an in-memory fake for the cache rather than a real database, in
keeping with tests/conftest.py's note that DB-backed tests belong in an
integration suite.
"""

import pytest

from app.services import stocknear_service
from app.stocknear_models import OptionsData


@pytest.fixture
def fake_cache(monkeypatch):
    """Replace the DB-backed cache helpers with a dict."""
    store: dict[str, dict] = {}

    async def get_cached_data(db, cache_key, include_expired=False):
        return store.get(cache_key)

    async def set_cached_data(db, cache_key, data_type, symbol, data, ttl_seconds=None):
        store[cache_key] = data

    monkeypatch.setattr(stocknear_service, "get_cached_data", get_cached_data)
    monkeypatch.setattr(stocknear_service, "set_cached_data", set_cached_data)
    return store


@pytest.mark.asyncio
async def test_get_options_overview_uses_mcp_fetcher(monkeypatch, fake_cache):
    async def fake_fetch(symbol):
        return OptionsData(symbol=symbol, implied_volatility=0.3365, iv_rank=42.0)

    monkeypatch.setattr(stocknear_service, "fetch_options_overview", fake_fetch)

    result = await stocknear_service.get_options_overview(db=None, symbol="aapl")

    assert result.symbol == "AAPL"
    assert result.implied_volatility == pytest.approx(0.3365)


@pytest.mark.asyncio
async def test_get_options_overview_falls_back_to_cache_on_mcp_failure(
    monkeypatch, fake_cache
):
    fake_cache["options_overview:AAPL"] = {
        "symbol": "AAPL",
        "implied_volatility": 0.30,
        "iv_rank": 40.0,
        "iv_percentile": None,
        "historical_volatility": None,
        "put_call_ratio": None,
        "total_volume": None,
        "total_open_interest": None,
        "max_pain": None,
        "raw_content": "",
    }

    async def boom(symbol):
        raise stocknear_service.StockNearMCPError("server down")

    monkeypatch.setattr(stocknear_service, "fetch_options_overview", boom)

    result = await stocknear_service.get_options_overview(
        db=None, symbol="AAPL", force_refresh=True
    )

    assert result.implied_volatility == 0.30


@pytest.mark.asyncio
async def test_get_options_overview_preserves_cached_value_when_fresh_is_null(
    monkeypatch, fake_cache
):
    """The merge behavior that keeps last-known values across a closed market."""
    fake_cache["options_overview:AAPL"] = {
        "symbol": "AAPL",
        "implied_volatility": 0.30,
        "iv_rank": 40.0,
        "iv_percentile": None,
        "historical_volatility": None,
        "put_call_ratio": None,
        "total_volume": None,
        "total_open_interest": None,
        "max_pain": None,
        "raw_content": "",
    }

    async def fake_fetch(symbol):
        return OptionsData(symbol=symbol, implied_volatility=0.35, iv_rank=None)

    monkeypatch.setattr(stocknear_service, "fetch_options_overview", fake_fetch)

    result = await stocknear_service.get_options_overview(
        db=None, symbol="AAPL", force_refresh=True
    )

    assert result.implied_volatility == 0.35
    assert result.iv_rank == 40.0


@pytest.mark.asyncio
async def test_get_max_pain_returns_none_on_no_data(monkeypatch, fake_cache):
    async def boom(symbol):
        raise stocknear_service.StockNearMCPNoData("unknown symbol")

    monkeypatch.setattr(stocknear_service, "fetch_options_overview", boom)

    assert await stocknear_service.get_max_pain(db=None, symbol="ZZZZ") is None


@pytest.mark.asyncio
async def test_get_max_pain_does_not_cache_on_no_data(monkeypatch, fake_cache):
    """A missing symbol must not write an empty row over good data."""
    async def boom(symbol):
        raise stocknear_service.StockNearMCPNoData("unknown symbol")

    monkeypatch.setattr(stocknear_service, "fetch_options_overview", boom)
    await stocknear_service.get_max_pain(db=None, symbol="ZZZZ")

    assert "max_pain:ZZZZ" not in fake_cache
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_stocknear_service_mcp.py -v`
Expected: FAIL — `AttributeError: module 'app.services.stocknear_service' has no attribute 'fetch_options_overview'`

- [ ] **Step 3: Import the MCP fetchers**

In `app/services/stocknear_service.py`, immediately after the existing import on line 114 (`from app.stocknear import StockNearScraper, ...`), add:

```python
from app.services.stocknear_mcp import (
    StockNearMCPError,
    StockNearMCPNoData,
    fetch_expirations,
    fetch_options_overview,
    fetch_stock_overview,
)
```

- [ ] **Step 4: Delete the three scrape thunks**

Delete lines 227-247 of `app/services/stocknear_service.py` in their entirety — the functions `_fetch_options_overview_sync`, `_fetch_max_pain_sync`, and `_fetch_stock_overview_sync`. They have no other callers.

- [ ] **Step 5: Repoint the options overview fetch**

In `get_options_overview`, replace line 288:

```python
        fresh_dict = await run_scraper(_fetch_options_overview_sync, symbol)
```

with:

```python
        fresh_dict = asdict(await fetch_options_overview(symbol))
```

- [ ] **Step 6: Repoint the max pain fetch**

In `get_max_pain`, replace the body of the `try` block (lines 352-355):

```python
        data_dict = await run_scraper(_fetch_max_pain_sync, symbol)
        await set_cached_data(db, cache_key, "max_pain", symbol, data_dict)
        logger.debug("Max pain for %s: %s", symbol, data_dict.get("max_pain"))
        return data_dict.get("max_pain")
```

with:

```python
        # Max pain rides along on the options-overview payload — one MCP
        # call serves both. asdict keeps the cached shape identical to what
        # the scraper wrote, so pre-existing rows stay readable.
        data_dict = asdict(await fetch_options_overview(symbol))
        await set_cached_data(db, cache_key, "max_pain", symbol, data_dict)
        logger.debug("Max pain for %s: %s", symbol, data_dict.get("max_pain"))
        return data_dict.get("max_pain")
```

- [ ] **Step 7: Repoint the stock data fetch**

In `get_stock_data`, replace line 383:

```python
        data_dict = await run_scraper(_fetch_stock_overview_sync, symbol)
```

with:

```python
        data_dict = asdict(await fetch_stock_overview(symbol))
```

- [ ] **Step 8: Repoint the expirations fetch**

Delete `_fetch_expirations_sync` (lines 524-527). Then find its call site with:

```bash
grep -n '_fetch_expirations_sync' app/services/stocknear_service.py
```

Replace each `await run_scraper(_fetch_expirations_sync, symbol)` with `await fetch_expirations(symbol)`.

- [ ] **Step 9: Run tests to verify they pass**

Run: `uv run pytest tests/test_stocknear_service_mcp.py -v`
Expected: PASS — 5 passed

- [ ] **Step 10: Run the full suite and type check**

Run: `uv run pytest && uv run mypy app`
Expected: all tests pass; mypy reports no new errors relative to the pre-existing baseline.

- [ ] **Step 11: Commit**

```bash
git add app/services/stocknear_service.py tests/test_stocknear_service_mcp.py
git commit -m "feat: serve symbol-level StockNear data from MCP instead of the scraper"
```

---

### Task 5: Remove the superseded scraper methods

**Files:**
- Modify: `app/stocknear.py` (delete methods, rewrite `main()`)
- Modify: `app/routers/debug.py:25-57` (`/api/debug/stocknear/{symbol}`)
- Modify: `AGENTS.md` (Dependencies and File Purposes sections)
- Modify: `README.md` (Project Structure section)

**Interfaces:**
- Consumes: `fetch_options_overview`, `fetch_stock_overview` from Tasks 2-3
- Produces: `StockNearScraper` retains `get_contract_quote`, `get_contract_quotes_batch`, `get_contract_quote_via_api`, `get_contract_quotes_via_api`, `get_available_strikes`, `get_options_chain`, `get_options_chain_parsed`, `navigate`, `get_page_text`, `screenshot`, `start`, `close`, and the context-manager protocol.

- [ ] **Step 1: Confirm nothing still calls the doomed methods**

Run:

Grep for *calls on a scraper instance*, not for the bare names. `get_options_overview` and `get_max_pain` are also the names of service-layer functions in `stocknear_service.py` that are staying — a bare-name grep hits those and `app/main.py`'s imports of them, which is noise, not a problem.

```bash
grep -rn 'scraper\.\(get_options_overview\|get_max_pain\|get_stock_overview\|get_available_expirations\|get_options_flow\|get_dark_pool\|get_analyst_ratings\|get_options_gex\|get_options_dex\)' --include='*.py' app/ tests/
```

Expected: hits only inside `app/stocknear.py`'s `main()` CLI, which Step 3 rewrites. A hit anywhere else means a caller was missed — resolve it before deleting.

- [ ] **Step 2: Delete the superseded scraper methods**

From `app/stocknear.py`, delete these nine methods from the `StockNearScraper` class:

`get_options_overview`, `get_max_pain`, `get_stock_overview`, `get_available_expirations`, `get_options_flow`, `get_dark_pool`, `get_analyst_ratings`, `get_options_gex`, `get_options_dex`.

The last five were only ever reachable from the module's CLI, never from the application.

- [ ] **Step 3: Repoint the CLI at MCP**

Replace `main()` in `app/stocknear.py` (currently lines 1320-1376) with:

```python
def main():
    """CLI interface.

    Symbol-level commands go through the MCP server; only the chain command
    still needs the browser. Run with `python -m app.stocknear <command>`.
    """
    import asyncio

    from app.services.stocknear_mcp import (
        call_tool,
        fetch_options_overview,
        fetch_stock_overview,
    )

    if len(sys.argv) < 3:
        print("Usage: python -m app.stocknear <command> <symbol>")
        print("\nMCP-backed commands:")
        print("  stock <symbol>             - Get stock overview")
        print("  options-overview <symbol>  - Get options overview (IV, OI, volume)")
        print("  max-pain <symbol>          - Get max pain analysis")
        print("  ratings <symbol>           - Get analyst ratings")
        print("  flow <symbol>              - Get options flow (unusual orders)")
        print("\nScraper-backed commands:")
        print("  options-chain <symbol> [expiration] - Get options chain with Greeks")
        sys.exit(1)

    command = sys.argv[1].lower()
    symbol = sys.argv[2]

    if command == "options-chain":
        expiration = sys.argv[3] if len(sys.argv) > 3 else None
        with StockNearScraper() as scraper:
            result = scraper.get_options_chain(symbol, expiration)
    elif command == "stock":
        data = asyncio.run(fetch_stock_overview(symbol))
        result = {"symbol": data.symbol, "price": data.price, "change": data.change}
    elif command == "options-overview":
        data = asyncio.run(fetch_options_overview(symbol))
        result = {
            "symbol": data.symbol,
            "iv_rank": data.iv_rank,
            "iv_percentile": data.iv_percentile,
            "implied_volatility": data.implied_volatility,
            "put_call_ratio": data.put_call_ratio,
            "total_volume": data.total_volume,
            "total_open_interest": data.total_open_interest,
        }
    elif command == "max-pain":
        data = asyncio.run(fetch_options_overview(symbol))
        result = {"symbol": data.symbol, "max_pain": data.max_pain}
    elif command == "ratings":
        result = asyncio.run(
            call_tool("get_ticker_analyst_rating", {"tickers": [symbol.upper()]})
        )
    elif command == "flow":
        result = asyncio.run(
            call_tool("get_ticker_unusual_activity", {"tickers": [symbol.upper()]})
        )
    else:
        print(f"Unknown command or missing arguments: {command}")
        sys.exit(1)

    print(json.dumps(result, indent=2, default=str))
```

Note the `gex` and `dex` commands are gone: the MCP server has no gamma- or delta-exposure tool, and the scraping for them was CLI-only.

- [ ] **Step 4: Update the module `__all__`**

`app/stocknear.py` line 40 lists exported names. Leave the dataclass re-exports (`OptionContract`, `OptionsChain`, `ContractQuote`, `OptionsData`, `StockData`, `StockNearScraper`, `extract_browser_cookies`) intact — `stocknear_service.py` imports `OptionsData` and `StockData` from here, and those are re-exports from `stocknear_models`, not scraper methods. No change needed unless the grep in Step 1 showed otherwise.

- [ ] **Step 5: Repoint the debug endpoint**

Replace the body of `debug_stocknear` in `app/routers/debug.py` (lines 25-57) with:

```python
@router.get("/stocknear/{symbol}")
async def debug_stocknear(symbol: str, db: AsyncSession = Depends(get_db)):
    """
    Debug endpoint to view the raw MCP payloads behind a symbol's data.

    Symbol-level data now comes from the MCP server rather than scraped
    pages, so this dumps the decoded tool payloads instead of page text.
    """
    from app.services.stocknear_mcp import StockNearMCPError, call_tool

    out: dict = {"symbol": symbol.upper()}
    for label, tool in (
        ("options_overview", "get_ticker_options_overview_data"),
        ("quote", "get_ticker_quote"),
    ):
        try:
            out[label] = await call_tool(tool, {"tickers": [symbol.upper()]})
        except StockNearMCPError as exc:
            out[label] = {"error": str(exc)}
    return out
```

- [ ] **Step 6: Update the documentation**

In `AGENTS.md`, change the Dependencies entry:

```
- `playwright` - StockNear scraper (Firefox, persistent context)
```

to:

```
- `playwright` - StockNear contract-quote scraper (Firefox, persistent context).
  Symbol-level data comes from the StockNear MCP server over httpx instead —
  see `app/services/stocknear_mcp.py`. The MCP server has no bid/ask or
  greeks for an arbitrary strike, which is why the scraper still exists.
```

In the same file's File Purposes section, add:

```
- `app/services/stocknear_mcp.py` - StockNear MCP client: options overview,
  max pain, stock quote, expirations
```

In `README.md`, add to the Project Structure tree under `services/`:

```
│       ├── stocknear_mcp.py # StockNear MCP client (symbol-level data)
```

- [ ] **Step 7: Run the full suite and type check**

Run: `uv run pytest && uv run mypy app`
Expected: all tests pass; mypy reports no new errors.

- [ ] **Step 8: Verify the app imports and the CLI works end to end**

Run:

```bash
uv run python -c "import app.main"
uv run python -m app.stocknear options-overview AAPL
```

Expected: the import is silent; the CLI prints a JSON object whose `implied_volatility` is a decimal below 1.0 (not a percentage above 1.0). This is the end-to-end check that the unit conversion survived the wiring.

- [ ] **Step 9: Commit**

```bash
git add app/stocknear.py app/routers/debug.py AGENTS.md README.md
git commit -m "refactor: drop scraper methods superseded by the MCP client"
```

---

## Self-Review

**Spec coverage:** Transport and its Cloudflare/statelessness constraints — Task 1. Configuration and credential handling — Task 1. Field mapping including the percent-to-decimal conversion — Task 2. Stock quote and expirations mapping — Task 3. Service rewiring with preserved cache keys and fallback — Task 4. Removals, CLI repoint, debug endpoint, documentation — Task 5. Error-handling table — split across Tasks 1 (transport errors, `isError`) and 2-3 (`StockNearMCPNoData`, null fields). Testing section — every listed case has a named test. Retained-Playwright section — asserted by Task 5 Step 1's grep and the Produces block.

**Type consistency:** `StockNearMCPError` / `StockNearMCPNoData` / `call_tool` / `_make_client` / `USER_AGENT` defined in Task 1 and used under those names in Tasks 2-5. `_require_symbol` defined in Task 2, reused in Task 3. `_pct_to_decimal` and `_select_max_pain` defined and tested in Task 2 only. `fetch_options_overview` / `fetch_stock_overview` / `fetch_expirations` defined in Tasks 2-3, consumed in Tasks 4-5 with matching signatures. `OptionsData` and `StockData` field names match `app/stocknear_models.py:159-183`.

**Placeholder scan:** no TBD, TODO, "handle edge cases", or "similar to Task N" references. Every code step carries the literal code to write; every test step carries the literal test.

**One spec revision made while planning:** the spec originally named `fetch_max_pain` as a separate fetcher. It was dropped and the spec updated to match — `max_pain` is already a field on the `OptionsData` that `fetch_options_overview` returns, and `get_max_pain` in the service layer caches the full `asdict(OptionsData)`. A separate fetcher would duplicate the same MCP call for one field it already has. Max pain selection still gets its own tested function, `_select_max_pain`.
