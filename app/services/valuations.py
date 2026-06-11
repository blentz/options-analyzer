"""Reality Gap (RG) valuation orchestration.

Glues the three pieces together for a ticker:
  1. fundamentals from StockNear  (app/services/stocknear_financials.py)
  2. annual CPI from BLS          (app/services/cpi_service.py)
  3. the pure RG math             (app/services/rg_math.py)

Returns a plain JSON-serializable dict for the /valuations page and API. RG can
be ∞ ("not fundamentally covered", paper §3.5); since JSON has no infinity we
represent that as value=None + covered=False and a formatted "∞".
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.cpi_service import get_cpi_by_year
from app.services.rg_math import SENSITIVITY_NS, compute_rg_sensitivity, format_rg
from app.services.stocknear_service import get_symbol_fundamentals

logger = logging.getLogger(__name__)

RG_WINDOW = 10  # RG10: ten-year smoothed earnings (paper §3.4)


async def get_valuation(db: AsyncSession, symbol: str, force_refresh: bool = False) -> dict:
    """Compute the Reality Gap report for `symbol`.

    `status` is one of: "ok", "insufficient_data", "error". Callers render the
    message for the non-ok cases.
    """
    symbol = symbol.upper()
    fundamentals = await get_symbol_fundamentals(db, symbol, force_refresh)

    if fundamentals is None:
        return {
            "symbol": symbol,
            "status": "error",
            "message": "Could not fetch fundamentals from StockNear "
                       "(check authentication / network).",
        }

    if not fundamentals.has_minimum_inputs:
        missing = []
        if fundamentals.market_cap is None:
            missing.append("market cap")
        if fundamentals.book_equity is None:
            missing.append("book equity")
        if not fundamentals.net_income_by_year:
            missing.append("net income history")
        # Nothing at all came back ⇒ almost always StockNear blocking automated
        # access (Cloudflare "verify you are human" / financials __data.json 403)
        # rather than a genuinely data-less ticker.
        if len(missing) == 3:
            message = (
                "StockNear returned no financials for this ticker — most likely a "
                "Cloudflare human-verification block on automated access, or an "
                "unrecognized ticker. Run the scraper headfully and solve the "
                "challenge, or check the symbol."
            )
        else:
            message = "Missing inputs: " + ", ".join(missing) + "."
        return {
            "symbol": symbol,
            "status": "insufficient_data",
            "message": message,
            "market_cap": fundamentals.market_cap,
            "book_equity": fundamentals.book_equity,
            "years_available": len(fundamentals.net_income_by_year),
        }

    years = set(fundamentals.net_income_by_year)
    cpi = await get_cpi_by_year(db, years)  # {} ⇒ degrade to nominal
    cpi_arg = cpi or None

    results = compute_rg_sensitivity(
        market_cap=fundamentals.market_cap,
        book_equity=fundamentals.book_equity,
        goodwill=fundamentals.goodwill or 0.0,
        intangibles=fundamentals.intangibles or 0.0,
        net_income_by_year=fundamentals.net_income_by_year,
        cpi_by_year=cpi_arg,
        ns=SENSITIVITY_NS,
        window=RG_WINDOW,
    )

    base = results.get(10) or next(iter(results.values()))
    comp = base.components
    short_window = comp.years_used < RG_WINDOW

    def serialize(r) -> dict:
        return {
            "n": r.n,
            "covered": r.covered,
            "value": (r.value if r.covered else None),
            "capitalized_earnings": r.components.capitalized_earnings,
            "fundamental_base": r.components.fundamental_base,
            "formatted": format_rg(r, short_window=short_window),
        }

    span = sorted(years)
    return {
        "symbol": symbol,
        "status": "ok",
        "market_cap": fundamentals.market_cap,
        "book_equity": fundamentals.book_equity,
        "goodwill": fundamentals.goodwill or 0.0,
        "intangibles": fundamentals.intangibles or 0.0,
        "tangible_equity": comp.tangible_equity,
        "smoothed_earnings": comp.smoothed_earnings,
        "years_used": comp.years_used,
        "window": RG_WINDOW,
        "short_window": short_window,
        "year_span": f"{span[0]}–{span[-1]}" if span else "",
        "balance_sheet_year": fundamentals.balance_sheet_year,
        "trend": base.trend,
        "inflation_adjusted": bool(cpi),
        "results": [serialize(results[n]) for n in sorted(results)],
        "net_income_by_year": {str(y): v for y, v in sorted(fundamentals.net_income_by_year.items())},
    }
