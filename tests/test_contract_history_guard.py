"""Guard tests for silent contract substitution.

Observed live: requesting HITI261016P00002500 while unauthenticated
returned HITI260821P00002500 — HTTP 200, well-formed CSV, different
contract. Without this guard an expired cookie writes another contract's
prices into your history and nothing downstream can tell.
"""

import pytest

from app.stocknear_models import (
    AuthExpiredError,
    ProGatedError,
    verify_contract_served,
)

WANTED = "HITI261016P00002500"
GOOD_URL = f"https://www.stocknear.com/stocks/HITI/options/contract-lookup?contract={WANTED}"
BANNER = (
    "The requested expiration date requires a Pro subscription. "
    "Showing the nearest available date."
)


class TestAccepts:
    def test_matching_contract_passes(self):
        verify_contract_served(WANTED, GOOD_URL, "Contract History  History  Download")

    def test_case_insensitive_url(self):
        verify_contract_served(WANTED, GOOD_URL.lower(), "Contract History")


class TestRejects:
    def test_substituted_contract_raises(self):
        substituted = GOOD_URL.replace(WANTED, "HITI260821P00002500")
        with pytest.raises(ProGatedError) as exc:
            verify_contract_served(WANTED, substituted, "Contract History")
        assert "HITI260821P00002500" in str(exc.value)

    def test_banner_raises_even_if_url_looks_right(self):
        """The URL is not always rewritten before the banner renders."""
        with pytest.raises(ProGatedError):
            verify_contract_served(WANTED, GOOD_URL, f"Option Contract Lookup {BANNER}")

    def test_login_redirect_raises(self):
        with pytest.raises(AuthExpiredError):
            verify_contract_served(WANTED, "https://www.stocknear.com/login", "Log in")

    def test_oauth_redirect_raises(self):
        with pytest.raises(AuthExpiredError):
            verify_contract_served(
                WANTED, "https://accounts.google.com/v3/signin/identifier?x=1", "Sign in"
            )

    def test_auth_checked_before_substitution(self):
        """A login redirect has no contract in the URL; report the real cause."""
        with pytest.raises(AuthExpiredError):
            verify_contract_served(WANTED, "https://www.stocknear.com/login", BANNER)
