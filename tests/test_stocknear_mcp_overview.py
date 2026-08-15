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
