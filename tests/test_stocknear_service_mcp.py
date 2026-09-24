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
async def test_no_data_does_not_write_a_cache_row(monkeypatch, fake_cache):
    """A missing symbol must not write an empty row over good data."""
    async def boom(symbol):
        raise StockNearMCPNoData("unknown symbol")

    monkeypatch.setattr(stocknear_service, "fetch_options_overview", boom)
    result = await stocknear_service.get_options_overview(db=None, symbol="ZZZZ")

    assert result is None
    assert fake_cache == {}


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


@pytest.mark.asyncio
async def test_get_options_chain_uses_mcp_and_caches(monkeypatch, fake_cache):
    from app.stocknear_models import OptionContract, OptionsChain

    async def fake_chain(symbol):
        return OptionsChain(
            symbol=symbol, expirations=["2026-10-16"], implied_volatility=1.16,
            contracts=[OptionContract(strike=2.5, option_type="PUT", expiration="2026-10-16", open_interest=3852)],
        )

    monkeypatch.setattr(stocknear_service, "fetch_options_chain", fake_chain)

    chain = await stocknear_service.get_options_chain(db=None, symbol="hiti")

    assert chain.expirations == ["2026-10-16"]
    assert chain.get_contract("2026-10-16", 2.5, "PUT").open_interest == 3852
    assert fake_cache["options_chain:HITI"]["implied_volatility"] == 1.16


@pytest.mark.asyncio
async def test_failed_contract_quote_is_not_cached(monkeypatch, fake_cache):
    from app.services.stocknear_contract_api import StockNearAPIError

    async def boom(*args):
        raise StockNearAPIError("403")

    monkeypatch.setattr(stocknear_service, "fetch_contract_quote", boom)

    assert await stocknear_service.get_contract_quote(None, "HITI", "Oct 16, 2026", 2.5, "PUT") is None
    assert not any(k.startswith("contract_quote:") for k in fake_cache)


@pytest.mark.asyncio
async def test_batch_keeps_a_slot_for_failed_contracts(monkeypatch, fake_cache):
    from app.stocknear_models import ContractQuote

    async def fake_quotes(contracts):
        return [None, ContractQuote(symbol="HITI", strike=2.5, option_type="PUT",
                                    expiration="2026-10-16", contract_id="HITI261016P00002500", mid=0.1)]

    monkeypatch.setattr(stocknear_service, "fetch_contract_quotes", fake_quotes)
    contracts = [
        {"symbol": "HITI", "expiration": "2026-10-16", "strike": 5.0, "option_type": "CALL"},
        {"symbol": "HITI", "expiration": "2026-10-16", "strike": 2.5, "option_type": "PUT"},
    ]

    quotes = await stocknear_service.get_contract_quotes_batch(None, contracts)

    assert quotes[0] is None and quotes[1].contract_id == "HITI261016P00002500"
    assert list(k for k in fake_cache if k.startswith("contract_quote:")) == ["contract_quote:HITI:2026-10-16:2.5:PUT"]
