"""Service-layer tests: the MCP fetchers are wired in and failures degrade
to cached data rather than propagating.

These use an in-memory fake for the cache rather than a real database, in
keeping with tests/conftest.py's note that DB-backed tests belong in an
integration suite.
"""

import pytest

from app.services import stocknear_service
from app.services.stocknear_mcp import StockNearMCPError, StockNearMCPNoData
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
        raise StockNearMCPError("server down")

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
        raise StockNearMCPNoData("unknown symbol")

    monkeypatch.setattr(stocknear_service, "fetch_options_overview", boom)

    assert await stocknear_service.get_max_pain(db=None, symbol="ZZZZ") is None


@pytest.mark.asyncio
async def test_get_max_pain_does_not_cache_on_no_data(monkeypatch, fake_cache):
    """A missing symbol must not write an empty row over good data."""
    async def boom(symbol):
        raise StockNearMCPNoData("unknown symbol")

    monkeypatch.setattr(stocknear_service, "fetch_options_overview", boom)
    await stocknear_service.get_max_pain(db=None, symbol="ZZZZ")

    assert "max_pain:ZZZZ" not in fake_cache
