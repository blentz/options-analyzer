"""Browser-free tests for the contract-lookup URL builder.

`verify_contract_served` (app.stocknear_models) requires the page URL to
carry exactly one `contract` query parameter equal to the requested OCC
symbol, case-insensitively. Nothing enforces that shape at the point the
URL is built, so this locks it down.

Importing app.stocknear pulls in Playwright, but StockNearScraper.__init__
does not launch a browser, so instantiating it here stays browser-free.
"""

from urllib.parse import urlparse, parse_qs

from app.stocknear import StockNearScraper

OCC_SYMBOL = "HITI261016P00002500"


class TestContractLookupUrl:
    def test_exact_path(self):
        scraper = StockNearScraper()
        url = scraper._contract_lookup_url("HITI", OCC_SYMBOL)
        assert url == (
            f"{scraper.base_url}/stocks/hiti"
            f"/options/contract-lookup?contract={OCC_SYMBOL}"
        )

    def test_ticker_segment_is_lowercased(self):
        scraper = StockNearScraper()
        url = scraper._contract_lookup_url("HITI", OCC_SYMBOL)
        path = urlparse(url).path
        assert "/stocks/hiti/" in path
        assert "/stocks/HITI/" not in path

    def test_query_has_exactly_one_contract_param_matching_occ_symbol(self):
        scraper = StockNearScraper()
        url = scraper._contract_lookup_url("hiti", OCC_SYMBOL)
        params = parse_qs(urlparse(url).query)
        assert params.get("contract") == [OCC_SYMBOL]

    def test_lowercased_input_ticker_still_produces_lowercase_segment(self):
        scraper = StockNearScraper()
        url = scraper._contract_lookup_url("hiti", OCC_SYMBOL)
        assert "/stocks/hiti/" in url
