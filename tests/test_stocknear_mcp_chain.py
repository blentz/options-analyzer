"""Mapping tests for the MCP-backed options chain and strike list.

These replace the Playwright scrape of /stocks/<sym>/options, which began
returning an empty page (2026-09-24) and silently emptied the speculation
page. Fixtures are trimmed live HITI payloads from that date.
"""

import json
from datetime import date
from pathlib import Path

import pytest

from app.services import stocknear_mcp
from app.services.stocknear_mcp import (
    StockNearMCPNoData,
    fetch_options_chain,
    fetch_strikes,
)

FIXTURES = Path(__file__).parent / "fixtures" / "stocknear_mcp"
TOOLS = {
    "get_ticker_options_overview_data": "options_overview_hiti.json",
    "get_ticker_open_interest_by_strike_and_expiry": "open_interest_hiti.json",
}


@pytest.fixture
def frozen_today(monkeypatch):
    class _FixedDate(date):
        @classmethod
        def today(cls):
            return date(2026, 9, 24)

    monkeypatch.setattr(stocknear_mcp, "date", _FixedDate)


@pytest.fixture
def stub_tools(monkeypatch):
    calls = []

    async def fake(name, arguments):
        calls.append(name)
        return json.loads((FIXTURES / TOOLS[name]).read_text())

    monkeypatch.setattr(stocknear_mcp, "call_tool", fake)
    return calls


@pytest.mark.asyncio
async def test_chain_carries_symbol_level_fields(stub_tools, frozen_today):
    chain = await fetch_options_chain("hiti")

    assert chain.symbol == "HITI"
    assert chain.implied_volatility == pytest.approx(1.1625)
    assert chain.iv_rank == pytest.approx(74.81)
    assert chain.iv_percentile == pytest.approx(97.32)
    assert chain.put_call_ratio == pytest.approx(24.0)
    assert chain.total_open_interest == 28550
    assert chain.max_pain == pytest.approx(2.5)
    assert chain.expirations == ["2026-10-16", "2026-11-20", "2027-01-15", "2027-04-16"]


@pytest.mark.asyncio
async def test_chain_contracts_come_from_open_interest(stub_tools, frozen_today):
    chain = await fetch_options_chain("HITI")

    put = chain.get_contract("2026-10-16", 2.5, "PUT")
    assert put is not None and put.open_interest == 3852
    # MCP has no quotes; a contract must never pretend to have a price.
    assert put.bid is None and put.ask is None and put.mid_price is None
    # Zero-OI sides are not listed contracts.
    assert chain.get_contract("2026-10-16", 5.0, "PUT") is None
    assert chain.get_strikes_for_expiration("2026-10-16") == [2.5, 5.0, 7.5]


@pytest.mark.asyncio
async def test_chain_drops_past_expiries(stub_tools, monkeypatch):
    class _Later(date):
        @classmethod
        def today(cls):
            return date(2026, 10, 20)

    monkeypatch.setattr(stocknear_mcp, "date", _Later)
    chain = await fetch_options_chain("HITI")
    assert "2026-10-16" not in chain.expirations
    assert all(c.expiration != "2026-10-16" for c in chain.contracts)


@pytest.mark.asyncio
async def test_strikes(stub_tools, frozen_today):
    result = await fetch_strikes("HITI")
    assert result == {
        "strikes": [2.5, 5.0, 7.5],
        "expirations": ["2026-10-16", "2026-11-20", "2027-01-15", "2027-04-16"],
    }
    assert stub_tools == ["get_ticker_open_interest_by_strike_and_expiry"]


@pytest.mark.asyncio
async def test_unknown_symbol_raises_no_data(monkeypatch):
    async def fake(name, arguments):
        return {}

    monkeypatch.setattr(stocknear_mcp, "call_tool", fake)
    with pytest.raises(StockNearMCPNoData):
        await fetch_options_chain("ZZZZ")
    with pytest.raises(StockNearMCPNoData):
        await fetch_strikes("ZZZZ")
