"""Transport-layer tests for the StockNear MCP client.

All HTTP is mocked through httpx.MockTransport — these tests never touch
the network. The client factory `_make_client` is the monkeypatch seam.
"""

import asyncio
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


@pytest.mark.asyncio
async def test_top_level_json_list_raises_mcp_error(monkeypatch):
    """A valid-JSON-but-wrong-shape response (list instead of object) must
    surface as StockNearMCPError, not an AttributeError from body.get(...)
    on a list — the router only catches StockNearMCPError.
    """
    def handler(request):
        return httpx.Response(200, json=[1, 2, 3])

    _install_transport(monkeypatch, handler)
    with pytest.raises(StockNearMCPError):
        await call_tool("get_ticker_quote", {"tickers": ["AAPL"]})


@pytest.mark.asyncio
async def test_content_as_string_raises_mcp_error(monkeypatch):
    """result.content being a bare string (instead of a list of blocks)
    would otherwise iterate its characters and fail deep inside with an
    AttributeError on block.get(...). Must surface as StockNearMCPError.
    """
    def handler(request):
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "result": {"content": "not a list", "isError": False},
            },
        )

    _install_transport(monkeypatch, handler)
    with pytest.raises(StockNearMCPError):
        await call_tool("get_ticker_quote", {"tickers": ["AAPL"]})


# --- Concurrency bound ---------------------------------------------------
#
# The scraper path this replaced funnelled through a semaphore of 1, so the
# app could never hammer StockNear. Sub-second MCP calls do not need a bound
# that tight, but they do need one: the spec records StockNear's rate limits
# as undocumented, and N concurrent requests would otherwise mean N
# concurrent MCP calls with nothing in the way.


class _DepthTrackingTransport(httpx.AsyncBaseTransport):
    """Records how many requests are ever in flight at the same moment."""

    def __init__(self):
        self.in_flight = 0
        self.max_in_flight = 0

    async def handle_async_request(self, request):
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        await asyncio.sleep(0.01)   # hold the slot so overlap is observable
        self.in_flight -= 1
        return _tool_response({"ok": True})


@pytest.mark.asyncio
async def test_concurrent_calls_are_capped(monkeypatch):
    transport = _DepthTrackingTransport()
    monkeypatch.setattr(
        stocknear_mcp, "_make_client",
        lambda: httpx.AsyncClient(transport=transport),
    )
    monkeypatch.setattr(stocknear_mcp, "_mcp_semaphore", asyncio.Semaphore(2))

    await asyncio.gather(*(
        call_tool("get_ticker_quote", {"tickers": [f"S{i}"]}) for i in range(8)
    ))

    assert transport.max_in_flight <= 2, (
        f"{transport.max_in_flight} concurrent MCP calls escaped the cap of 2"
    )
    assert transport.max_in_flight > 1, "test did not actually exercise concurrency"
