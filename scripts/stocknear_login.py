"""One-time stocknear login + financials DOM capture into a persistent profile.

This opens a VISIBLE Playwright Firefox using a dedicated, persistent profile
directory (``STOCKNEAR_USER_DATA_DIR``). You log into stocknear and solve any
Cloudflare "verify you are human" challenge once, in this window. The session
and clearance are saved to that profile, so the app — pointed at the same
``STOCKNEAR_USER_DATA_DIR`` — reuses this exact authenticated browser for all
financials reads. No cookie injection, no re-challenge.

While here, it also saves the rendered financials/balance-sheet HTML to
tests/fixtures/ so the DOM-table parser can be built and unit-tested.

Run (from repo root):

    STOCKNEAR_USER_DATA_DIR="$HOME/.cache/options-analyzer/sn-firefox" \
    STOCKNEAR_HEADLESS=false \
    STOCKNEAR_BASE_URL=https://www.stocknear.com \
    uv run python scripts/stocknear_login.py AAPL

The app must NOT be running against the same profile at the same time (a
profile can only be open in one browser).
"""

import sys
from pathlib import Path

from app.config import settings
from app.stocknear import StockNearScraper

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures"
PAGES = {
    "income": "/stocks/{sym}/financials",
    "balance": "/stocks/{sym}/financials/balance-sheet",
}


def main(tickers: list[str]) -> None:
    if not settings.stocknear_user_data_dir:
        print("ERROR: set STOCKNEAR_USER_DATA_DIR to a dedicated profile directory first.")
        raise SystemExit(2)

    sc = StockNearScraper(headless=False)
    sc.start()
    try:
        if not sc.is_logged_in():
            print("\n>>> Log into stocknear in the browser window (and solve the Cloudflare\n"
                  ">>> checkbox if it appears). Waiting up to 5 minutes…")
            if not sc.wait_for_login(timeout_seconds=300):
                print("Not logged in — aborting.")
                return
        print(">>> Authenticated. Session saved to:", settings.stocknear_user_data_dir)

        FIXTURES.mkdir(parents=True, exist_ok=True)
        for tk in tickers:
            sym = tk.lower()
            for label, path in PAGES.items():
                url = f"{settings.stocknear_base_url}{path.format(sym=sym)}"
                print(f"\n→ {url}")
                sc._rate_limit()
                sc.page.goto(url, wait_until="domcontentloaded")
                try:
                    sc.page.wait_for_selector("text=Net Income", timeout=60_000)
                except Exception:
                    try:
                        sc.page.wait_for_selector("table", timeout=10_000)
                    except Exception:
                        print("  ! no table detected — saving whatever rendered.")
                sc.page.wait_for_timeout(2500)
                out = FIXTURES / f"{sym}_{label}_page.html"
                out.write_text(sc.page.content(), encoding="utf-8")
                print(f"  saved {out}  ({out.stat().st_size} bytes)")
    finally:
        sc.close()


if __name__ == "__main__":
    main(sys.argv[1:] or ["AAPL"])
