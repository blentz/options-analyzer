"""Live end-to-end history fetch against StockNear's contract API.

Deselected by default. Run explicitly:
    pytest tests/test_contract_history_live.py -m live -v

MAINTENANCE: the contract below (HITI261016P00002500) expires 2026-10-16.
After that date StockNear may stop serving it and this test will fail with
"unknown contract" regardless of the code — update CONTRACT/SYMBOL to a
live expiration. The row-count assertion is a floor (> 50), not an exact
figure, so a substitute doesn't need identical data.
"""

import asyncio

import pytest

from app.services.contract_history import parse_history_json
from app.services.stocknear_contract_api import fetch_contract_history

CONTRACT = "HITI261016P00002500"
SYMBOL = "HITI"


@pytest.mark.live
def test_fetches_and_parses_real_contract():
    payload = asyncio.run(fetch_contract_history(SYMBOL, CONTRACT))
    rows = parse_history_json(payload, CONTRACT)

    assert len(rows) > 50
    assert rows == sorted(rows, key=lambda r: r.date)
    # IV is a decimal fraction, not a percentage.
    ivs = [r.implied_volatility for r in rows if r.implied_volatility is not None]
    assert ivs and all(0 < iv < 10 for iv in ivs)
    # dte is derived from the payload's expiration.
    assert all(r.dte is not None and r.dte >= 0 for r in rows)
