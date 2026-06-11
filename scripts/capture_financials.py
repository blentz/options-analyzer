"""Headful capture of StockNear financials pages for building the DOM scraper.

Runs Firefox VISIBLY so you (a human) can solve the Cloudflare "verify you are
human" Turnstile challenge when it appears. Once the financials table renders,
the rendered HTML is saved to tests/fixtures/ so the DOM parser can be written
and unit-tested against real markup — no automated bot-challenge bypass.

Usage (from repo root, so cookies/auth load from your profile):

    STOCKNEAR_BROWSER_PROFILE_PATH=/home/blentz/.mozilla/firefox/rtzwitzk.default-release \
    uv run python scripts/capture_financials.py AAPL

When the browser window opens and shows the Cloudflare checkbox, click it. The
script waits up to 3 minutes for a financial table to appear, then saves the
HTML and exits. Repeat for a couple of tickers if you like.
"""

import sys
from pathlib import Path

from app.config import settings

settings.stocknear_base_url = "https://www.stocknear.com"  # canonical host for the session

from app.stocknear import StockNearScraper

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures"
PAGES = {
    "income": "/stocks/{sym}/financials",
    "balance": "/stocks/{sym}/financials/balance-sheet",
}


def capture(ticker: str) -> None:
    sym = ticker.lower()
    FIXTURES.mkdir(parents=True, exist_ok=True)
    sc = StockNearScraper(headless=False)
    sc.start()
    try:
        for label, path in PAGES.items():
            url = f"{settings.stocknear_base_url}{path.format(sym=sym)}"
            print(f"\n→ {url}\n  Solve the Cloudflare checkbox if it appears…")
            sc.page.goto(url, wait_until="domcontentloaded")
            # Give the human up to 3 minutes to pass Turnstile; then wait for a
            # rendered statement table (a row containing a known line item).
            try:
                sc.page.wait_for_selector("text=Net Income", timeout=180_000)
            except Exception:
                try:
                    sc.page.wait_for_selector("table", timeout=10_000)
                except Exception:
                    print("  ! No table detected — saving whatever rendered.")
            sc.page.wait_for_timeout(2500)
            out = FIXTURES / f"{sym}_{label}_page.html"
            out.write_text(sc.page.content(), encoding="utf-8")
            print(f"  saved {out}  ({out.stat().st_size} bytes)")
    finally:
        sc.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: uv run python scripts/capture_financials.py TICKER [TICKER ...]")
        raise SystemExit(2)
    for tk in sys.argv[1:]:
        capture(tk)
