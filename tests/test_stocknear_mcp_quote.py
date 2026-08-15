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
async def test_fetch_stock_overview_keeps_missing_market_cap_as_none(monkeypatch):
    """Absent marketCap must stay None, never the string "None"."""
    _stub_call_tool(monkeypatch, {"AAPL": {"symbol": "AAPL", "price": 305.93}})

    data = await fetch_stock_overview("AAPL")

    assert data.market_cap is None


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


@pytest.mark.asyncio
async def test_fetch_expirations_filters_past_expiries(monkeypatch):
    """The server is not trusted to have already dropped past expiries —
    same defensive assumption as _select_max_pain. Far-past/far-future
    dates so this test cannot rot with the passage of time.
    """
    _stub_call_tool(monkeypatch, {"AAPL": {"table": [
        {"expiration": "2020-01-17"},
        {"expiration": "2099-01-15"},
    ]}})

    assert await fetch_expirations("AAPL") == ["2099-01-15"]


@pytest.mark.asyncio
async def test_fetch_stock_overview_stores_raw_payload(monkeypatch):
    """raw_content backs the debug endpoint, so it must round-trip as JSON."""
    payload = _load("quote_aapl.json")
    _stub_call_tool(monkeypatch, payload)

    data = await fetch_stock_overview("AAPL")

    assert json.loads(data.raw_content) == payload["AAPL"]
