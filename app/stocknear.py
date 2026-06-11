"""StockNear browser scraper using Playwright with LibreWolf cookies.

Provides authenticated access to stocknear.com data. Configuration is
loaded from environment variables via app.config.

Module layout:
  - Data shapes (OptionContract, ContractQuote, etc) live in
    `app.stocknear_models` so consumers can import them without Playwright.
  - Cookie extraction lives in `app.stocknear_cookies` for the same reason.
  - This file holds the StockNearScraper class itself and the CLI entry
    point. The dataclasses + cookie helper are re-exported below so the
    historical `from app.stocknear import OptionContract` etc. continue
    to work.
"""

import json
import logging
import re
import sys
import time
from pathlib import Path
from typing import Optional
from playwright.sync_api import sync_playwright, Page, BrowserContext

from app.config import settings
from app.stocknear_models import (
    OptionContract,
    ContractQuote,
    OptionsChain,
    OptionsData,
    StockData,
)
from app.stocknear_cookies import extract_browser_cookies

# Re-exported public API — keep these names available for callers that
# `from app.stocknear import OptionContract`. Without __all__ the names are
# still importable; this just documents intent.
__all__ = [
    "OptionContract", "ContractQuote", "OptionsChain", "OptionsData",
    "StockData", "extract_browser_cookies", "StockNearScraper",
]

logger = logging.getLogger(__name__)


