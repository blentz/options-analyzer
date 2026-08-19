"""Endpoint-serialization test for POST /api/contract-history/sync.

The endpoint hand-serializes SyncSummary/SyncError into a JSON dict field
by field. Nothing import-checks that mapping, so a typo'd attribute would
only surface as an AttributeError when a user actually clicks the button.
This test exercises the endpoint through FastAPI's TestClient with
sync_open_positions monkeypatched (no browser, no real DB query) to catch
exactly that class of bug.
"""

from fastapi.testclient import TestClient

from app.database import get_db
from app.main import app
from app.services.contract_history import SyncError, SyncSummary


async def _fake_get_db():
    # sync_open_positions is monkeypatched below, so the session it would
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

    async def fake_sync_open_positions(db, force=False, downloader=None):
        return summary

    monkeypatch.setattr(
        "app.services.contract_history.sync_open_positions",
        fake_sync_open_positions,
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
