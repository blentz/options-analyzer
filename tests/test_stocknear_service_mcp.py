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
    """Replace the DB-backed cache helpers with a dict.

    Entries live in `store` and are fresh by default. Marking a key expired
    via `store.expire(key)` makes it visible only to `include_expired=True`
    reads, which is the distinction the merge logic exists to serve — a fake
    that returned every row to both kinds of read would let a fresh-vs-stale
    test pass without proving anything.
    """
    class _Store(dict):
        def __init__(self):
            super().__init__()
            self.expired: set[str] = set()

        def expire(self, key: str) -> None:
            self.expired.add(key)

    store = _Store()

    async def get_cached_data(db, cache_key, include_expired=False):
        if cache_key in store.expired and not include_expired:
            return None
        return store.get(cache_key)

    async def set_cached_data(db, cache_key, data_type, symbol, data, ttl_seconds=None):
        store[cache_key] = data
        store.expired.discard(cache_key)

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
    """The merge behavior that keeps last-known values across a closed market.

    iv_rank is deliberately NOT used as the example field here — it is
    exempted from this preserve behavior (see the exemption test below)
    because the MCP server returns it null most of the time, which would
    otherwise pin a pre-migration scraped value in place forever.
    """
    fake_cache["options_overview:AAPL"] = {
        "symbol": "AAPL",
        "implied_volatility": 0.30,
        "iv_rank": None,
        "iv_percentile": None,
        "historical_volatility": None,
        "put_call_ratio": 0.85,
        "total_volume": None,
        "total_open_interest": None,
        "max_pain": None,
        "raw_content": "",
    }

    async def fake_fetch(symbol):
        return OptionsData(symbol=symbol, implied_volatility=0.35, put_call_ratio=None)

    monkeypatch.setattr(stocknear_service, "fetch_options_overview", fake_fetch)

    result = await stocknear_service.get_options_overview(
        db=None, symbol="AAPL", force_refresh=True
    )

    assert result.implied_volatility == 0.35
    assert result.put_call_ratio == 0.85


@pytest.mark.asyncio
async def test_get_options_overview_clears_iv_rank_when_fresh_is_null(
    monkeypatch, fake_cache
):
    """iv_rank is exempted from the null-preserve merge rule: a null fresh
    value must clear it rather than freezing a pre-migration scraped value
    in place forever with its TTL reset on every write. A blank IV Rank is
    honest; a frozen one is not.
    """
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

    assert result.iv_rank is None


@pytest.mark.asyncio
async def test_get_max_pain_returns_none_on_no_data(monkeypatch, fake_cache):
    async def boom(symbol):
        raise StockNearMCPNoData("unknown symbol")

    monkeypatch.setattr(stocknear_service, "fetch_options_overview", boom)

    assert await stocknear_service.get_max_pain(db=None, symbol="ZZZZ") is None


@pytest.mark.asyncio
async def test_no_data_does_not_write_a_cache_row(monkeypatch, fake_cache):
    """A missing symbol must not write an empty row over good data.

    Asserts on the options_overview key, which is the one actually written
    now that max pain shares it — asserting `max_pain:ZZZZ` is absent would
    pass whatever the code did, since nothing writes that key any more.
    """
    async def boom(symbol):
        raise StockNearMCPNoData("unknown symbol")

    monkeypatch.setattr(stocknear_service, "fetch_options_overview", boom)
    await stocknear_service.get_max_pain(db=None, symbol="ZZZZ")

    assert "options_overview:ZZZZ" not in fake_cache
    assert fake_cache == {}


# --- One payload, one fetch ---------------------------------------------
#
# Under the scraper, max pain and the options overview genuinely came from
# two different pages, so two fetches and two cache keys were right. The MCP
# server serves both from one get_ticker_options_overview_data payload, so a
# second fetch is now pure duplication — double latency on a cold cache, the
# full raw_content stored twice, and two keys with independent TTLs that can
# drift into disagreeing about the same underlying snapshot.


