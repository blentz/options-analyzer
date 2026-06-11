"""Tests for the Reality Gap valuation orchestration.

The two data dependencies (StockNear fundamentals, BLS CPI) are stubbed so we
test the assembly + serialization in isolation — no network, no DB.
"""

import pytest

import app.services.valuations as valuations
from app.stocknear_models import Fundamentals


def _stub(monkeypatch, fundamentals, cpi=None):
    async def fake_fundamentals(db, symbol, force_refresh=False):
        return fundamentals

    async def fake_cpi(db, years, force_refresh=False):
        return cpi or {}

    monkeypatch.setattr(valuations, "get_symbol_fundamentals", fake_fundamentals)
    monkeypatch.setattr(valuations, "get_cpi_by_year", fake_cpi)


@pytest.mark.asyncio
async def test_ok_result_reconciles(monkeypatch):
    f = Fundamentals(
        symbol="TEST",
        market_cap=3000.0,
        book_equity=200.0,
        goodwill=0.0,
        intangibles=0.0,
        net_income_by_year={y: 10.0 for y in range(2015, 2025)},  # 10 years, G=10
        balance_sheet_year=2024,
    )
    _stub(monkeypatch, f)
    out = await valuations.get_valuation(None, "test")

    assert out["status"] == "ok"
    assert out["tangible_equity"] == 200.0
    # RG10: FB = 200 + 10*10 = 300 → 3000/300 = 10.0
    rg10 = next(r for r in out["results"] if r["n"] == 10)
    assert rg10["fundamental_base"] == 300.0
    assert rg10["value"] == pytest.approx(10.0)
    assert out["short_window"] is False
    # Larger N ⇒ larger base ⇒ smaller RG.
    rg8 = next(r for r in out["results"] if r["n"] == 8)
    rg12 = next(r for r in out["results"] if r["n"] == 12)
    assert rg8["value"] > rg10["value"] > rg12["value"]


@pytest.mark.asyncio
async def test_short_window_flagged(monkeypatch):
    f = Fundamentals(
        symbol="SHORT", market_cap=1000.0, book_equity=100.0, goodwill=0.0,
        intangibles=0.0, net_income_by_year={2023: 10.0, 2024: 10.0}, balance_sheet_year=2024,
    )
    _stub(monkeypatch, f)
    out = await valuations.get_valuation(None, "short")
    assert out["short_window"] is True
    assert out["results"][0]["formatted"].startswith("RG8*")


@pytest.mark.asyncio
async def test_not_covered_serializes_as_none(monkeypatch):
    # Negative tangible equity + negative earnings ⇒ RG = ∞ (paper §3.5 Case B).
    f = Fundamentals(
        symbol="UNCOV", market_cap=5000.0, book_equity=10.0, goodwill=40.0,
        intangibles=30.0, net_income_by_year={y: -5.0 for y in range(2018, 2025)},
        balance_sheet_year=2024,
    )
    _stub(monkeypatch, f)
    out = await valuations.get_valuation(None, "uncov")
    assert out["status"] == "ok"
    rg10 = next(r for r in out["results"] if r["n"] == 10)
    assert rg10["covered"] is False
    assert rg10["value"] is None
    assert "∞" in rg10["formatted"]


@pytest.mark.asyncio
async def test_insufficient_data_all_missing_hints_block(monkeypatch):
    # Nothing came back at all → message points at the likely Cloudflare block.
    f = Fundamentals(symbol="NODATA", market_cap=None, book_equity=None, net_income_by_year={})
    _stub(monkeypatch, f)
    out = await valuations.get_valuation(None, "nodata")
    assert out["status"] == "insufficient_data"
    assert "Cloudflare" in out["message"]


@pytest.mark.asyncio
async def test_insufficient_data_partial_lists_missing(monkeypatch):
    # Some inputs present → list exactly what's missing.
    f = Fundamentals(symbol="PART", market_cap=1000.0, book_equity=100.0, net_income_by_year={})
    _stub(monkeypatch, f)
    out = await valuations.get_valuation(None, "part")
    assert out["status"] == "insufficient_data"
    assert "net income history" in out["message"]


@pytest.mark.asyncio
async def test_error_when_fetch_returns_none(monkeypatch):
    _stub(monkeypatch, None)
    out = await valuations.get_valuation(None, "boom")
    assert out["status"] == "error"


@pytest.mark.asyncio
async def test_inflation_adjustment_applied(monkeypatch):
    f = Fundamentals(
        symbol="INFL", market_cap=1000.0, book_equity=200.0, goodwill=0.0, intangibles=0.0,
        net_income_by_year={2015: 100.0, 2024: 100.0}, balance_sheet_year=2024,
    )
    _stub(monkeypatch, f, cpi={2015: 80.0, 2024: 120.0})
    out = await valuations.get_valuation(None, "infl")
    assert out["inflation_adjusted"] is True
    # 2015 income inflates to 150 in 2024 dollars → G = 125, not 100.
    assert out["smoothed_earnings"] == pytest.approx(125.0)
