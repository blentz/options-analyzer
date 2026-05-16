"""StockNear data classes — pure data shapes, no scraping/no IO.

Extracted from stocknear.py so the scraper module focuses on the browser
automation and these structures can be imported by services/tests without
pulling in Playwright as a dependency.

The cookie-extraction helper lives in `stocknear_cookies` (Firefox cookie
SQLite reader, no Playwright either).
"""

from dataclasses import dataclass, field
from typing import Optional


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
    """Full options chain for a symbol."""
    symbol: str
    current_price: Optional[float] = None
    expirations: list[str] = field(default_factory=list)  # Available expiration dates
    contracts: list[OptionContract] = field(default_factory=list)
    iv_rank: Optional[float] = None
    iv_percentile: Optional[float] = None
    implied_volatility: Optional[float] = None
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