@pytest.mark.asyncio
async def test_get_max_pain_does_not_refetch_when_overview_is_cached(
    monkeypatch, fake_cache
):
    calls = []

    async def counting_fetch(symbol):
        calls.append(symbol)
        return OptionsData(symbol=symbol, max_pain=250.0, implied_volatility=0.31)

    monkeypatch.setattr(stocknear_service, "fetch_options_overview", counting_fetch)

    first = await stocknear_service.get_options_overview(db=None, symbol="AAPL")
    second = await stocknear_service.get_max_pain(db=None, symbol="AAPL")

    assert first.max_pain == 250.0
    assert second == 250.0
    assert calls == ["AAPL"], f"expected one upstream fetch, got {len(calls)}"


@pytest.mark.asyncio
async def test_get_max_pain_shares_the_overview_cache_key(monkeypatch, fake_cache):
    """No second cache key means no second copy of raw_content, and no drift."""
    async def fake_fetch(symbol):
        return OptionsData(symbol=symbol, max_pain=250.0, raw_content='{"big": "payload"}')

    monkeypatch.setattr(stocknear_service, "fetch_options_overview", fake_fetch)

    await stocknear_service.get_max_pain(db=None, symbol="AAPL")

    assert "options_overview:AAPL" in fake_cache
    assert "max_pain:AAPL" not in fake_cache


@pytest.mark.asyncio
async def test_get_enriched_quote_takes_max_pain_from_the_same_payload(
    monkeypatch, fake_cache
):
    calls = []

    async def counting_fetch(symbol):
        calls.append(symbol)
        return OptionsData(symbol=symbol, max_pain=250.0, implied_volatility=0.31)

    monkeypatch.setattr(stocknear_service, "fetch_options_overview", counting_fetch)

    quote = await stocknear_service.get_enriched_quote(db=None, symbol="AAPL")

    assert quote.max_pain == 250.0
    assert quote.implied_volatility == pytest.approx(0.31)
    assert calls == ["AAPL"], f"expected one upstream fetch, got {len(calls)}"


@pytest.mark.asyncio
async def test_expired_cache_triggers_a_refetch_rather_than_being_served(
    monkeypatch, fake_cache
):
    """The fresh-vs-expired distinction, which the old fake could not express."""
    fake_cache["options_overview:AAPL"] = {
        "symbol": "AAPL", "implied_volatility": 0.30, "iv_rank": None,
        "iv_percentile": None, "historical_volatility": None,
        "put_call_ratio": None, "total_volume": None,
        "total_open_interest": None, "max_pain": None, "raw_content": "",
    }
    fake_cache.expire("options_overview:AAPL")

    calls = []

    async def counting_fetch(symbol):
        calls.append(symbol)
        return OptionsData(symbol=symbol, implied_volatility=0.42)

    monkeypatch.setattr(stocknear_service, "fetch_options_overview", counting_fetch)

    result = await stocknear_service.get_options_overview(db=None, symbol="AAPL")

    assert calls == ["AAPL"], "expired cache must not be served as fresh"
    assert result.implied_volatility == pytest.approx(0.42)


@pytest.mark.asyncio
async def test_fresh_cache_is_served_without_a_refetch(monkeypatch, fake_cache):
    """The other side of the same distinction."""
    fake_cache["options_overview:AAPL"] = {
        "symbol": "AAPL", "implied_volatility": 0.30, "iv_rank": None,
        "iv_percentile": None, "historical_volatility": None,
        "put_call_ratio": None, "total_volume": None,
        "total_open_interest": None, "max_pain": None, "raw_content": "",
    }

    calls = []

    async def counting_fetch(symbol):
        calls.append(symbol)
        return OptionsData(symbol=symbol, implied_volatility=0.42)

    monkeypatch.setattr(stocknear_service, "fetch_options_overview", counting_fetch)

    result = await stocknear_service.get_options_overview(db=None, symbol="AAPL")

    assert calls == [], "fresh cache must not trigger an upstream call"
    assert result.implied_volatility == pytest.approx(0.30)