class StockNearScraper:
    """
    Scraper for stocknear.com using Playwright with rate limiting.
    
    Configuration is read from environment variables:
    - STOCKNEAR_BROWSER_PROFILE_PATH: Path to Firefox/LibreWolf profile with cookies
    - STOCKNEAR_HEADLESS: Whether to run browser in headless mode (default: true)
    - STOCKNEAR_RATE_LIMIT_DELAY: Seconds between requests (default: 1.0)
    """
    
    def __init__(self, headless: Optional[bool] = None, rate_limit_delay: Optional[float] = None):
        self.headless = headless if headless is not None else settings.stocknear_headless
        # Hard floor of 1.0s: never hit stocknear faster than one request per
        # second, regardless of how STOCKNEAR_RATE_LIMIT_DELAY is configured.
        # A larger value (more polite) is honored; anything smaller is clamped.
        _delay = rate_limit_delay if rate_limit_delay is not None else settings.stocknear_rate_limit_delay
        self.rate_limit_delay = max(1.0, _delay)
        self.base_url = settings.stocknear_base_url
        self.profile_path = settings.stocknear_browser_profile_path
        self.user_data_dir = settings.stocknear_user_data_dir

        self.playwright = None
        self.context: BrowserContext = None
        self.page: Page = None
        self._persistent = False
        self._last_request_time: float = 0

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def start(self):
        """Start the browser, authenticated.

        Two modes:

        * **Persistent profile** (``stocknear_user_data_dir`` set): drive ONE
          long-lived browser on a dedicated on-disk profile. You log into
          stocknear (and solve any Cloudflare challenge) once in this visible
          browser; the session + clearance persist, so automated reads happen in
          the very same authenticated browser. This is the reliable way past
          bot-verification — a throwaway context flagged as automation gets
          challenged on protected routes no matter what cookies we inject.
        * **Cookie injection** (legacy): launch a throwaway context and inject
          cookies from a real Firefox profile. Works for lightly-protected
          pages; fails Cloudflare on hardened routes.
        """
        logger.info("Starting StockNear scraper (headless=%s, persistent=%s)",
                    self.headless, bool(self.user_data_dir))
        self.playwright = sync_playwright().start()

        if self.user_data_dir:
            self._persistent = True
            Path(self.user_data_dir).mkdir(parents=True, exist_ok=True)
            # Persistent context owns its browser; cookies/storage live on disk.
            self.context = self.playwright.firefox.launch_persistent_context(
                self.user_data_dir,
                headless=self.headless,
                viewport={"width": 1280, "height": 900},
            )
            # Reduce the most obvious automation signal so Cloudflare is less
            # likely to re-challenge a browser whose clearance you already solved.
            try:
                self.context.add_init_script(
                    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
                )
            except Exception:
                pass
            self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        else:
            # Legacy: throwaway context + injected cookies.
            browser = self.playwright.firefox.launch(headless=self.headless)
            self.context = browser.new_context(viewport={"width": 1280, "height": 800})
            if self.profile_path:
                logger.info("Loading cookies from profile: %s", self.profile_path)
                cookies = extract_browser_cookies(self.profile_path, "stocknear.com")
                if cookies:
                    logger.info("Adding %d cookies to browser context", len(cookies))
                    self.context.add_cookies(cookies)
                else:
                    logger.warning("No cookies found for stocknear.com - scraper will not be authenticated!")
            else:
                logger.warning("No profile path configured - scraper will not be authenticated!")
            self.page = self.context.new_page()

        # Test authentication / surface login state.
        logger.debug("Testing authentication status...")
        try:
            self.page.goto(f"{self.base_url}/stocks/aapl", wait_until="domcontentloaded")
            self.page.wait_for_timeout(2000)
            page_text = self.page.inner_text("body")
        except Exception as e:
            logger.warning("Auth-check navigation failed: %s", e)
            page_text = ""
        if "Login" in page_text and "Start Trial" in page_text:
            logger.warning("Authentication may have FAILED - page shows Login/Start Trial buttons")
        elif "Logout" in page_text or "Account" in page_text or "Profile" in page_text:
            logger.info("Authentication SUCCESS - user appears to be logged in")
        else:
            logger.debug("Authentication status unclear - page content length: %d", len(page_text))

    def is_logged_in(self) -> bool:
        """Best-effort check that the current session is authenticated."""
        try:
            text = self.page.inner_text("body")
        except Exception:
            return False
        return ("Logout" in text or "Account" in text or "Profile" in text) and "Start Trial" not in text

    def wait_for_login(self, timeout_seconds: int = 300) -> bool:
        """Block until the user has logged in (and cleared any challenge) in the
        visible persistent browser. Returns True once authenticated."""
        self.page.goto(self.base_url, wait_until="domcontentloaded")
        logger.info("Waiting up to %ds for you to log into stocknear in the browser window…", timeout_seconds)
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            if self.is_logged_in():
                logger.info("Login detected — session persisted to %s", self.user_data_dir)
                return True
            self.page.wait_for_timeout(1500)
        logger.warning("Timed out waiting for login.")
        return False

    def close(self):
        """Clean up browser resources."""
        if self.context:
            self.context.close()
        if self.playwright:
            self.playwright.stop()

    def _rate_limit(self):
        """Enforce rate limiting between requests."""
        elapsed = time.time() - self._last_request_time
        if elapsed < self.rate_limit_delay:
            time.sleep(self.rate_limit_delay - elapsed)
        self._last_request_time = time.time()

    def navigate(self, path: str, wait_for_selector: str = None) -> str:
        """
        Navigate to a StockNear page and return the page content.
        
        Args:
            path: URL path to navigate to
            wait_for_selector: Optional CSS selector to wait for before returning
        """
        self._rate_limit()
        url = f"{self.base_url}{path}" if path.startswith("/") else f"{self.base_url}/{path}"
        self.page.goto(url, wait_until="networkidle")
        
        # Wait for selector if provided
        if wait_for_selector:
            try:
                self.page.wait_for_selector(wait_for_selector, timeout=10000)
            except Exception as e:
                logger.warning("Timeout waiting for selector '%s': %s", wait_for_selector, e)
        
        return self.page.content()

    def get_page_text(self) -> str:
        """Get visible text content from current page."""
        return self.page.inner_text("body")

    def fetch_data_json(self, path: str) -> Optional[str]:
        """Fetch a SvelteKit ``__data.json`` endpoint as the logged-in browser.

        The fetch runs *inside* the current authenticated page via
        ``page.evaluate`` (a real same-origin browser ``fetch``), so it carries
        the live session cookies AND the browser's fingerprint — Cloudflare and
        the server treat it exactly like the SvelteKit client requesting its own
        page data. A bare ``context.request`` call, by contrast, lacks the
        browser fingerprint and is rejected (HTTP 403) even when authenticated.

        `path` is resolved against the page's own ``location.origin`` so it
        stays same-origin regardless of any www/non-www redirect. Returns the
        raw JSON text, or None on failure / non-JSON body (login redirect).
        """
        self._rate_limit()
        try:
            result = self.page.evaluate(
                """async (p) => {
                    const url = location.origin + p;
                    const r = await fetch(url, {credentials: 'include', headers: {'accept': 'application/json'}});
                    return {status: r.status, body: await r.text()};
                }""",
                path,
            )
        except Exception as e:
            logger.error("data.json in-page fetch failed for %s: %s", path, e)
            return None
        if result.get("status") != 200:
            logger.warning("data.json fetch %s -> HTTP %s", path, result.get("status"))
            return None
        text = result.get("body") or ""
        if not text.lstrip().startswith("{"):
            logger.warning("data.json fetch for %s returned non-JSON (auth/login redirect?)", path)
            return None
        return text

    def get_fundamentals(self, symbol: str):
        """Fetch + parse company fundamentals (income statement + balance sheet)
        through the authenticated session. Returns a Fundamentals dataclass.

        Navigates to the ticker's financials page first so the subsequent
        ``__data.json`` fetches are same-origin reads of the page's own data —
        the same requests the site itself makes."""
        from app.services.stocknear_financials import (
            INCOME_DATA_PATH, BALANCE_DATA_PATH, build_fundamentals,
        )
        sym = symbol.lower()
        income_text = balance_text = None
        # Never raise: a Cloudflare challenge can reload the page mid-call and
        # destroy the execution context (page.goto / evaluate throw). The caller
        # should see "data unavailable", not a hard error — so swallow here and
        # return whatever we managed to read.
        try:
            # domcontentloaded, not networkidle — these pages stream/poll and
            # never go idle within the default timeout.
            self._rate_limit()
            self.page.goto(
                f"{self.base_url}/stocks/{sym}/financials",
                wait_until="domcontentloaded", timeout=20000,
            )
            income_text = self.fetch_data_json(INCOME_DATA_PATH.format(sym=sym))
            balance_text = self.fetch_data_json(BALANCE_DATA_PATH.format(sym=sym))
        except Exception as e:
            logger.warning("get_fundamentals(%s) blocked/failed: %s", symbol, e)
        return build_fundamentals(symbol, income_text, balance_text)

    def _parse_number(self, text: str) -> Optional[float]:
        """Parse a number from text, handling K/M/B suffixes."""
        if not text:
            return None
        text = text.strip().replace(",", "").replace("$", "").replace("%", "")
        
        multipliers = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}
        for suffix, mult in multipliers.items():
            if text.endswith(suffix):
                try:
                    return float(text[:-1]) * mult
                except ValueError:
                    return None
        try:
            return float(text)
        except ValueError:
            return None

    def get_options_overview(self, symbol: str) -> OptionsData:
        """
        Get options overview data (IV, OI, volume stats) for a stock.
        Returns parsed OptionsData with IV rank, IV percentile, etc.
        """
        logger.info("Scraping options overview for %s", symbol)
        # Navigate and wait for the page to fully load
        # Wait for text containing "IV Rank" or similar to appear
        self.navigate(f"/stocks/{symbol.lower()}/options")
        
        # Wait for the main content to render - try multiple selectors
        try:
            # Wait for any element containing IV data to appear
            self.page.wait_for_function(
                """() => {
                    const text = document.body.innerText;
                    return text.includes('IV Rank') || 
                           text.includes('IV Percentile') || 
                           text.includes('Implied Volatility') ||
                           text.includes('Put/Call Ratio') ||
                           text.length > 5000;
                }""",
                timeout=15000
            )
        except Exception as e:
            logger.warning("Timeout waiting for IV data to load for %s: %s", symbol, e)
        
        # Additional wait for dynamic content
        self.page.wait_for_timeout(2000)

        content = self.get_page_text()
        data = OptionsData(symbol=symbol.upper(), raw_content=content)
        
        logger.debug("Page content length for %s: %d chars", symbol, len(content))
        
        # Log first 500 chars for debugging
        if len(content) < 4000:
            logger.warning("Short page content for %s - may not be fully loaded. First 500 chars: %s", symbol, content[:500])

        # Parse IV Rank - handles newline-separated format from StockNear
        # Format: "IV Rank\n16.28%" or "IV Rank\n16.28"
        iv_rank_match = re.search(r'IV\s*Rank\s*[\n\r]+\s*(\d+\.?\d*)\s*%?', content, re.IGNORECASE)
        if not iv_rank_match:
            # Fallback: older format "IV Rank: 45.2"
            iv_rank_match = re.search(r'IV\s*Rank[:\s\t]+(\d+\.?\d*)\s*%?', content, re.IGNORECASE)
        if iv_rank_match:
            data.iv_rank = float(iv_rank_match.group(1))
            logger.debug("Parsed IV Rank: %s", data.iv_rank)
        else:
            logger.debug("IV Rank not found in content")

        # Parse IV Percentile - handles newline-separated format
        iv_pct_match = re.search(r'IV\s*Percentile\s*[\n\r]+\s*(\d+\.?\d*)\s*%?', content, re.IGNORECASE)
        if not iv_pct_match:
            iv_pct_match = re.search(r'IV\s*Percentile[:\s\t]+(\d+\.?\d*)\s*%?', content, re.IGNORECASE)
        if iv_pct_match:
            data.iv_percentile = float(iv_pct_match.group(1))
            logger.debug("Parsed IV Percentile: %s", data.iv_percentile)
        else:
            logger.debug("IV Percentile not found in content")

        # Parse Implied Volatility (current IV) - handles newline-separated StockNear format
        # Format: "Implied Volatility (30d)\n47.02%" or "IV (30d)\n47.02% IV"
        iv_match = re.search(r'Implied\s*Volatility\s*\([^)]*\)\s*[\n\r]+\s*(\d+\.?\d*)\s*%', content, re.IGNORECASE)
        if not iv_match:
            # Try: "IV (30d)\n47.02% IV" format
            iv_match = re.search(r'IV\s*\(\d+d\)\s*[\n\r]+\s*(\d+\.?\d*)\s*%', content, re.IGNORECASE)
        if not iv_match:
            # Fallback: "47.02% IV" near "Implied Volatility" 
            iv_match = re.search(r'(\d+\.?\d*)\s*%\s*IV\b', content, re.IGNORECASE)
        if not iv_match:
            # Fallback: older inline format
            iv_match = re.search(r'Implied\s*Volatility(?:\s*\(IV\))?[:\s\t]+(\d+\.?\d*)\s*%', content, re.IGNORECASE)
        if iv_match:
            data.implied_volatility = float(iv_match.group(1)) / 100  # Convert to decimal
            logger.debug("Parsed IV: %s", data.implied_volatility)
        else:
            logger.debug("Implied Volatility not found in content")

        # Parse Put/Call Ratio - handles newline-separated format
        # Format: "Put-Call Ratio\n0.68" or "Put/Call Ratio\n0.44"
        pcr_match = re.search(r'Put[/-]Call\s*Ratio\s*[\n\r]+\s*(\d+\.?\d*)', content, re.IGNORECASE)
        if not pcr_match:
            # Fallback: inline format
            pcr_match = re.search(r'Put[/\s-]*Call\s*Ratio[:\s\t]+(\d+\.?\d*)', content, re.IGNORECASE)
        if pcr_match:
            data.put_call_ratio = float(pcr_match.group(1))
            logger.debug("Parsed Put/Call Ratio: %s", data.put_call_ratio)

        # Parse Total Volume - handles newline-separated format
        vol_match = re.search(r'Today\'s\s*Volume\s*[\n\r]+\s*([\d,]+)', content, re.IGNORECASE)
        if not vol_match:
            vol_match = re.search(r'Total\s*(?:Options\s*)?Volume[:\s\t\n\r]+([\d,]+[KMB]?)', content, re.IGNORECASE)
        if vol_match:
            data.total_volume = int(self._parse_number(vol_match.group(1)) or 0)
            logger.debug("Parsed Total Volume: %s", data.total_volume)

        # Parse Total Open Interest - handles newline-separated format
        oi_match = re.search(r'Today\'s\s*Open\s*Interest\s*[\n\r]+\s*([\d,]+)', content, re.IGNORECASE)
        if not oi_match:
            oi_match = re.search(r'(?:Total\s*)?Open\s*Interest[:\s\t\n\r]+([\d,]+[KMB]?)', content, re.IGNORECASE)
        if oi_match:
            data.total_open_interest = int(self._parse_number(oi_match.group(1)) or 0)

        logger.info(
            "Options overview for %s: IV=%s, IV_Rank=%s, IV_Pct=%s, PCR=%s",
            symbol, data.implied_volatility, data.iv_rank, data.iv_percentile, data.put_call_ratio
        )
        return data

    def get_max_pain(self, symbol: str) -> OptionsData:
        """Get max pain analysis for a stock's options."""
        self.navigate(f"/stocks/{symbol.lower()}/options/max-pain")
        self.page.wait_for_timeout(2000)

        content = self.get_page_text()
        data = OptionsData(symbol=symbol.upper(), raw_content=content)

        # Parse max pain price
        max_pain_match = re.search(r'Max\s*Pain[:\s]*\$?([\d,]+\.?\d*)', content, re.IGNORECASE)
        if max_pain_match:
            data.max_pain = self._parse_number(max_pain_match.group(1))

        return data

    def get_stock_overview(self, symbol: str) -> StockData:
        """Get overview data for a stock."""
        self.navigate(f"/stocks/{symbol.lower()}")
        self.page.wait_for_timeout(2000)

        content = self.get_page_text()
        data = StockData(symbol=symbol.upper(), raw_content=content)

        # Parse current price - look for dollar amount near the top
        price_match = re.search(r'\$(\d+\.?\d*)', content)
        if price_match:
            data.price = float(price_match.group(1))

        # Parse change
        change_match = re.search(r'([+-]?\d+\.?\d*)\s*\(([+-]?\d+\.?\d*)%\)', content)
        if change_match:
            data.change = float(change_match.group(1))
            data.change_percent = float(change_match.group(2))

        return data

    def get_options_chain(self, symbol: str, expiration: str = None) -> dict:
        """Get options chain with Greeks for a stock."""
        self.navigate(f"/stocks/{symbol.lower()}/options/greeks")
        self.page.wait_for_timeout(2000)

        if expiration:
            try:
                self.page.click(f"text={expiration}", timeout=3000)
                self.page.wait_for_timeout(2000)
            except:
                pass

        return {
            "symbol": symbol.upper(),
            "url": self.page.url,
            "content": self.get_page_text()
        }

    def get_options_chain_parsed(self, symbol: str) -> OptionsChain:
        """
        Get full options chain with parsed data.
        
        Uses the options overview page which contains expiration table data.
        Returns an OptionsChain object with structured contract data.
        """
        logger.info("Scraping options chain for %s", symbol)
        chain = OptionsChain(symbol=symbol.upper())
        
        # The options overview page has all the data we need:
        # - IV, IV Rank, IV Percentile
        # - Expiration table with dates, volumes, max pain, and per-expiry IV
        # Navigate and get content (reusing the overview page)
        self.navigate(f"/stocks/{symbol.lower()}/options")
        
        # Wait for content to load
        try:
            self.page.wait_for_function(
                """() => {
                    const text = document.body.innerText;
                    return text.includes('EXPIRATION') || 
                           text.includes('MAX PAIN') ||
                           text.includes('Implied Volatility') ||
                           text.length > 3000;
                }""",
                timeout=15000
            )
        except Exception as e:
            logger.warning("Timeout waiting for options data to load for %s: %s", symbol, e)
        
        self.page.wait_for_timeout(2000)
        
        content = self.get_page_text()
        chain.raw_content = content
        logger.debug("Options page content length: %d chars", len(content))
        
        # Parse IV data using same logic as get_options_overview
        # IV Rank - newline-separated format
        iv_rank_match = re.search(r'IV\s*Rank\s*[\n\r]+\s*(\d+\.?\d*)\s*%?', content, re.IGNORECASE)
        if iv_rank_match:
            chain.iv_rank = float(iv_rank_match.group(1))
        
        # IV Percentile - newline-separated format  
        iv_pct_match = re.search(r'IV\s*Percentile\s*[\n\r]+\s*(\d+\.?\d*)\s*%?', content, re.IGNORECASE)
        if iv_pct_match:
            chain.iv_percentile = float(iv_pct_match.group(1))
        
        # Implied Volatility - newline-separated format
        iv_match = re.search(r'Implied\s*Volatility\s*\([^)]*\)\s*[\n\r]+\s*(\d+\.?\d*)\s*%', content, re.IGNORECASE)
        if not iv_match:
            iv_match = re.search(r'(\d+\.?\d*)\s*%\s*IV\b', content, re.IGNORECASE)
        if iv_match:
            chain.implied_volatility = float(iv_match.group(1)) / 100
        
        logger.debug("Chain IV data: iv=%s, rank=%s, pct=%s",
                     chain.implied_volatility, chain.iv_rank, chain.iv_percentile)

        # Symbol-level overview fields parsed from the SAME page. These used
        # to require a separate /options-overview navigation; collapsing here
        # cuts the symbol-lookup path from 3 Playwright scrapes to 1.
        pcr_match = re.search(r'Put[/-]Call\s*Ratio\s*[\n\r]+\s*(\d+\.?\d*)', content, re.IGNORECASE)
        if not pcr_match:
            pcr_match = re.search(r'Put[/\s-]*Call\s*Ratio[:\s\t]+(\d+\.?\d*)', content, re.IGNORECASE)
        if pcr_match:
            try:
                chain.put_call_ratio = float(pcr_match.group(1))
            except ValueError:
                pass

        vol_match = re.search(r"Today'?s\s*Volume\s*[\n\r]+\s*([\d,]+)", content, re.IGNORECASE)
        if not vol_match:
            vol_match = re.search(r'Total\s*(?:Options\s*)?Volume[:\s\t\n\r]+([\d,]+[KMB]?)', content, re.IGNORECASE)
        if vol_match:
            n = self._parse_number(vol_match.group(1))
            if n is not None:
                chain.total_volume = int(n)

        oi_match = re.search(r"Today'?s\s*Open\s*Interest\s*[\n\r]+\s*([\d,]+)", content, re.IGNORECASE)
        if not oi_match:
            oi_match = re.search(r'(?:Total\s*)?Open\s*Interest[:\s\t\n\r]+([\d,]+[KMB]?)', content, re.IGNORECASE)
        if oi_match:
            n = self._parse_number(oi_match.group(1))
            if n is not None:
                chain.total_open_interest = int(n)

        # Do NOT scrape the underlying price from this page. The options page
        # contains many dollar amounts (max pain, strikes, market cap, etc.) and
        # the first $ match is unreliable — historically caused silent price
        # corruption that propagated through every downstream calculation.
        # Live price must come from a dedicated quote source (Yahoo Finance).
        chain.current_price = None
        
        # Parse expiration table - format is tab-separated:
        # EXPIRATION\tCALL VOL\tPUT VOL\tP/C VOL\tCALL OI\tPUT OI\tP/C OI\tIMPLIED VOLATILITY\tMAX PAIN
        # Jan 30, 2026\t122,233\t48,208\t0.39\t...
        exp_pattern = re.compile(
            r'([A-Z][a-z]{2}\s+\d{1,2},\s*\d{4})'  # Date like "Jan 30, 2026"
            r'\t([\d,]+)\t([\d,]+)\t([\d.]+)'       # CALL VOL, PUT VOL, P/C VOL
            r'\t([\d,]+)\t([\d,]+)\t([\d.]+)'       # CALL OI, PUT OI, P/C OI
            r'\t([\d.]+)%\t([\d.]+)',              # IV%, MAX PAIN
            re.IGNORECASE
        )
        
        expirations = []
        expiration_data = {}  # Store additional data per expiration
        
        for match in exp_pattern.finditer(content):
            exp_date = match.group(1)
            call_vol = int(match.group(2).replace(',', ''))
            put_vol = int(match.group(3).replace(',', ''))
            call_oi = int(match.group(5).replace(',', ''))
            put_oi = int(match.group(6).replace(',', ''))
            exp_iv = float(match.group(8)) / 100  # Convert to decimal
            max_pain = float(match.group(9))
            
            expirations.append(exp_date)
            expiration_data[exp_date] = {
                'call_volume': call_vol,
                'put_volume': put_vol,
                'call_oi': call_oi,
                'put_oi': put_oi,
                'iv': exp_iv,
                'max_pain': max_pain
            }
            logger.debug("Parsed expiration %s: IV=%.2f%%, max_pain=$%.2f", 
                         exp_date, exp_iv * 100, max_pain)
        
        if not expirations:
            # Fallback: look for date patterns like "Jan 30, 2026"
            fallback_pattern = re.compile(r'([A-Z][a-z]{2}\s+\d{1,2},\s*\d{4})')
            matches = fallback_pattern.findall(content)
            # Dedupe while preserving order
            seen = set()
            for m in matches:
                if m not in seen:
                    seen.add(m)
                    expirations.append(m)
        
        chain.expirations = expirations[:25]  # Keep up to 25 expirations
        # Surface the nearest-expiry max-pain as a representative symbol-level
        # value. The full per-expiration map (with everything) stays internal
        # for now — the dashboard only ever consumed the single number, so a
        # full per-expiry API would be premature.
        if chain.expirations and expiration_data:
            first = expiration_data.get(chain.expirations[0])
            if first and first.get('max_pain') is not None:
                chain.max_pain = first['max_pain']
        logger.info("Found %d expirations for %s", len(chain.expirations), symbol)
        
        # We don't have individual contract bid/ask data from the overview page,
        # so we'll create placeholder contracts based on max pain as center strike
        if chain.expirations and expiration_data:
            for exp_date in chain.expirations[:5]:  # First 5 expirations
                exp_info = expiration_data.get(exp_date)
                if not exp_info:
                    continue
                
                max_pain = exp_info['max_pain']
                exp_iv = exp_info['iv']
                
                # Create strikes around max pain
                strike_step = 5 if max_pain > 50 else (2.5 if max_pain > 20 else 1)
                for offset in range(-5, 6):  # 11 strikes centered on max pain
                    strike = round(max_pain + offset * strike_step, 2)
                    if strike <= 0:
                        continue
                    
                    # Create call contract
                    chain.contracts.append(OptionContract(
                        strike=strike,
                        option_type="CALL",
                        expiration=exp_date,
                        implied_volatility=exp_iv,
                    ))
                    
                    # Create put contract
                    chain.contracts.append(OptionContract(
                        strike=strike,
                        option_type="PUT",
                        expiration=exp_date,
                        implied_volatility=exp_iv,
                    ))
        
        logger.info(
            "Options chain for %s: price=%s, %d expirations, %d contracts",
            symbol, chain.current_price, len(chain.expirations), len(chain.contracts)
        )
        return chain

    def get_available_expirations(self, symbol: str) -> list[str]:
        """Get list of available expiration dates for a symbol's options."""
        self.navigate(f"/stocks/{symbol.lower()}/options")
        self.page.wait_for_timeout(2000)
        
        content = self.get_page_text()
        
        # Look for date patterns that appear to be expirations
        # StockNear typically shows dates like "Jan 17", "Feb 21", etc.
        expirations = []
        
        # Pattern for month day format
        month_day_pattern = re.compile(r'(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{1,2})', re.IGNORECASE)
        matches = month_day_pattern.findall(content)
        
        # Convert to standardized format and dedupe
        seen = set()
        for month, day in matches:
            exp_str = f"{month} {day}"
            if exp_str not in seen:
                seen.add(exp_str)
                expirations.append(exp_str)
        
        return expirations[:15]  # Return up to 15 expirations

    def get_options_flow(self, symbol: str = None) -> dict:
        """Get options flow data, optionally filtered by symbol."""
        if symbol:
            self.navigate(f"/stocks/{symbol.lower()}")
            self.page.wait_for_timeout(2000)
            try:
                self.page.click("text=Unusual Orders", timeout=3000)
                self.page.wait_for_timeout(2000)
            except:
                pass
        else:
            self.navigate("/options-flow")
            self.page.wait_for_timeout(2000)

        return {
            "symbol": symbol.upper() if symbol else "ALL",
            "url": self.page.url,
            "content": self.get_page_text()
        }

    def get_dark_pool(self, symbol: str = None) -> dict:
        """Get dark pool transaction data."""
        path = "/darkpool-flow" if not symbol else f"/stocks/{symbol.lower()}"
        self.navigate(path)
        self.page.wait_for_timeout(2000)

        return {
            "symbol": symbol.upper() if symbol else "ALL",
            "url": self.page.url,
            "content": self.get_page_text()
        }

    def get_analyst_ratings(self, symbol: str) -> dict:
        """Get analyst ratings and price targets for a stock."""
        self.navigate(f"/stocks/{symbol.lower()}")
        self.page.wait_for_timeout(2000)
        try:
            self.page.click("text=Forecast", timeout=3000)
            self.page.wait_for_timeout(2000)
        except:
            pass

        return {
            "symbol": symbol.upper(),
            "url": self.page.url,
            "content": self.get_page_text()
        }

    def get_options_gex(self, symbol: str) -> dict:
        """Get gamma exposure (GEX) data for a stock's options."""
        self.navigate(f"/stocks/{symbol.lower()}/options/gex/strike")
        self.page.wait_for_timeout(2000)

        return {
            "symbol": symbol.upper(),
            "url": self.page.url,
            "content": self.get_page_text()
        }

    def get_options_dex(self, symbol: str) -> dict:
        """Get delta exposure (DEX) data for a stock's options."""
        self.navigate(f"/stocks/{symbol.lower()}/options/dex/strike")
        self.page.wait_for_timeout(2000)

        return {
            "symbol": symbol.upper(),
            "url": self.page.url,
            "content": self.get_page_text()
        }

    def _build_contract_id(self, symbol: str, expiration: str, option_type: str, strike: float) -> str:
        """
        Build a StockNear contract ID from components.
        
        Format: {SYMBOL}{YYMMDD}{P|C}{STRIKE*1000:08d}
        Example: BEPC260320P00035000 for BEPC $35 PUT expiring 2026-03-20
        
        Args:
            symbol: Stock ticker (e.g., "BEPC")
            expiration: Date string like "2026-03-20" or "Mar 20, 2026"
            option_type: "CALL" or "PUT"
            strike: Strike price (e.g., 35.00)
        
        Returns:
            Contract ID string like "BEPC260320P00035000"
        """
        from datetime import datetime
        
        # Parse expiration date - try multiple formats
        exp_date = None
        for fmt in ["%Y-%m-%d", "%b %d, %Y", "%B %d, %Y", "%m/%d/%Y"]:
            try:
                exp_date = datetime.strptime(expiration, fmt)
                break
            except ValueError:
                continue
        
        if not exp_date:
            raise ValueError(f"Could not parse expiration date: {expiration}")
        
        # Format: YYMMDD
        date_str = exp_date.strftime("%y%m%d")
        
        # P for PUT, C for CALL
        type_char = "P" if option_type.upper() == "PUT" else "C"
        
        # Strike * 1000, padded to 8 digits
        strike_int = int(strike * 1000)
        strike_str = f"{strike_int:08d}"
        
        return f"{symbol.upper()}{date_str}{type_char}{strike_str}"

    def get_contract_quote(self, symbol: str, expiration: str, strike: float, option_type: str) -> ContractQuote:
        """
        Get real-time quote for a specific option contract from StockNear's contract lookup page.
        
        Uses interactive dropdown selection since URL parameters don't trigger data load.
        
        Args:
            symbol: Stock ticker (e.g., "BEPC")
            expiration: Date string like "Mar 20, 2026" (must match StockNear format)
            strike: Strike price (e.g., 35.00)
            option_type: "CALL" or "PUT"
        
        Returns:
            ContractQuote with bid, ask, mid, last, Greeks, etc.
        """
        contract_id = self._build_contract_id(symbol, expiration, option_type, strike)
        
        logger.info("Fetching contract quote for %s via dropdown selection (contract_id=%s)", symbol, contract_id)
        
        # Navigate to contract lookup page (without URL params)
        url_path = f"/stocks/{symbol.lower()}/options/contract-lookup"
        self.navigate(url_path)
        
        # Wait for page to load
        self.page.wait_for_timeout(2000)
        
        # Select expiration date from dropdown
        try:
            # Click on the expiration dropdown
            exp_dropdown = self.page.locator('select:has-text("Expiration"), [data-testid="expiration-select"], button:has-text("Expiration")').first
            if exp_dropdown.count() == 0:
                # Try finding by looking for a dropdown near "Date Expiration" label
                exp_dropdown = self.page.locator('text=Date Expiration').locator('..').locator('select, button').first
            
            if exp_dropdown.count() > 0:
                exp_dropdown.click()
                self.page.wait_for_timeout(500)
                # Click on the specific expiration option
                self.page.locator(f'text="{expiration}"').first.click()
                self.page.wait_for_timeout(500)
                logger.debug("Selected expiration: %s", expiration)
        except Exception as e:
            logger.warning("Could not select expiration dropdown: %s", e)
        
        # Select strike price
        try:
            strike_str = str(int(strike)) if strike == int(strike) else str(strike)
            strike_dropdown = self.page.locator('select:has-text("Strike"), [data-testid="strike-select"]').first
            if strike_dropdown.count() == 0:
                strike_dropdown = self.page.locator('text=Strike Price').locator('..').locator('select, input').first
            
            if strike_dropdown.count() > 0:
                strike_dropdown.click()
                self.page.wait_for_timeout(500)
                self.page.locator(f'text="{strike_str}"').first.click()
                self.page.wait_for_timeout(500)
                logger.debug("Selected strike: %s", strike_str)
        except Exception as e:
            logger.warning("Could not select strike dropdown: %s", e)
        
        # Select option type (Call/Put)
        try:
            type_text = "Call" if option_type.upper() == "CALL" else "Put"
            type_dropdown = self.page.locator('select:has-text("Option Type"), [data-testid="type-select"]').first
            if type_dropdown.count() == 0:
                type_dropdown = self.page.locator('text=Option Type').locator('..').locator('select, button').first
            
            if type_dropdown.count() > 0:
                type_dropdown.click()
                self.page.wait_for_timeout(500)
                self.page.locator(f'text="{type_text}"').first.click()
                self.page.wait_for_timeout(500)
                logger.debug("Selected option type: %s", type_text)
        except Exception as e:
            logger.warning("Could not select option type dropdown: %s", e)
        
        # Wait for price data to load after selections
        try:
            self.page.wait_for_function(
                """() => {
                    const text = document.body.innerText;
                    return text.includes('Last') || 
                           text.includes('Bid') || 
                           text.includes('Ask') ||
                           text.includes('Volume') ||
                           text.includes('No data');
                }""",
                timeout=15000
            )
        except Exception as e:
            logger.warning("Timeout waiting for contract data to load for %s: %s", contract_id, e)
        
        self.page.wait_for_timeout(2000)
        
        content = self.get_page_text()
        
        quote = ContractQuote(
            symbol=symbol.upper(),
            strike=strike,
            option_type=option_type.upper(),
            expiration=expiration,
            contract_id=contract_id,
            raw_content=content
        )
        
        logger.debug("Contract page content length: %d chars", len(content))
        
        # Parse price data - StockNear uses newline-separated format:
        # Last\n0.55
        # Bid\n0.3
        # Mid\n0.60
        # Ask\n0.9
        
        # Last price
        last_match = re.search(r'\bLast\s*[\n\r]+\s*\$?([\d.]+)', content, re.IGNORECASE)
        if last_match:
            quote.last = float(last_match.group(1))
            logger.debug("Parsed Last: %s", quote.last)
        
        # Bid price
        bid_match = re.search(r'\bBid\s*[\n\r]+\s*\$?([\d.]+)', content, re.IGNORECASE)
        if bid_match:
            quote.bid = float(bid_match.group(1))
            logger.debug("Parsed Bid: %s", quote.bid)
        
        # Mid price
        mid_match = re.search(r'\bMid\s*[\n\r]+\s*\$?([\d.]+)', content, re.IGNORECASE)
        if mid_match:
            quote.mid = float(mid_match.group(1))
            logger.debug("Parsed Mid: %s", quote.mid)
        
        # Ask price
        ask_match = re.search(r'\bAsk\s*[\n\r]+\s*\$?([\d.]+)', content, re.IGNORECASE)
        if ask_match:
            quote.ask = float(ask_match.group(1))
            logger.debug("Parsed Ask: %s", quote.ask)
        
        # Calculate mid if not found but bid/ask are available
        if quote.mid is None and quote.bid is not None and quote.ask is not None:
            quote.mid = (quote.bid + quote.ask) / 2
            logger.debug("Calculated Mid: %s", quote.mid)
        
        # Open price
        open_match = re.search(r'\bOpen\s*[\n\r]+\s*\$?([\d.]+)', content, re.IGNORECASE)
        if open_match:
            quote.open_price = float(open_match.group(1))
        
        # Volume
        vol_match = re.search(r'\bVolume\s*[\n\r]+\s*([\d,]+)', content, re.IGNORECASE)
        if vol_match:
            quote.volume = int(vol_match.group(1).replace(',', ''))
        
        # Open Interest
        oi_match = re.search(r'Open\s*Interest\s*[\n\r]+\s*([\d,]+)', content, re.IGNORECASE)
        if oi_match:
            quote.open_interest = int(oi_match.group(1).replace(',', ''))
        
        # IV (Implied Volatility)
        iv_match = re.search(r'\bIV\s*[\n\r]+\s*([\d.]+)\s*%', content, re.IGNORECASE)
        if iv_match:
            quote.implied_volatility = float(iv_match.group(1)) / 100
            logger.debug("Parsed IV: %s", quote.implied_volatility)
        
        # Delta
        delta_match = re.search(r'\bDelta\s*[\n\r]+\s*(-?[\d.]+)', content, re.IGNORECASE)
        if delta_match:
            quote.delta = float(delta_match.group(1))
        
        # Gamma
        gamma_match = re.search(r'\bGamma\s*[\n\r]+\s*([\d.]+)', content, re.IGNORECASE)
        if gamma_match:
            quote.gamma = float(gamma_match.group(1))
        
        # Theta
        theta_match = re.search(r'\bTheta\s*[\n\r]+\s*(-?[\d.]+)', content, re.IGNORECASE)
        if theta_match:
            quote.theta = float(theta_match.group(1))
        
        # Vega
        vega_match = re.search(r'\bVega\s*[\n\r]+\s*([\d.]+)', content, re.IGNORECASE)
        if vega_match:
            quote.vega = float(vega_match.group(1))
        
        logger.info(
            "Contract quote for %s: bid=%s, ask=%s, mid=%s, last=%s, IV=%s",
            contract_id, quote.bid, quote.ask, quote.mid, quote.last, quote.implied_volatility
        )
        
        return quote

    def get_contract_quotes_batch(self, contracts: list[dict]) -> list[ContractQuote]:
        """
        Fetch quotes for multiple contracts in a SINGLE browser session.
        
        Even though we fetch each contract's page individually, this is much faster
        than creating a new browser instance for each contract (which takes ~3 seconds each).
        
        For an iron condor (4 legs, same expiration): 
        - Old: 4 browser launches = ~12-16 seconds
        - New: 1 browser, 4 page loads = ~8-10 seconds
        
        Args:
            contracts: List of dicts with keys: symbol, expiration, strike, option_type
        
        Returns:
            List of ContractQuote objects (in same order as input)
        """
        if not contracts:
            return []
        
        logger.info("Batch fetching %d contract quotes in single browser session", len(contracts))
        results = []
        
        for i, contract in enumerate(contracts):
            symbol = contract["symbol"].upper()
            expiration = contract["expiration"]
            strike = contract["strike"]
            option_type = contract["option_type"].upper()
            
            try:
                contract_id = self._build_contract_id(symbol, expiration, option_type, strike)
                logger.debug("Fetching contract %d/%d: %s", i + 1, len(contracts), contract_id)
                
                # Navigate to contract lookup page
                url_path = f"/stocks/{symbol.lower()}/options/contract-lookup?contract={contract_id}"
                self._rate_limit()
                self.page.goto(f"{self.base_url}{url_path}", wait_until="networkidle")
                
                # Wait for the page to load - look for price-related text
                try:
                    self.page.wait_for_function(
                        """() => {
                            const text = document.body.innerText;
                            return text.includes('Bid') || text.includes('Ask') || 
                                   text.includes('Last') || text.includes('Mid') ||
                                   text.includes('No data') || text.includes('not found');
                        }""",
                        timeout=10000
                    )
                except Exception as e:
                    logger.debug("Wait timeout for %s: %s", contract_id, e)
                
                # Give JS time to render
                self.page.wait_for_timeout(1500)
                
                content = self.get_page_text()
                logger.debug("Contract %s page length: %d chars", contract_id, len(content))
                
                # Log first 500 chars for debugging if content is short
                if len(content) < 1500:
                    logger.warning("Short page content for %s: %s", contract_id, content[:500])
                
                quote = ContractQuote(
                    symbol=symbol,
                    strike=strike,
                    option_type=option_type,
                    expiration=expiration,
                    contract_id=contract_id,
                    raw_content=content
                )
                
                # Parse prices - StockNear format: "Label\nValue" on separate lines
                # Try both newline format and inline format
                
                # Bid
                bid_match = re.search(r'\bBid\s*[\n\r:]+\s*\$?([\d.]+)', content, re.IGNORECASE)
                if bid_match:
                    quote.bid = float(bid_match.group(1))
                
                # Ask
                ask_match = re.search(r'\bAsk\s*[\n\r:]+\s*\$?([\d.]+)', content, re.IGNORECASE)
                if ask_match:
                    quote.ask = float(ask_match.group(1))
                
                # Mid
                mid_match = re.search(r'\bMid\s*[\n\r:]+\s*\$?([\d.]+)', content, re.IGNORECASE)
                if mid_match:
                    quote.mid = float(mid_match.group(1))
                
                # Last
                last_match = re.search(r'\bLast\s*[\n\r:]+\s*\$?([\d.]+)', content, re.IGNORECASE)
                if last_match:
                    quote.last = float(last_match.group(1))
                
                # Calculate mid if not found
                if quote.mid is None and quote.bid is not None and quote.ask is not None:
                    quote.mid = (quote.bid + quote.ask) / 2
                
                # Volume
                vol_match = re.search(r'\bVolume\s*[\n\r:]+\s*([\d,]+)', content, re.IGNORECASE)
                if vol_match:
                    quote.volume = int(vol_match.group(1).replace(',', ''))
                
                # Open Interest
                oi_match = re.search(r'Open\s*Interest\s*[\n\r:]+\s*([\d,]+)', content, re.IGNORECASE)
                if oi_match:
                    quote.open_interest = int(oi_match.group(1).replace(',', ''))
                
                # IV
                iv_match = re.search(r'\bIV\s*[\n\r:]+\s*([\d.]+)\s*%', content, re.IGNORECASE)
                if iv_match:
                    quote.implied_volatility = float(iv_match.group(1)) / 100
                
                # Delta
                delta_match = re.search(r'\bDelta\s*[\n\r:]+\s*(-?[\d.]+)', content, re.IGNORECASE)
                if delta_match:
                    quote.delta = float(delta_match.group(1))
                
                logger.info(
                    "Quote %s: bid=%s, ask=%s, mid=%s, last=%s",
                    contract_id, quote.bid, quote.ask, quote.mid, quote.last
                )
                results.append(quote)
                
            except Exception as e:
                logger.error("Error fetching contract %d (%s): %s", i, contract.get("symbol"), e)
                results.append(ContractQuote(
                    symbol=symbol,
                    strike=strike,
                    option_type=option_type,
                    expiration=expiration,
                    contract_id=""
                ))
        
        success_count = sum(1 for q in results if q.bid is not None or q.ask is not None or q.last is not None)
        logger.info("Batch complete: %d/%d quotes with data", success_count, len(results))
        return results

    def get_contract_quote_via_api(self, symbol: str, expiration: str, strike: float, option_type: str) -> ContractQuote:
        """
        Get real-time quote for a specific option contract using direct API call.
        
        This is MUCH faster than browser scraping (~500ms vs 5-10s) and more reliable.
        The StockNear API requires UPPERCASE for both ticker and contract symbols.
        
        Args:
            symbol: Stock ticker (e.g., "AAPL")
            expiration: Date string like "Jan 30, 2026" or "2026-01-30"
            strike: Strike price (e.g., 250.00)
            option_type: "CALL" or "PUT"
        
        Returns:
            ContractQuote with bid, ask, mid, last, Greeks, etc.
        """
        import requests
        from datetime import datetime
        
        contract_id = self._build_contract_id(symbol, expiration, option_type, strike)
        
        # CRITICAL: API requires UPPERCASE for both ticker and contract
        ticker_upper = symbol.upper()
        contract_upper = contract_id.upper()
        
        logger.info("Fetching contract quote via API: ticker=%s, contract=%s", ticker_upper, contract_upper)
        
        # Make direct API call with auth cookies
        api_url = "https://stocknear.com/api/options-contract-history"
        
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:132.0) Gecko/20100101 Firefox/132.0",
            "Origin": "https://stocknear.com",
            "Referer": f"https://stocknear.com/stocks/{symbol.lower()}/options/contract-lookup",
        }
        
        # Add cookies from browser context if available
        cookies = {}
        if hasattr(self, 'context') and self.context:
            try:
                browser_cookies = self.context.cookies()
                for c in browser_cookies:
                    if "stocknear" in c.get("domain", ""):
                        cookies[c["name"]] = c["value"]
            except:
                pass
        
        payload = {
            "ticker": ticker_upper,
            "contract": contract_upper
        }
        
        try:
            response = requests.post(api_url, json=payload, headers=headers, cookies=cookies, timeout=10)
            response.raise_for_status()
            data = response.json()
        except Exception as e:
            logger.error("API request failed for %s: %s", contract_id, e)
            return ContractQuote(
                symbol=ticker_upper,
                strike=strike,
                option_type=option_type.upper(),
                expiration=expiration,
                contract_id=contract_id,
                raw_content=str(e)
            )
        
        # Parse the response - it returns a history array, we want the most recent entry
        quote = ContractQuote(
            symbol=ticker_upper,
            strike=strike,
            option_type=option_type.upper(),
            expiration=expiration,
            contract_id=contract_id,
            raw_content=json.dumps(data)
        )
        
        if isinstance(data, dict) and "history" in data:
            history = data.get("history", [])
            if history:
                # Get the most recent entry (last in the list)
                latest = history[-1]
                
                quote.bid = latest.get("close_bid")
                quote.ask = latest.get("close_ask")
                quote.last = latest.get("close") or latest.get("mark")
                quote.open_price = latest.get("open")
                quote.volume = latest.get("volume")
                quote.open_interest = latest.get("open_interest")
                quote.implied_volatility = latest.get("implied_volatility")
                quote.delta = latest.get("delta")
                quote.gamma = latest.get("gamma")
                quote.theta = latest.get("theta")
                quote.vega = latest.get("vega")
                
                # Calculate mid if we have bid/ask
                if quote.bid is not None and quote.ask is not None:
                    quote.mid = (quote.bid + quote.ask) / 2
                elif quote.last is not None:
                    quote.mid = quote.last
                
                logger.info(
                    "API quote for %s: bid=%s, ask=%s, mid=%s, last=%s, IV=%s, delta=%s",
                    contract_id, quote.bid, quote.ask, quote.mid, quote.last,
                    quote.implied_volatility, quote.delta
                )
        elif isinstance(data, list):
            # API might return array directly (empty = no data)
            if data:
                latest = data[-1]
                quote.bid = latest.get("close_bid")
                quote.ask = latest.get("close_ask")
                quote.last = latest.get("close") or latest.get("mark")
                quote.implied_volatility = latest.get("implied_volatility")
                quote.delta = latest.get("delta")
                quote.gamma = latest.get("gamma")
                quote.theta = latest.get("theta")
                quote.vega = latest.get("vega")
                
                if quote.bid is not None and quote.ask is not None:
                    quote.mid = (quote.bid + quote.ask) / 2
            else:
                logger.warning("API returned empty array for %s - contract may not exist", contract_id)
        else:
            logger.warning("Unexpected API response format for %s: %s", contract_id, type(data))
        
        return quote

    def get_contract_quotes_via_api(self, contracts: list[dict]) -> list[ContractQuote]:
        """
        Fetch quotes for multiple contracts using direct API calls.
        
        Uses sequential fetching to avoid threading issues with Playwright.
        Still much faster than browser scraping since each API call is ~500ms.
        
        For an iron condor (4 legs): 
        - Browser scraping: ~20-40 seconds
        - Direct API (sequential): ~2-3 seconds
        
        Args:
            contracts: List of dicts with keys: symbol, expiration, strike, option_type
        
        Returns:
            List of ContractQuote objects (in same order as input)
        """
        import requests
        
        if not contracts:
            return []
        
        logger.info("Batch fetching %d contract quotes via direct API (sequential)", len(contracts))
        
        results = []
        for i, contract in enumerate(contracts):
            try:
                quote = self.get_contract_quote_via_api(
                    symbol=contract["symbol"],
                    expiration=contract["expiration"],
                    strike=contract["strike"],
                    option_type=contract["option_type"]
                )
                results.append(quote)
            except Exception as e:
                logger.error("Error fetching contract %d (%s): %s", i, contract.get("symbol"), e)
                # Create empty quote on error
                contract_id = self._build_contract_id(
                    contract["symbol"],
                    contract["expiration"],
                    contract["option_type"],
                    contract["strike"]
                )
                results.append(ContractQuote(
                    symbol=contract["symbol"].upper(),
                    strike=contract["strike"],
                    option_type=contract["option_type"].upper(),
                    expiration=contract["expiration"],
                    contract_id=contract_id
                ))
        
        success_count = sum(1 for q in results if q.bid is not None or q.last is not None)
        logger.info("API batch complete: %d/%d quotes with data", success_count, len(results))
        return results

    def get_available_strikes(self, symbol: str, return_raw_html: bool = False) -> dict:
        """
        Get available strikes and expirations for a symbol from StockNear's contract-lookup page.
        
        Uses Playwright to interact with the dropdowns and extract available options.
        
        Returns:
            dict with structure:
            {
                "expirations": ["Jan 30, 2026", "Feb 7, 2026", ...],
                "strikes": [620, 625, 630, ...],  # All strikes for first expiration
                "current_price": 659.00
            }
        """
        import re
        
        logger.info("Fetching available strikes for %s", symbol)
        
        url = f"/stocks/{symbol.lower()}/options/contract-lookup"
        self.navigate(url)
        self.page.wait_for_timeout(3000)
        
        result = {
            "expirations": [],
            "strikes": [],
            "current_price": None,
            "_debug": {}
        }
        
        # Get current price from page
        content = self.get_page_text()
        price_match = re.search(r'\$\s*(\d+\.?\d*)', content)
        if price_match:
            result["current_price"] = float(price_match.group(1))
        
        try:
            # Click on Date Expiration dropdown to get expirations
            date_btn = self.page.locator('button:has-text("Jan"):has-text("20")').first
            if date_btn.count() == 0:
                date_btn = self.page.locator('button:has-text("Feb"):has-text("20")').first
            if date_btn.count() == 0:
                date_btn = self.page.locator('button:has-text("Mar"):has-text("20")').first
            
            if date_btn.count() > 0:
                date_btn.click()
                self.page.wait_for_timeout(500)
                
                # Extract expiration dates from menu items
                menu_items = self.page.locator('[role="menuitem"]').all()
                for item in menu_items[:30]:  # Limit to first 30
                    try:
                        text = item.text_content()
                        if text and re.match(r'^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)', text.strip()):
                            result["expirations"].append(text.strip())
                    except:
                        pass
                
                # Close menu by pressing Escape
                self.page.keyboard.press("Escape")
                self.page.wait_for_timeout(300)
                
                logger.info("Found %d expirations", len(result["expirations"]))
        except Exception as e:
            result["_debug"]["expiration_error"] = str(e)
            logger.warning("Failed to get expirations: %s", e)
        
        try:
            # Click on Strike dropdown to get strikes
            # The strike button shows just a number like "660" - look for span with numeric text
            # First try to find the strike label section
            strike_section = self.page.locator('text=Strike').first
            
            if strike_section.count() > 0:
                # Find the button in or near this section - it's a sibling button with a number
                # Look for button with a span containing a 3-digit number
                strike_btn = self.page.locator('button:has(span)').filter(
                    has=self.page.locator('span.truncate').filter(has_text=re.compile(r'^\d{2,4}$'))
                ).first
                
                if strike_btn.count() == 0:
                    # Alternative: just find any button with a 3-digit number as text
                    strike_btn = self.page.locator('button').filter(has_text=re.compile(r'^\\d{3}$')).first
                
                if strike_btn.count() == 0:
                    # Try finding by visible text that looks like a strike price
                    buttons = self.page.locator('button').all()
                    for btn in buttons:
                        try:
                            text = btn.text_content()
                            if text and re.match(r'^\s*\d{2,4}(\.\d+)?\s*$', text.strip().split('\n')[0]):
                                strike_btn = btn
                                logger.debug("Found strike button with text: %s", text.strip()[:20])
                                break
                        except:
                            pass
            else:
                strike_btn = None
                logger.warning("Could not find Strike label on page")
            
            if strike_btn and (not hasattr(strike_btn, 'count') or strike_btn.count() > 0):
                strike_btn.click()
                self.page.wait_for_timeout(800)
                
                # Take screenshot of open dropdown
                try:
                    self.screenshot(f"/app/data/debug_strikes_dropdown_{symbol.lower()}.png")
                except:
                    pass
                
                # Extract strikes from menu items
                menu_items = self.page.locator('[role="menuitem"]').all()
                result["_debug"]["menu_items_count"] = len(menu_items)
                
                for item in menu_items[:250]:  # Limit to first 250 strikes
                    try:
                        text = item.text_content()
                        if text:
                            text = text.strip()
                            # Match strike values like "660", "662.5", etc.
                            if re.match(r'^\d+\.?\d*$', text):
                                strike = float(text)
                                result["strikes"].append(strike)
                    except:
                        pass
                
                # Close menu
                self.page.keyboard.press("Escape")
                self.page.wait_for_timeout(300)
                
                logger.info("Found %d strikes from %d menu items", len(result["strikes"]), len(menu_items))
            else:
                result["_debug"]["strike_btn_not_found"] = True
                logger.warning("Could not find strike button")
        except Exception as e:
            result["_debug"]["strike_error"] = str(e)
            logger.warning("Failed to get strikes: %s", e)
        
        # Sort strikes
        result["strikes"] = sorted(list(set(result["strikes"])))
        
        if return_raw_html:
            result["_debug"]["html_sample"] = self.page.content()[:3000]
        
        return result

    def screenshot(self, path: str = "screenshot.png"):
        """Take a screenshot of the current page."""
        self.page.screenshot(path=path)
        return path


