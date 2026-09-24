"""Debug router: low-level data-source inspection endpoints.

These return raw MCP payloads, uncached contract quotes and similar
internals. Disabled by default via DebugGateMiddleware in main.py. The
browser page-scraping endpoints were removed with the scraper data path.

All endpoints here were previously inline in main.py. They were extracted
verbatim — only the decorator was swapped from @app.* to @router.* and
the runtime imports that were inline-imported per-call have been moved
to module level where straightforward.

If you add a debug route, add it here, not in main.py. The middleware
will only gate routes under /api/debug.
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db

router = APIRouter(prefix="/api/debug", tags=["debug"])

@router.get("/stocknear/{symbol}")
async def debug_stocknear(symbol: str):
    """
    Debug endpoint to view the raw MCP payloads behind a symbol's data.

    Symbol-level data now comes from the MCP server rather than scraped
    pages, so this dumps the decoded tool payloads instead of page text.
    """
    from app.services.stocknear_mcp import StockNearMCPError, call_tool

    out: dict = {"symbol": symbol.upper()}
    for label, tool in (
        ("options_overview", "get_ticker_options_overview_data"),
        ("quote", "get_ticker_quote"),
    ):
        try:
            out[label] = await call_tool(tool, {"tickers": [symbol.upper()]})
        except StockNearMCPError as exc:
            out[label] = {"error": str(exc)}
    return out


@router.get("/yahoo-option/{symbol}")
async def debug_yahoo_option(
    symbol: str,
    expiration: str,
    strike: float,
    option_type: str
):
    """
    Debug endpoint to test Yahoo Finance options API.
    """
    from app.services.price_service import get_option_quote, get_option_chain
    
    # Get the specific quote
    quote = await get_option_quote(symbol, expiration, strike, option_type)
    
    if quote:
        return {
            "status": "found",
            "contract_symbol": quote.contract_symbol,
            "underlying": quote.underlying,
            "strike": quote.strike,
            "expiration": quote.expiration,
            "option_type": quote.option_type,
            "bid": quote.bid,
            "ask": quote.ask,
            "last": quote.last,
            "volume": quote.volume,
            "open_interest": quote.open_interest,
            "implied_volatility": quote.implied_volatility,
            "in_the_money": quote.in_the_money
        }
    else:
        return {
            "status": "not_found",
            "symbol": symbol,
            "expiration": expiration,
            "strike": strike,
            "option_type": option_type
        }


@router.get("/quote-api/{symbol}")
async def debug_quote_api(symbol: str, expiration: str, strike: float, option_type: str):
    """
    Raw contract quote from StockNear's contract JSON API, uncached.

    Example: /api/debug/quote-api/AAPL?expiration=Jan%2030,%202026&strike=250&option_type=CALL
    """
    from dataclasses import asdict
    from app.services.stocknear_contract_api import StockNearAPIError, fetch_contract_quote

    try:
        return asdict(await fetch_contract_quote(symbol, expiration, strike, option_type))
    except (StockNearAPIError, ValueError) as exc:
        raise HTTPException(502, str(exc))


@router.get("/strikes/{symbol}")
async def debug_strikes(symbol: str):
    """Strikes and expirations from the MCP open-interest table, uncached."""
    from app.services.stocknear_mcp import StockNearMCPError, fetch_strikes

    try:
        result = await fetch_strikes(symbol)
    except StockNearMCPError as exc:
        raise HTTPException(502, str(exc))
    return {
        "symbol": symbol.upper(),
        "expiration_count": len(result["expirations"]),
        "expirations": result["expirations"],
        "strike_count": len(result["strikes"]),
        "strikes": result["strikes"],
    }


@router.get("/assignment-calc")
async def debug_assignment_calc(
    symbol: str,
    strike: float,
    option_type: str,
    days: int,
    current_price: float,
    iv: Optional[float] = None,  # Implied volatility as decimal (e.g., 0.80 for 80%)
    db: AsyncSession = Depends(get_db)
):
    """
    Debug endpoint to trace assignment probability calculation step by step.
    
    This helps verify that _estimate_delta() and calculate_price_at_delta() are consistent.
    
    Example: /api/debug/assignment-calc?symbol=CSIQ&strike=20&option_type=PUT&days=168&current_price=19.77
    
    Expected result: At current_price close to strike, probability should be ~50%.
    The price_at_50pct should be close to strike for reasonable IV levels.
    """
    import math
    from app.services.risk_analysis import (
        _estimate_delta,
        calculate_price_at_delta,
        _calculate_d1_d2,
        _norm_cdf,
        DEFAULT_VOLATILITY,
        DEFAULT_RISK_FREE_RATE,
        CALENDAR_DAYS_PER_YEAR,
    )
    from app.services.stocknear_service import get_options_overview
    
    option_type = option_type.upper()
    symbol = symbol.upper()
    
    # Get IV from StockNear if not provided
    iv_source = "provided"
    volatility = iv
    
    if volatility is None:
        try:
            options_data = await get_options_overview(db, symbol)
            if options_data and options_data.implied_volatility:
                volatility = options_data.implied_volatility
                iv_source = f"stocknear ({volatility:.1%})"
            else:
                volatility = DEFAULT_VOLATILITY
                iv_source = f"default ({DEFAULT_VOLATILITY:.1%})"
        except Exception as e:
            volatility = DEFAULT_VOLATILITY
            iv_source = f"default (error: {str(e)[:50]})"
    
    # Step-by-step calculation
    T = days / CALENDAR_DAYS_PER_YEAR
    sqrt_T = math.sqrt(T)
    
    # Calculate d1 and d2 at current price
    d1, d2 = _calculate_d1_d2(current_price, strike, T, DEFAULT_RISK_FREE_RATE, volatility)
    
    # Calculate assignment probability using _estimate_delta
    assignment_prob = _estimate_delta(option_type, strike, current_price, days, volatility=volatility)
    
    # Calculate price at 50% assignment
    price_at_50pct = calculate_price_at_delta(option_type, strike, days, target_delta=0.5, volatility=volatility)
    
    # Manual verification of the formulas
    # For PUT: P(ITM) = N(-d2)
    # For CALL: P(ITM) = N(d2)
    if option_type == "PUT":
        manual_prob = _norm_cdf(-d2)
    else:
        manual_prob = _norm_cdf(d2)
    
    # Verify price_at_50pct by calculating d2 at that price
    d1_at_50: Optional[float] = None
    d2_at_50: Optional[float] = None
    prob_at_50_price: Optional[float] = None
    if price_at_50pct:
        d1_at_50, d2_at_50 = _calculate_d1_d2(price_at_50pct, strike, T, DEFAULT_RISK_FREE_RATE, volatility)
        if option_type == "PUT":
            prob_at_50_price = _norm_cdf(-d2_at_50)
        else:
            prob_at_50_price = _norm_cdf(d2_at_50)
    
    # Expected price at 50% for PUT: when d2 = 0
    # S = K * exp((σ²/2 - r) * T)
    # This is the theoretical 50% point
    drift_term = (0.5 * volatility ** 2 - DEFAULT_RISK_FREE_RATE) * T
    theoretical_50pct_put = strike * math.exp(drift_term)
    
    # For CALL: when d2 = 0, same formula
    theoretical_50pct_call = theoretical_50pct_put
    
    return {
        "input": {
            "symbol": symbol,
            "strike": strike,
            "option_type": option_type,
            "days_to_expiry": days,
            "current_price": current_price,
            "iv_used": round(volatility * 100, 2),
            "iv_source": iv_source,
        },
        "time_params": {
            "T_years": round(T, 6),
            "sqrt_T": round(sqrt_T, 6),
            "risk_free_rate": DEFAULT_RISK_FREE_RATE,
        },
        "d1_d2_at_current_price": {
            "d1": round(d1, 6),
            "d2": round(d2, 6),
            "N(d2)": round(_norm_cdf(d2), 6),
            "N(-d2)": round(_norm_cdf(-d2), 6),
        },
        "assignment_probability": {
            "from_estimate_delta": round(assignment_prob * 100, 2),
            "manual_calc": round(manual_prob * 100, 2),
            "match": abs(assignment_prob - manual_prob) < 0.0001,
        },
        "price_at_50pct": {
            "calculated": round(price_at_50pct, 4) if price_at_50pct else None,
            "theoretical": round(theoretical_50pct_put, 4),
            "match": abs(price_at_50pct - theoretical_50pct_put) < 0.01 if price_at_50pct else None,
        },
        "verification_at_50pct_price": {
            "d1_at_50pct": round(d1_at_50, 6) if d1_at_50 else None,
            "d2_at_50pct": round(d2_at_50, 6) if d2_at_50 else None,
            "prob_at_50pct_price": round(prob_at_50_price * 100, 2) if prob_at_50_price else None,
            "should_be_50": prob_at_50_price is not None and abs(prob_at_50_price - 0.5) < 0.01,
        },
        "interpretation": {
            "current_vs_strike": "ITM" if (option_type == "PUT" and current_price < strike) or (option_type == "CALL" and current_price > strike) else "OTM",
            "distance_to_strike_pct": round(abs(current_price - strike) / strike * 100, 2),
            "high_iv_effect": "With high IV, the 50% assignment price can be above strike for PUTs due to drift" if volatility > 0.5 and option_type == "PUT" and price_at_50pct is not None and price_at_50pct > strike else None,
        },
        "math_explanation": {
            "formula_d2": "d2 = (ln(S/K) + (r - σ²/2)T) / (σ√T)",
            "for_put_50pct": "When N(-d2) = 0.5, d2 = 0, so ln(S/K) = (σ²/2 - r)T",
            "high_iv_insight": f"With σ={volatility:.0%}, drift term = (σ²/2 - r)T = ({0.5*volatility**2:.4f} - {DEFAULT_RISK_FREE_RATE})×{T:.4f} = {drift_term:.4f}",
            "result": f"S = K × exp({drift_term:.4f}) = {strike} × {math.exp(drift_term):.4f} = {strike * math.exp(drift_term):.2f}"
        }
    }

