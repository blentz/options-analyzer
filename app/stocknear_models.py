"""StockNear data classes — pure data shapes, no scraping/no IO.

Extracted from stocknear.py so the scraper module focuses on the browser
automation and these structures can be imported by services/tests without
pulling in Playwright as a dependency.

The cookie-extraction helper lives in `stocknear_cookies` (Firefox cookie
SQLite reader, no Playwright either).
"""

from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlparse, parse_qs


@dataclass
class OptionContract:
    """Individual option contract from options chain."""
    strike: float
    option_type: str  # "CALL" or "PUT"
    expiration: str  # Date string like "2025-03-21"
    bid: Optional[float] = None
    ask: Optional[float] = None
    last: Optional[float] = None
    volume: Optional[int] = None
    open_interest: Optional[int] = None
    implied_volatility: Optional[float] = None  # As decimal (0.35 = 35%)
    delta: Optional[float] = None
    gamma: Optional[float] = None
    theta: Optional[float] = None
    vega: Optional[float] = None
    
    @property
    def mid_price(self) -> Optional[float]:
        """Mid of bid/ask, or None when both are unavailable.

        Previously this silently fell back to `last`, which can be days old
        for illiquid contracts. Callers that intentionally want last-trade
        as a fallback should do so explicitly so the staleness is visible
        in the call site rather than buried inside this property.
        """
        if self.bid is not None and self.ask is not None and self.bid > 0 and self.ask > 0:
            return (self.bid + self.ask) / 2
        return None


@dataclass
class ContractQuote:
    """Real-time quote data for a specific option contract from StockNear."""
    symbol: str
    strike: float
    option_type: str  # "CALL" or "PUT"
    expiration: str  # Date string like "2025-03-21"
    contract_id: str  # StockNear contract ID like "BEPC260320P00035000"
    
    # Price data
    bid: Optional[float] = None
    ask: Optional[float] = None
    mid: Optional[float] = None
    last: Optional[float] = None
    open_price: Optional[float] = None
    
    # Volume data
    volume: Optional[int] = None
    open_interest: Optional[int] = None
    
    # Greeks
    implied_volatility: Optional[float] = None  # As decimal (0.35 = 35%)
    delta: Optional[float] = None
    gamma: Optional[float] = None
    theta: Optional[float] = None
    vega: Optional[float] = None

    raw_content: str = ""

    @property
    def spread_quality(self) -> str:
        """
        Classify the bid/ask spread so the UI can warn before users treat a
        wide-spread `mid` as a real price. Categories:
          - "tight"   : spread <= 5% of mid  (mid is meaningful)
          - "moderate": 5% < spread <= 20%   (mid is approximate)
          - "wide"    : 20% < spread <= 50%  (mid only a hint; expect slippage)
          - "very_wide": spread > 50%        (mid is essentially fictional)
          - "no_bid"  : bid is 0 or missing  (no real market)
          - "no_quote": no bid AND no ask    (nothing to trade against)
        """
        if (self.bid is None or self.bid == 0) and (self.ask is None or self.ask == 0):
            return "no_quote"
        if self.bid is None or self.bid == 0:
            return "no_bid"
        if self.ask is None or self.ask == 0:
            return "no_quote"
        mid = (self.bid + self.ask) / 2
        if mid <= 0:
            return "no_quote"
        spread_pct = (self.ask - self.bid) / mid
        if spread_pct <= 0.05:
            return "tight"
        if spread_pct <= 0.20:
            return "moderate"
        if spread_pct <= 0.50:
            return "wide"
        return "very_wide"


@dataclass
class OptionsChain:
    """Full options chain for a symbol.

    The StockNear /stocks/<sym>/options page already contains everything
    we need — IV/rank/percentile, max pain, put-call ratio, total OI,
    expirations, and per-expiration max-pain. The dashboard used to scrape
    the SAME page twice (once via get_options_overview, once via
    get_options_chain) and then make a third trip to /options/max-pain.
    Putting all those fields on this single dataclass lets the symbol-
    lookup endpoint collapse three Playwright navigations into one.
    """
    symbol: str
    current_price: Optional[float] = None
    expirations: list[str] = field(default_factory=list)  # Available expiration dates
    contracts: list[OptionContract] = field(default_factory=list)
    iv_rank: Optional[float] = None
    iv_percentile: Optional[float] = None
    implied_volatility: Optional[float] = None
    # Additional symbol-level fields parsed from the same overview page,
    # so consumers don't need to re-scrape /options/max-pain etc.
    put_call_ratio: Optional[float] = None
    total_volume: Optional[int] = None
    total_open_interest: Optional[int] = None
    max_pain: Optional[float] = None  # nearest-expiry max pain ($)
    raw_content: str = ""
    
    def get_strikes_for_expiration(self, expiration: str) -> list[float]:
        """Get unique strikes for a given expiration."""
        strikes = set()
        for c in self.contracts:
            if c.expiration == expiration:
                strikes.add(c.strike)
        return sorted(strikes)
    
    def get_contract(self, expiration: str, strike: float, option_type: str) -> Optional[OptionContract]:
        """Get a specific contract."""
        for c in self.contracts:
            if c.expiration == expiration and c.strike == strike and c.option_type == option_type:
                return c
        return None
    
    def get_calls(self, expiration: str = None) -> list[OptionContract]:
        """Get all call contracts, optionally filtered by expiration."""
        return [c for c in self.contracts 
                if c.option_type == "CALL" and (expiration is None or c.expiration == expiration)]
    
    def get_puts(self, expiration: str = None) -> list[OptionContract]:
        """Get all put contracts, optionally filtered by expiration."""
        return [c for c in self.contracts 
                if c.option_type == "PUT" and (expiration is None or c.expiration == expiration)]


