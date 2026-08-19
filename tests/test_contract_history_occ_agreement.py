"""Regression test for N6: occ_symbol() (service) and _build_contract_id()
(scraper) must produce identical OCC symbols for the same contract.

_build_contract_id used int(strike * 1000) on a raw float, which truncates
for strikes like 2.55 (2.55 * 1000 == 2549.9999999999995, so int() gives
2549 instead of 2550). occ_symbol always routed through Decimal(str(strike))
and was correct.

Why this matters more than ordinary duplication: if the two ever produced
different symbols, the requested URL and the served page would still
*agree with each other* — the substitution guard in verify_contract_served
compares the served contract param against the OCC symbol the caller
built, and both sides would use the same (wrong) code path consistently.
The guard would pass while a different contract's history got written
under the requested contract's id, which is precisely the corruption the
guard exists to prevent.

Importing app.stocknear pulls in Playwright, but StockNearScraper.__init__
does not launch a browser, so instantiating it here stays browser-free.
"""

from datetime import date
from decimal import Decimal

import pytest

from app.models import OptionContract
from app.services.contract_history import occ_symbol
from app.stocknear import StockNearScraper


@pytest.mark.parametrize(
    "strike",
    ["2.55", "0.05", "1234.50", "5.00", "150.00", "0.01", "99.99"],
)
def test_occ_symbol_agrees_with_build_contract_id(strike):
    contract = OptionContract(
        symbol="HITI",
        expiration=date(2026, 10, 16),
        strike=Decimal(strike),
        option_type="PUT",
    )
    service_result = occ_symbol(contract)

    scraper = StockNearScraper()
    scraper_result = scraper._build_contract_id(
        "HITI", "2026-10-16", "PUT", float(strike)
    )

    assert service_result == scraper_result


class TestOccSymbolValidation:
    """N10: occ_symbol() flows unvalidated into a filesystem path in
    StockNearScraper.download_contract_history (dest_dir / f"{occ_symbol}.csv")
    and into a URL query parameter. A malformed symbol (e.g. containing '/'
    or produced from a ticker longer than the OCC format allows) must be
    rejected at the point it's built, since occ_symbol() is the sole real
    producer of this string for every caller.
    """

    def test_rejects_ticker_longer_than_occ_allows(self):
        from app.stocknear_models import ContractHistoryError

        contract = OptionContract(
            symbol="TOOLONGTICKER",
            expiration=date(2026, 10, 16),
            strike=Decimal("2.50"),
            option_type="PUT",
        )
        with pytest.raises(ContractHistoryError):
            occ_symbol(contract)

    def test_accepts_well_formed_symbol(self):
        contract = OptionContract(
            symbol="HITI",
            expiration=date(2026, 10, 16),
            strike=Decimal("2.50"),
            option_type="PUT",
        )
        assert occ_symbol(contract) == "HITI261016P00002500"


def test_occ_symbol_255_strike_regression():
    """Pins the exact failure mode: 2.55 * 1000 float-truncates to 2549."""
    contract = OptionContract(
        symbol="HITI",
        expiration=date(2026, 10, 16),
        strike=Decimal("2.55"),
        option_type="PUT",
    )
    assert occ_symbol(contract) == "HITI261016P00002550"

    scraper = StockNearScraper()
    assert (
        scraper._build_contract_id("HITI", "2026-10-16", "PUT", 2.55)
        == "HITI261016P00002550"
    )
