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
