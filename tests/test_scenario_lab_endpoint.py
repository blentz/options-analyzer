"""Endpoint test for GET /api/risk/scenario-lab.

The endpoint stitches together a DB position, a Yahoo quote, the StockNear
overview and a scraped contract quote. All four are monkeypatched here so the
test exercises the wiring and JSON shape without a browser or database.
"""

from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.database import get_db
from app.main import app
from app.stocknear_models import ContractQuote, OptionsData


async def _fake_get_db():
    yield None


@pytest.fixture
def client(monkeypatch):
    expiration = date.today() + timedelta(days=22)
    position = SimpleNamespace(
        contract=SimpleNamespace(symbol="HITI", strike=2.5, option_type="PUT", expiration=expiration),
        strategy="SHORT PUT",
        total_premium=50.0,
        num_contracts=2,
        volatility_override=None,
        is_closed=False,
    )

    async def fake_find(db, contract_id):
        return position

    async def fake_price(symbol):
        return SimpleNamespace(price=2.66, change=-0.06, change_percent=-2.2, timestamp=datetime.now())

    async def fake_overview(db, symbol, force_refresh=False):
        return OptionsData(symbol=symbol, implied_volatility=1.1625, iv_rank=74.8, max_pain=2.5)

    async def fake_quote(db, symbol, expiration, strike, option_type, force_refresh=False):
        return ContractQuote(
            symbol=symbol, strike=strike, option_type=option_type,
            expiration=expiration, contract_id="HITI261016P00002500",
            bid=0.092, ask=0.108, mid=0.10, implied_volatility=0.7344,
        )

    monkeypatch.setattr("app.routers.scenario_lab._find_open_position", fake_find)
    monkeypatch.setattr("app.services.price_service.get_stock_price", fake_price)
    monkeypatch.setattr("app.services.stocknear_service.get_options_overview", fake_overview)
    monkeypatch.setattr("app.services.stocknear_service.get_contract_quote", fake_quote)
    app.dependency_overrides[get_db] = _fake_get_db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_db, None)


URL = "/api/risk/scenario-lab"
CID = "HITI 10/16/26 $2.50 PUT"


def test_auto_volatility_uses_mid(client):
    data = client.get(URL, params={"contract_id": CID}).json()
    # Spread 0.016 on 0.10 mid is "moderate" -> mid-implied IV wins over
    # the scraped 73% and the symbol-wide 116%.
    assert data["volatility"]["source"] == "implied_from_mid"
    assert data["volatility"]["used"] == pytest.approx(0.67, abs=0.02)
    assert data["model"]["price"] == pytest.approx(0.10, abs=0.002)
    assert data["contract_market"]["spread_quality"] == "moderate"
    assert data["value_matrix"]["days_left"][-1] == 0
    assert "target" not in data


def test_target_conditions(client):
    data = client.get(URL, params={"contract_id": CID, "target_price": 0.05, "volatility": 0.7344}).json()
    assert data["volatility"]["source"] == "manual"
    t = data["target"]
    assert t["direction"] == "decrease"
    assert t["spot_needed_now"] == pytest.approx(2.93, abs=0.01)
    # Sold 2 contracts at $0.25, buy back at $0.05 -> +$40.
    assert t["close_pnl"] == pytest.approx(40.0)
    assert {"spot_path", "hold_path", "iv_path"} <= t.keys()
    assert t["hold_path"][0]["date"] is None or isinstance(t["hold_path"][0]["date"], str)


def test_custom_grid_percents(client):
    data = client.get(URL, params={"contract_id": CID, "target_price": 0.05, "grid_pct": "90,60"}).json()
    assert data["grid_percents"] == [90, 60]
    days = data["value_matrix"]["days_left"]
    assert [r["days_left"] for r in data["target"]["spot_path"]] == days[:-1]
    assert days == [22, 20, 13, 7, 5, 3, 1, 0]


def test_invalid_grid_percents_is_400(client):
    res = client.get(URL, params={"contract_id": CID, "grid_pct": "150"})
    assert res.status_code == 400
