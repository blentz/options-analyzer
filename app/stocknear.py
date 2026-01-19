"""
StockNear browser scraper using Playwright with LibreWolf cookies.
Provides authenticated access to stocknear.com data.

Configuration is loaded from environment variables via app.config.
"""

import json
import re
import sqlite3
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from playwright.sync_api import sync_playwright, Page, BrowserContext

from app.config import settings


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


def extract_browser_cookies(profile_path: str, domain_filter: str = "stocknear.com") -> list[dict]:
    """Extract cookies from Firefox/LibreWolf cookies.sqlite for a specific domain."""
    cookies_db = Path(profile_path) / "cookies.sqlite"

    if not cookies_db.exists():
        print(f"Warning: cookies.sqlite not found at {cookies_db}", file=sys.stderr)
        return []

    # Copy the database to avoid locking issues with running browser
    with tempfile.NamedTemporaryFile(delete=False, suffix=".sqlite") as tmp:
        shutil.copy(cookies_db, tmp.name)
        tmp_path = tmp.name

    try:
        conn = sqlite3.connect(tmp_path)
        cursor = conn.cursor()

        # Firefox/LibreWolf cookie schema
        cursor.execute("""
            SELECT name, value, host, path, expiry, isSecure, isHttpOnly, sameSite
            FROM moz_cookies
            WHERE host LIKE ?
        """, (f"%{domain_filter}%",))

        cookies = []
        for row in cursor.fetchall():
            name, value, host, path, expiry, is_secure, is_http_only, same_site = row

            # Map Firefox sameSite values to Playwright format
            same_site_map = {0: "None", 1: "Lax", 2: "Strict"}

            cookie = {
                "name": name,
                "value": value,
                "domain": host,
                "path": path,
                "secure": bool(is_secure),
                "httpOnly": bool(is_http_only),
                "sameSite": same_site_map.get(same_site, "Lax")
            }
            # Firefox stores expiry in seconds since epoch
            if expiry and expiry > 0:
                # Convert from microseconds to seconds if needed (13+ digits = ms/us)
                if expiry > 10000000000000:  # More than 13 digits = microseconds
                    cookie["expires"] = expiry // 1000000
                elif expiry > 10000000000:  # 13 digits = milliseconds
                    cookie["expires"] = expiry // 1000
                else:
                    cookie["expires"] = expiry

            cookies.append(cookie)

        conn.close()
        return cookies
    finally:
        Path(tmp_path).unlink(missing_ok=True)


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
        self.rate_limit_delay = rate_limit_delay if rate_limit_delay is not None else settings.stocknear_rate_limit_delay
        self.base_url = settings.stocknear_base_url
        self.profile_path = settings.stocknear_browser_profile_path
        
        self.playwright = None
        self.context: BrowserContext = None
        self.page: Page = None
        self._last_request_time: float = 0

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def start(self):
        """Start browser and inject cookies for authenticated session."""
        self.playwright = sync_playwright().start()

        # Launch fresh browser, then inject cookies from profile
        browser = self.playwright.firefox.launch(headless=self.headless)
        self.context = browser.new_context(viewport={"width": 1280, "height": 800})

        # Extract and add cookies if profile path is configured
        if self.profile_path:
            cookies = extract_browser_cookies(self.profile_path, "stocknear.com")
            if cookies:
                self.context.add_cookies(cookies)

        self.page = self.context.new_page()

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

    def navigate(self, path: str) -> str:
        """Navigate to a StockNear page and return the page content."""
        self._rate_limit()
        url = f"{self.base_url}{path}" if path.startswith("/") else f"{self.base_url}/{path}"
        self.page.goto(url, wait_until="networkidle")
        return self.page.content()

    def get_page_text(self) -> str:
        """Get visible text content from current page."""
        return self.page.inner_text("body")

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
        self.navigate(f"/stocks/{symbol.lower()}/options")
        self.page.wait_for_timeout(2000)

        content = self.get_page_text()
        data = OptionsData(symbol=symbol.upper(), raw_content=content)

        # Parse IV Rank - look for pattern like "IV Rank 45.2%"
        iv_rank_match = re.search(r'IV\s*Rank[:\s]*(\d+\.?\d*)\s*%?', content, re.IGNORECASE)
        if iv_rank_match:
            data.iv_rank = float(iv_rank_match.group(1))

        # Parse IV Percentile
        iv_pct_match = re.search(r'IV\s*Percentile[:\s]*(\d+\.?\d*)\s*%?', content, re.IGNORECASE)
        if iv_pct_match:
            data.iv_percentile = float(iv_pct_match.group(1))

        # Parse Implied Volatility (current IV)
        iv_match = re.search(r'(?:Implied\s*Volatility|IV)[:\s]*(\d+\.?\d*)\s*%', content, re.IGNORECASE)
        if iv_match:
            data.implied_volatility = float(iv_match.group(1)) / 100  # Convert to decimal

        # Parse Put/Call Ratio
        pcr_match = re.search(r'Put[/\s]*Call\s*Ratio[:\s]*(\d+\.?\d*)', content, re.IGNORECASE)
        if pcr_match:
            data.put_call_ratio = float(pcr_match.group(1))

        # Parse Total Volume
        vol_match = re.search(r'Total\s*(?:Options\s*)?Volume[:\s]*([\d,]+[KMB]?)', content, re.IGNORECASE)
        if vol_match:
            data.total_volume = int(self._parse_number(vol_match.group(1)) or 0)

        # Parse Total Open Interest
        oi_match = re.search(r'(?:Total\s*)?Open\s*Interest[:\s]*([\d,]+[KMB]?)', content, re.IGNORECASE)
        if oi_match:
            data.total_open_interest = int(self._parse_number(oi_match.group(1)) or 0)

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
