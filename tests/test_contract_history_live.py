"""Live end-to-end download against Stocknear.

Deselected by default. Run explicitly:
    pytest tests/test_contract_history_live.py -m live -v

Requires STOCKNEAR_BROWSER_PROFILE_PATH to point at a Firefox/LibreWolf
profile with a valid logged-in Stocknear session, and a subscription tier
covering the expiration below.

If this test raises ProGatedError, that is NOT a test failure: it means the
configured subscription tier does not cover this contract's expiration, and
it means the substitution guard is working correctly. Do not go debugging
the download code over it -- check the subscription tier instead.
"""

import tempfile
from pathlib import Path

import pytest

from app.config import settings
from app.services.contract_history import parse_history_csv
from app.stocknear import StockNearScraper

CONTRACT = "HITI261016P00002500"
SYMBOL = "HITI"


@pytest.mark.live
def test_downloads_and_parses_real_contract():
    if not settings.stocknear_browser_profile_path:
        pytest.skip("STOCKNEAR_BROWSER_PROFILE_PATH not configured")

    with tempfile.TemporaryDirectory() as tmpdir:
        with StockNearScraper() as scraper:
            path = scraper.download_contract_history(SYMBOL, CONTRACT, Path(tmpdir))

        assert path.exists()
        rows = parse_history_csv(path)

        assert len(rows) > 50
        assert all(r.date is not None for r in rows)
        # IV is a decimal fraction, not a percentage.
        ivs = [r.implied_volatility for r in rows if r.implied_volatility is not None]
        assert ivs and all(0 < iv < 10 for iv in ivs)
