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


class TestVulnerabilityFixes:
    """Tests for vulnerabilities found by adversarial review."""

    def test_exact_contract_param_match_rejects_echoed_in_other_param(self):
        """URL echoing the wanted contract in a second param while contract= serves different.

        Vulnerability: substring matching (`"HITI261016P00002500" in url`) would pass
        if the requested contract appears in ANY query parameter.
        Fix: exact match on the contract parameter value only.
        """
        # Contract param serves a different contract; wanted is in a second param
        url = "https://www.stocknear.com/stocks/HITI/options/contract-lookup?contract=HITI260821P00002500&requested=HITI261016P00002500"
        with pytest.raises(ProGatedError) as exc:
            verify_contract_served(WANTED, url, "Contract History")
        assert "HITI260821P00002500" in str(exc.value)

    def test_exact_contract_param_match_rejects_extra_digit_suffix(self):
        """Contract parameter with extra-digit suffix still passes substring matching.

        Vulnerability: substring matching (`"HITI261016P00002500" in url`) passes
        if the URL contains "contract=HITI261016P000025005" (extra digit).
        Fix: exact string match on the contract parameter.
        """
        url = f"https://www.stocknear.com/stocks/HITI/options/contract-lookup?contract=HITI261016P000025005"
        with pytest.raises(ProGatedError) as exc:
            verify_contract_served(WANTED, url, "Contract History")
        assert "HITI261016P000025005" in str(exc.value)

    def test_rejects_url_with_no_contract_parameter(self):
        """URL lacks contract parameter entirely; we cannot confirm which contract was served.

        Fail closed: if we cannot confirm the right contract was served,
        we must reject it rather than assume it's correct.
        """
        url = "https://www.stocknear.com/stocks/HITI/options/contract-lookup"
        with pytest.raises(ProGatedError) as exc:
            verify_contract_served(WANTED, url, "Contract History")
        assert "no contract parameter" in str(exc.value).lower()

    def test_empty_requested_occ_raises(self):
        """Empty string for requested_occ makes the check a no-op ('' in url is always True).

        Vulnerability: empty symbol would make `wanted not in url` always False,
        so any URL would pass.
        Fix: validate that requested_occ is non-empty before processing.
        """
        from app.stocknear_models import ContractHistoryError

        with pytest.raises(ContractHistoryError) as exc:
            verify_contract_served("", GOOD_URL, "Contract History")
        assert "non-empty string" in str(exc.value).lower()

    def test_none_requested_occ_raises_contract_history_error(self):
        """None for requested_occ raises AttributeError outside module's exception hierarchy.

        Vulnerability: `requested_occ.lower()` on None raises AttributeError,
        which a caller catching ContractHistoryError will not catch.
        Fix: validate input and raise ContractHistoryError.
        """
        from app.stocknear_models import ContractHistoryError

        with pytest.raises(ContractHistoryError) as exc:
            verify_contract_served(None, GOOD_URL, "Contract History")
        # Verify it's a ContractHistoryError, not AttributeError
        assert isinstance(exc.value, ContractHistoryError)
        assert "non-empty string" in str(exc.value).lower()
