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
from datetime import date
from typing import Any

import httpx

from app.config import settings
from app.stocknear_models import OptionsData, StockData

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
