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
