"""Tests for the browser-free contract quote client.

Contract quotes used to be fetched through a running Playwright browser
whose only job was holding cookies. The client now reads the cookies from
the profile directly and posts with httpx; these tests pin the request
shape and the response parsing without touching the network.
"""

import json

import httpx
import pytest

from app.services import stocknear_contract_api as api
from app.services.stocknear_contract_api import (
    StockNearAPIError,
    build_contract_id,
    fetch_contract_quote,
    fetch_contract_quotes,
    parse_quote,
)

HISTORY = {"history": [
    {"close_bid": 0.04, "close_ask": 0.2, "close": 0.12},
    {"close_bid": 0.05, "close_ask": 0.15, "close": 0.1, "open": 0, "volume": 0,
     "open_interest": 3852, "implied_volatility": 0.73437, "delta": -0.28674,
     "gamma": 0.67904, "theta": -0.00361, "vega": 0.002325},
]}


@pytest.fixture
def transport(monkeypatch):
    """Install a MockTransport; `responses` maps contract id -> (status, body)."""
    seen = []
    responses = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append((request, body))
        status, payload = responses.get(body["contract"], (200, {"history": []}))
        return httpx.Response(status, json=payload)

    monkeypatch.setattr(api, "_make_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(api, "_load_cookies", lambda: {"pb_auth": "tok", "cf_clearance": "cf"})
    return seen, responses


class TestBuildContractId:
    @pytest.mark.parametrize("expiration", ["2026-10-16", "Oct 16, 2026", "October 16, 2026", "10/16/2026"])
    def test_formats(self, expiration):
        assert build_contract_id("hiti", expiration, "PUT", 2.5) == "HITI261016P00002500"

    def test_float_strike_does_not_truncate(self):
        assert build_contract_id("X", "2026-10-16", "CALL", 2.55).endswith("C00002550")

    def test_bad_date(self):
        with pytest.raises(ValueError):
            build_contract_id("X", "16.10.2026", "CALL", 5)


class TestParseQuote:
    def test_latest_history_row_wins(self):
        q = parse_quote(HISTORY, "HITI", "Oct 16, 2026", 2.5, "put")
        assert (q.bid, q.ask, q.mid, q.last) == (0.05, 0.15, pytest.approx(0.10), 0.1)
        assert q.open_interest == 3852 and q.implied_volatility == pytest.approx(0.73437)
        assert q.contract_id == "HITI261016P00002500" and q.option_type == "PUT"

    def test_mid_falls_back_to_last(self):
        q = parse_quote({"history": [{"close": 0.3}]}, "X", "2026-10-16", 5, "CALL")
        assert q.mid == 0.3

    def test_bare_list_response(self):
        q = parse_quote([{"close_bid": 1.0, "close_ask": 1.2}], "X", "2026-10-16", 5, "CALL")
        assert q.mid == pytest.approx(1.1)

    def test_empty_history_is_blank_quote(self):
        q = parse_quote({"history": []}, "X", "2026-10-16", 5, "CALL")
        assert q.bid is None and q.mid is None


@pytest.mark.asyncio
async def test_fetch_sends_uppercase_ids_and_cookies(transport):
    seen, responses = transport
    responses["HITI261016P00002500"] = (200, HISTORY)

    q = await fetch_contract_quote("hiti", "Oct 16, 2026", 2.5, "put")

    request, body = seen[0]
    assert body == {"ticker": "HITI", "contract": "HITI261016P00002500"}
    assert "pb_auth=tok" in request.headers["cookie"]
    assert request.headers["user-agent"].startswith("Mozilla/")
    assert q.mid == pytest.approx(0.10)


@pytest.mark.asyncio
async def test_http_error_raises(transport):
    _, responses = transport
    responses["HITI261016P00002500"] = (403, {"error": "denied"})
    with pytest.raises(StockNearAPIError):
        await fetch_contract_quote("HITI", "2026-10-16", 2.5, "PUT")


@pytest.mark.asyncio
async def test_batch_keeps_order_and_isolates_failures(transport):
    _, responses = transport
    responses["HITI261016P00002500"] = (200, HISTORY)
    responses["HITI261016C00005000"] = (500, {})
    contracts = [
        {"symbol": "HITI", "expiration": "2026-10-16", "strike": 5.0, "option_type": "CALL"},
        {"symbol": "HITI", "expiration": "2026-10-16", "strike": 2.5, "option_type": "PUT"},
    ]
    quotes = await fetch_contract_quotes(contracts)
    assert quotes[0] is None
    assert quotes[1].contract_id == "HITI261016P00002500"