def main():
    """CLI interface for the scraper."""
    if len(sys.argv) < 2:
        print("Usage: python stocknear.py <command> [args]")
        print("\nStock Commands:")
        print("  stock <symbol>        - Get stock overview")
        print("  ratings <symbol>      - Get analyst ratings")
        print("\nOptions Commands:")
        print("  options-overview <symbol>  - Get options overview (IV, OI, volume)")
        print("  options-chain <symbol>     - Get options chain with Greeks")
        print("  max-pain <symbol>     - Get max pain analysis")
        print("  gex <symbol>          - Get gamma exposure (GEX)")
        print("  dex <symbol>          - Get delta exposure (DEX)")
        print("  options [symbol]      - Get options flow (unusual orders)")
        print("  darkpool [symbol]     - Get dark pool data")
        sys.exit(1)

    command = sys.argv[1].lower()

    with StockNearScraper() as scraper:
        if command == "stock" and len(sys.argv) > 2:
            result = scraper.get_stock_overview(sys.argv[2])
            result = {"symbol": result.symbol, "price": result.price, "change": result.change}
        elif command == "options-overview" and len(sys.argv) > 2:
            result = scraper.get_options_overview(sys.argv[2])
            result = {
                "symbol": result.symbol,
                "iv_rank": result.iv_rank,
                "iv_percentile": result.iv_percentile,
                "implied_volatility": result.implied_volatility,
                "put_call_ratio": result.put_call_ratio,
                "total_volume": result.total_volume,
                "total_open_interest": result.total_open_interest
            }
        elif command == "max-pain" and len(sys.argv) > 2:
            result = scraper.get_max_pain(sys.argv[2])
            result = {"symbol": result.symbol, "max_pain": result.max_pain}
        elif command == "options-chain" and len(sys.argv) > 2:
            expiration = sys.argv[3] if len(sys.argv) > 3 else None
            result = scraper.get_options_chain(sys.argv[2], expiration)
        elif command == "gex" and len(sys.argv) > 2:
            result = scraper.get_options_gex(sys.argv[2])
        elif command == "dex" and len(sys.argv) > 2:
            result = scraper.get_options_dex(sys.argv[2])
        elif command == "options":
            symbol = sys.argv[2] if len(sys.argv) > 2 else None
            result = scraper.get_options_flow(symbol)
        elif command == "darkpool":
            symbol = sys.argv[2] if len(sys.argv) > 2 else None
            result = scraper.get_dark_pool(symbol)
        elif command == "ratings" and len(sys.argv) > 2:
            result = scraper.get_analyst_ratings(sys.argv[2])
        else:
            print(f"Unknown command or missing arguments: {command}")
            sys.exit(1)

        print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
