"""Endpoint-serialization test for POST /api/contract-history/sync.

The endpoint hand-serializes SyncSummary/SyncError into a JSON dict field
by field. Nothing import-checks that mapping, so a typo'd attribute would
only surface as an AttributeError when a user actually clicks the button.
This test exercises the endpoint through FastAPI's TestClient with
sync_contract_history monkeypatched (no browser, no real DB query) to catch
exactly that class of bug.
"""

from fastapi.testclient import TestClient

from app.database import get_db
from app.main import app
from app.services.contract_history import SyncError, SyncSummary


async def _fake_get_db():
    # sync_contract_history is monkeypatched below, so the session it would
    # receive is never touched. Yielding None keeps this test free of any
    # real database setup.
    yield None


def test_sync_endpoint_serializes_summary_and_errors(monkeypatch):
    summary = SyncSummary(
        contracts_total=3,
        synced=1,
        skipped=1,
        failed=2,
        rows_upserted=42,
        errors=[
            SyncError(contract="AAPL261016C00200000", error="ProGatedError: pro tier required"),
            SyncError(contract="MSFT261016P00400000", error="AuthExpiredError: session expired"),
        ],
    )

    captured = {}

    async def fake_sync_contract_history(db, force=False, downloader=None, extra=None):
        captured['extra'] = extra
        return summary

    monkeypatch.setattr(
        "app.services.contract_history.sync_contract_history",
        fake_sync_contract_history,
    )
    app.dependency_overrides[get_db] = _fake_get_db

    try:
        client = TestClient(app)
        response = client.post("/api/contract-history/sync")
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert response.status_code == 200
    data = response.json()

    assert data.keys() == {
        "contracts_total", "synced", "skipped", "failed",
        "rows_upserted", "errors",
    }
    assert data["contracts_total"] == 3
    assert data["synced"] == 1
    assert data["skipped"] == 1
    assert data["failed"] == 2
    assert data["rows_upserted"] == 42

    assert data["errors"] == [
        {"contract": "AAPL261016C00200000", "error": "ProGatedError: pro tier required"},
        {"contract": "MSFT261016P00400000", "error": "AuthExpiredError: session expired"},
    ]


def test_sync_endpoint_forwards_speculation_legs(monkeypatch):
    """The speculation page posts its strategy legs; they must reach the service.

    A leg carries action/quantity/premium that history does not need — only
    symbol, strike, expiration and type matter for building an OCC symbol.
    """
    from datetime import date
    from decimal import Decimal

    captured = {}

    async def fake_sync(db, force=False, downloader=None, extra=None):
        captured["extra"] = extra
        return SyncSummary(contracts_total=len(extra or []), synced=len(extra or []))

    monkeypatch.setattr("app.services.contract_history.sync_contract_history", fake_sync)
    app.dependency_overrides[get_db] = _fake_get_db
    try:
        client = TestClient(app)
        response = client.post("/api/contract-history/sync", json={
            "symbol": "AAPL",
            "legs": [
                {"option_type": "CALL", "strike": 200.0, "expiration": "2026-12-18"},
                {"option_type": "PUT", "strike": 180.5, "expiration": "2027-01-15"},
            ],
        })
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert response.status_code == 200
    refs = captured["extra"]
    assert [(r.symbol, r.strike, r.option_type, r.expiration) for r in refs] == [
        ("AAPL", Decimal("200.0"), "CALL", date(2026, 12, 18)),
        ("AAPL", Decimal("180.5"), "PUT", date(2027, 1, 15)),
    ]


def test_sync_endpoint_rejects_unusable_leg(monkeypatch):
    """A malformed leg is the caller's error — reject rather than sync a subset."""
    async def fake_sync(db, force=False, downloader=None, extra=None):
        raise AssertionError("service must not be called for an unusable leg")

    monkeypatch.setattr("app.services.contract_history.sync_contract_history", fake_sync)
    app.dependency_overrides[get_db] = _fake_get_db
    try:
        client = TestClient(app)
        response = client.post("/api/contract-history/sync", json={
            "symbol": "AAPL",
            "legs": [{"option_type": "CALL", "strike": 200.0, "expiration": "not-a-date"}],
        })
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert response.status_code == 422


def test_sync_endpoint_without_body_syncs_open_positions_only(monkeypatch):
    """Positions-page behaviour must survive the signature change."""
    captured = {}

    async def fake_sync(db, force=False, downloader=None, extra=None):
        captured["extra"] = extra
        return SyncSummary()

    monkeypatch.setattr("app.services.contract_history.sync_contract_history", fake_sync)
    app.dependency_overrides[get_db] = _fake_get_db
    try:
        client = TestClient(app)
        response = client.post("/api/contract-history/sync")
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert response.status_code == 200
    assert captured["extra"] is None