@dataclass
class OptionsData:
    """Parsed options data from StockNear."""
    symbol: str
    iv_rank: Optional[float] = None  # IV Rank (0-100)
    iv_percentile: Optional[float] = None  # IV Percentile (0-100)
    implied_volatility: Optional[float] = None  # Current IV as decimal (e.g., 0.35 = 35%)
    historical_volatility: Optional[float] = None  # HV as decimal
    put_call_ratio: Optional[float] = None
    total_volume: Optional[int] = None
    total_open_interest: Optional[int] = None
    max_pain: Optional[float] = None
    raw_content: str = ""  # Raw page text for debugging


@dataclass
class StockData:
    """Parsed stock data from StockNear."""
    symbol: str
    price: Optional[float] = None
    change: Optional[float] = None
    change_percent: Optional[float] = None
    market_cap: Optional[str] = None
    volume: Optional[int] = None
    raw_content: str = ""


class ContractHistoryError(Exception):
    """Base for contract-history download failures."""


class ProGatedError(ContractHistoryError):
    """Stocknear served a different contract than the one requested.

    Requesting an expiration outside the current subscription tier does not
    error: the URL is rewritten to a nearer expiration and that contract's
    history is served with HTTP 200. Treating it as success would write the
    wrong contract's prices into the database.
    """


class AuthExpiredError(ContractHistoryError):
    """Session cookies are no longer valid; the page bounced to login."""


class DownloadTimeoutError(ContractHistoryError):
    """The download menu, CSV item, or download event never arrived."""


_PRO_BANNER = "requires a pro subscription. showing the nearest available"
_LOGIN_MARKERS = ("/login", "accounts.google.com")


def verify_contract_served(
    requested_occ: str, page_url: str, page_text: str
) -> None:
    """Raise unless the page is serving the contract we asked for.

    Checks auth first: a login redirect carries no contract in its URL, so
    testing for substitution first would misreport an expired cookie as a
    subscription problem.
    """
    # Validate input: requested_occ must be a non-empty string
    if not isinstance(requested_occ, str) or not requested_occ:
        raise ContractHistoryError(
            f"requested_occ must be a non-empty string, got {requested_occ!r}"
        )

    url = (page_url or "").lower()
    text = (page_text or "").lower()
    wanted = requested_occ.lower()

    # Check auth first: login redirects have no contract parameter,
    # so if we checked substitution first we'd misreport an expired cookie
    # as a subscription problem.
    if any(marker in url for marker in _LOGIN_MARKERS):
        raise AuthExpiredError(
            f"Requesting {requested_occ} redirected to {page_url!r}; "
            "session cookies are expired or missing."
        )

    # A redirect that is neither a recognised login bounce nor the
    # contract-lookup page itself (e.g. /sign-in, /auth, some other
    # interstitial) is not a subscription problem -- it just isn't the
    # page we asked for. Reporting it as ProGatedError would send the
    # user to check their subscription when their cookies actually
    # expired in some way _LOGIN_MARKERS doesn't recognise. This still
    # fails closed (a plain ContractHistoryError, not a silent pass); it
    # only changes the label so the user is pointed at the right fix.
    parsed_path = urlparse(page_url or "").path.lower()
    if "contract-lookup" not in parsed_path:
        raise ContractHistoryError(
            f"Requesting {requested_occ} landed on an unexpected page "
            f"(not the contract-lookup page): {page_url!r}"
        )

    # Banner check: secondary signal. Matches the fuller observed sentence
    # ("... requires a Pro subscription. Showing the nearest available
    # date") rather than the bare "requires a pro subscription" fragment,
    # which risked matching an unrelated site-wide upsell banner appearing
    # anywhere in the page body and misreporting a correctly-served
    # contract as gated. If Stocknear rewords the banner, the
    # query-parameter comparison below is the authoritative check and
    # will catch the substitution. This banner check exists to catch the
    # case where the URL rewrite hasn't happened yet but the banner has
    # rendered. Do NOT rely on the banner as the real guard.
    if _PRO_BANNER in text:
        raise ProGatedError(
            f"{requested_occ} requires a higher subscription tier; "
            "Stocknear substituted the nearest available expiration."
        )

    # Query-parameter comparison: authoritative check for contract substitution.
    # Exact match on the contract parameter, case-insensitive. This is the
    # only reliable way to detect silent contract substitution.
    # Parse the original (non-lowercased) URL to preserve contract casing in error messages.
    parsed = urlparse(page_url or "")
    params = parse_qs(parsed.query)

    # parse_qs returns lists of values; require exactly one contract parameter.
    # If there are multiple contract parameters, we cannot determine which one
    # the server actually rendered, so we must reject the ambiguity.
    values = params.get("contract", [])

    if len(values) == 0:
        raise ProGatedError(
            f"Page URL has no contract parameter. "
            f"Cannot confirm which contract was served: {page_url!r}"
        )

    if len(values) > 1:
        raise ProGatedError(
            f"Page URL has multiple contract parameters: {values}. "
            f"Cannot determine which contract was served: {page_url!r}"
        )

    served_contract = values[0]
    served_contract_lower = served_contract.lower()

    if served_contract_lower != wanted:
        raise ProGatedError(
            f"Requested {requested_occ} but the page served {served_contract}. "
            f"Refusing to ingest a different contract's history."
        )

