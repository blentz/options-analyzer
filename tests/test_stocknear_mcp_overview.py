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
    _plausible_iv_rank,
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


@pytest.mark.asyncio
async def test_fetch_options_overview_selects_max_pain_from_table(monkeypatch):
    """Pins the table->_select_max_pain wiring, including the zero-skip.

    Uses far-future expiries rather than the AAPL fixture's real dates so
    the assertion cannot start failing once those expiries pass.
    """
    _stub_call_tool(monkeypatch, {"AAPL": {"table": [
        {"expiration": "2099-01-15", "maxPain": 0},
        {"expiration": "2099-02-19", "maxPain": 250.0},
    ]}})

    data = await fetch_options_overview("AAPL")

    assert data.max_pain == 250.0


# --- IV Rank plausibility ------------------------------------------------
#
# StockNear computes ivRank as (current - ivLow) / (ivHigh - ivLow) * 100.
# Verified against live data on 2026-08-15: GME returned ivRank 17.66 with
# current 73.11, ivLow 54.83, ivHigh 158.33, which reproduces exactly.
#
# The problem is that ivHigh is sometimes corrupt upstream — the same probe
# saw 13482% (NVDA), 21947% (MSTR), 54297% (F) and 358042% (AMC). A corrupt
# ivHigh does not make ivRank null; it makes it a plausible-looking near-zero.
# Ford came back with ivRank 0.03 next to ivPercentile 73.83: the rank says
# "IV at the bottom of its range", the percentile says "higher than 74% of
# the year". Rendering 0.03 as a live IV Rank is worse than rendering nothing.


def test_iv_rank_is_kept_when_iv_high_is_plausible():
    assert _plausible_iv_rank(17.66, iv_high=158.33) == 17.66


def test_iv_rank_is_dropped_when_iv_high_is_absurd():
    """Ford's real 2026-08-15 payload: ivHigh of 54297% is not a real quote."""
    assert _plausible_iv_rank(0.03, iv_high=54297.3) is None


def test_iv_rank_is_kept_when_iv_high_is_missing():
    """No ivHigh to judge by means no grounds to reject the server's rank."""
    assert _plausible_iv_rank(42.0, iv_high=None) == 42.0


def test_iv_rank_none_stays_none():
    assert _plausible_iv_rank(None, iv_high=158.33) is None


def test_iv_rank_zero_survives_a_plausible_iv_high():
    """0.0 is a legitimate rank when the range it came from is sane."""
    assert _plausible_iv_rank(0.0, iv_high=158.33) == 0.0


@pytest.mark.asyncio
async def test_fetch_options_overview_drops_iv_rank_from_corrupt_iv_high(monkeypatch):
    """End-to-end: the guard is actually wired into the mapping."""
    _stub_call_tool(monkeypatch, {"F": {"impliedVolatility": {
        "current": 48.83, "ivRank": 0.03, "ivPercentile": 73.83,
        "ivLow": 35.16, "ivHigh": 54297.3,
    }}})

    data = await fetch_options_overview("F")

    assert data.iv_rank is None
    assert data.iv_percentile == 73.83, "percentile is independent and must survive"
