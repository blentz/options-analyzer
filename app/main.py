"""FastAPI application for options trading analysis."""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from typing import List, Optional
from fastapi import FastAPI, Request, UploadFile, File, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
# Set debug level for our modules
logging.getLogger("app.stocknear").setLevel(logging.DEBUG)
logging.getLogger("app.services.stocknear_service").setLevel(logging.DEBUG)

from app.database import init_db, get_db
from app.models import OptionPosition, OptionContract
from app.services.csv_import import import_csv
from app.services.analytics import (
    get_overall_stats,
    get_pnl_by_symbol,
    get_monthly_pnl,
    get_cumulative_pnl,
    get_positions,
    get_strategy_breakdown
)
from app.charts import (
    create_cumulative_pnl_chart,
    create_monthly_pnl_chart,
    create_symbol_pnl_chart,
    create_win_loss_chart,
    create_strategy_chart,
    create_position_pnl_chart,
    create_combined_risk_chart,
    create_risk_summary_chart,
    create_theta_decay_chart
)
from app.services.risk_analysis import get_portfolio_risk_summary
from app.services.speculation_analysis import (
    OptionLeg, analyze_strategy, analyze_single_leg, STRATEGY_TEMPLATES
)
from app.services.stocknear_service import get_symbol_speculation_data, get_options_chain
from pydantic import BaseModel


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize database on startup."""
    await init_db()
    yield


app = FastAPI(
    title="Options Trading Analyzer",
    description="Track and analyze options trading performance",
    version="1.0.0",
    lifespan=lifespan
)

# Templates
templates_dir = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(templates_dir))

# Static files
static_dir = Path(__file__).parent.parent / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, db: AsyncSession = Depends(get_db)):
    """Main dashboard with charts and statistics."""
    stats = await get_overall_stats(db)
    symbol_stats = await get_pnl_by_symbol(db)
    monthly_stats = await get_monthly_pnl(db)
    cumulative_data = await get_cumulative_pnl(db)
    strategy_data = await get_strategy_breakdown(db)

    # Generate charts
    cumulative_script, cumulative_div = create_cumulative_pnl_chart(cumulative_data)
    monthly_script, monthly_div = create_monthly_pnl_chart(monthly_stats)
    symbol_script, symbol_div = create_symbol_pnl_chart(symbol_stats)
    winloss_script, winloss_div = create_win_loss_chart(stats.winners, stats.losers)
    strategy_script, strategy_div = create_strategy_chart(strategy_data)

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "stats": stats,
        "cumulative_script": cumulative_script,
        "cumulative_div": cumulative_div,
        "monthly_script": monthly_script,
        "monthly_div": monthly_div,
        "symbol_script": symbol_script,
        "symbol_div": symbol_div,
        "winloss_script": winloss_script,
        "winloss_div": winloss_div,
        "strategy_script": strategy_script,
        "strategy_div": strategy_div,
    })


@app.get("/positions", response_class=HTMLResponse)
async def positions_page(
    request: Request,
    closed: bool = False,
    open: bool = False,
    db: AsyncSession = Depends(get_db)
):
    """View all positions."""
    positions = await get_positions(db, closed_only=closed, open_only=open)

    return templates.TemplateResponse("positions.html", {
        "request": request,
        "positions": positions,
        "show_closed_only": closed,
        "show_open_only": open
    })


@app.get("/import", response_class=HTMLResponse)
async def import_page(request: Request):
    """CSV import page."""
    return templates.TemplateResponse("import.html", {
        "request": request,
        "message": None,
        "error": None
    })


@app.post("/import", response_class=HTMLResponse)
async def import_csv_files(
    request: Request,
    files: List[UploadFile] = File(...),
    db: AsyncSession = Depends(get_db)
):
    """Handle multiple CSV file uploads and import."""
    results = []
    total_imported = 0
    total_skipped = 0
    files_processed = 0
    files_failed = 0

    for file in files:
        if not file.filename.endswith('.csv'):
            results.append({
                "filename": file.filename,
                "success": False,
                "error": "Not a CSV file"
            })
            files_failed += 1
            continue

        try:
            content = await file.read()
            content_str = content.decode('utf-8-sig')
            imported, skipped = await import_csv(db, content_str, file.filename)

            results.append({
                "filename": file.filename,
                "success": True,
                "imported": imported,
                "skipped": skipped
            })
            total_imported += imported
            total_skipped += skipped
            files_processed += 1

        except Exception as e:
            results.append({
                "filename": file.filename,
                "success": False,
                "error": str(e)
            })
            files_failed += 1

    summary = {
        "total_imported": total_imported,
        "total_skipped": total_skipped,
        "files_processed": files_processed,
        "files_failed": files_failed
    }

    return templates.TemplateResponse("import.html", {
        "request": request,
        "results": results,
        "summary": summary if len(files) > 1 else None,
        "error": None
    })


@app.get("/api/stats")
async def api_stats(db: AsyncSession = Depends(get_db)):
    """API endpoint for overall statistics."""
    stats = await get_overall_stats(db)
    return {
        "total_positions": stats.total_positions,
        "closed_positions": stats.closed_positions,
        "open_positions": stats.open_positions,
        "winners": stats.winners,
        "losers": stats.losers,
        "win_rate": stats.win_rate,
        "total_pnl": float(stats.total_pnl),
        "total_commissions": float(stats.total_commissions),
        "total_fees": float(stats.total_fees)
    }


@app.get("/api/positions")
async def api_positions(closed: bool = False, db: AsyncSession = Depends(get_db)):
    """API endpoint for positions list."""
    positions = await get_positions(db, closed_only=closed)
    return [
        {
            "contract": p.contract_id,
            "symbol": p.symbol,
            "strategy": p.strategy,
            "outcome": p.outcome,
            "open_date": p.open_date,
            "close_date": p.close_date,
            "net_pnl": float(p.net_pnl),
            "is_winner": p.is_winner,
            "is_closed": p.is_closed
        }
        for p in positions
    ]


@app.get("/risk", response_class=HTMLResponse)
async def risk_analysis_page(request: Request, db: AsyncSession = Depends(get_db)):
    """Open positions risk analysis page."""
    risk_summary = await get_portfolio_risk_summary(db)
    analyses = risk_summary["analyses"]

    # Generate combined risk chart
    combined_script, combined_div = create_combined_risk_chart(analyses)

    # Generate risk summary chart
    summary_script, summary_div = create_risk_summary_chart(analyses)

    # Generate individual position P&L charts
    position_charts = []
    for analysis in analyses:
        script, div = create_position_pnl_chart(analysis)
        
        # Generate theta decay chart for positions with more than 7 days to expiry
        theta_script, theta_div = None, None
        if analysis.days_to_expiry > 7:
            theta_script, theta_div = create_theta_decay_chart(analysis)
        
        position_charts.append({
            "analysis": analysis,
            "script": script,
            "div": div,
            "theta_script": theta_script,
            "theta_div": theta_div
        })

    return templates.TemplateResponse("risk.html", {
        "request": request,
        "risk_summary": risk_summary,
        "combined_script": combined_script,
        "combined_div": combined_div,
        "summary_script": summary_script,
        "summary_div": summary_div,
        "position_charts": position_charts
    })


@app.get("/api/risk")
async def api_risk(db: AsyncSession = Depends(get_db)):
    """API endpoint for risk analysis."""
    risk_summary = await get_portfolio_risk_summary(db)

    return {
        "total_positions": risk_summary["total_positions"],
        "total_premium": risk_summary["total_premium"],
        "total_max_profit": risk_summary["total_max_profit"],
        "total_max_loss": risk_summary["total_max_loss"],
        "positions_expiring_soon": risk_summary["positions_expiring_soon"],
        "positions": [
            {
                "contract": a.contract_id,
                "symbol": a.symbol,
                "expiration": a.expiration.isoformat(),
                "days_to_expiry": a.days_to_expiry,
                "strike": a.strike,
                "option_type": a.option_type,
                "strategy": a.strategy,
                "quantity": a.quantity,
                "premium_received": a.premium_received,
                "max_profit": a.max_profit,
                "max_loss": a.max_loss,
                "breakeven": a.breakeven,
                "assignment_probability": a.assignment_probability,
                "price_at_50pct_assignment": a.price_at_50pct_assignment,
                "exit_scenarios": [
                    {
                        "name": es.name,
                        "description": es.description,
                        "pnl": es.pnl,
                        "pnl_percent": es.pnl_percent,
                        "underlying_price": es.underlying_price,
                        "probability": es.probability,
                        "price_lower_bound": es.price_lower_bound,
                        "price_upper_bound": es.price_upper_bound
                    }
                    for es in a.exit_scenarios
                ],
                "scenarios": [
                    {"price": s.underlying_price, "pnl": s.pnl}
                    for s in a.scenarios
                ]
            }
            for a in risk_summary["analyses"]
        ]
    }


@app.get("/api/risk/calculate-exit")
async def api_calculate_exit(
    contract_id: str,
    close_price: Optional[float] = None,
    assignment_price: Optional[float] = None,
    volatility: Optional[float] = None,
    db: AsyncSession = Depends(get_db)
):
    """
    Calculate P&L and probability for custom exit scenarios.
    
    Args:
        contract_id: The contract ID string (e.g., "BEPC 03/20/26 $35.00 PUT")
        close_price: Option price per share to close at (buy to close / sell to close)
        assignment_price: Underlying price for assignment scenario
        volatility: Implied volatility as decimal (e.g., 0.30 for 30%). If not provided,
                   fetches live IV from StockNear (cached for 1 hour).
    """
    from app.services.risk_analysis import (
        calculate_close_scenario_with_probability,
        calculate_assignment_scenario_with_probability
    )
    from app.services.price_service import get_stock_price
    from app.services.stocknear_service import get_options_overview
    from datetime import date, datetime
    import re
    
    # Parse the contract_id string to extract components first (need symbol for IV lookup)
    # Format: "SYMBOL MM/DD/YY $STRIKE TYPE"
    match = re.match(r'^(\w+)\s+(\d{2}/\d{2}/\d{2})\s+\$(\d+\.?\d*)\s+(PUT|CALL)$', contract_id)
    if not match:
        return {"error": f"Invalid contract_id format: {contract_id}"}
    
    symbol = match.group(1)
    exp_str = match.group(2)
    strike = float(match.group(3))
    option_type = match.group(4)
    
    # Fetch live IV from StockNear if not provided
    iv_source = "user_provided"
    if volatility is None:
        try:
            options_data = await get_options_overview(db, symbol)
            if options_data and options_data.implied_volatility:
                volatility = options_data.implied_volatility
                iv_source = "stocknear_live"
            else:
                volatility = 0.30
                iv_source = "default"
        except Exception as e:
            print(f"Warning: Could not fetch IV from StockNear for {symbol}: {e}")
            volatility = 0.30
            iv_source = "default"
    
    # Clamp volatility to reasonable range (5% to 200%)
    volatility = max(0.05, min(2.0, volatility))
    
    # Parse expiration date
    exp_date = datetime.strptime(exp_str, "%m/%d/%y").date()
    
    # Find the position by contract components
    stmt = select(OptionPosition).options(
        selectinload(OptionPosition.contract)
    ).join(OptionContract).where(
        OptionContract.symbol == symbol,
        OptionContract.expiration == exp_date,
        OptionContract.strike == strike,
        OptionContract.option_type == option_type
    )
    
    result = await db.execute(stmt)
    position = result.scalar_one_or_none()
    
    if not position:
        return {"error": "Position not found"}
    
    contract = position.contract
    strike = float(contract.strike)
    option_type = contract.option_type
    strategy = position.strategy
    premium = float(position.total_premium)
    num_contracts = position.num_contracts or 1
    multiplier = 100 * num_contracts
    premium_per_share = premium / multiplier if multiplier > 0 else 0
    
    today = date.today()
    days_to_expiry = (contract.expiration - today).days
    
    # Fetch current price for probability calculations
    current_quote = await get_stock_price(symbol)
    current_price = current_quote.price if current_quote else None
    
    response = {
        "contract_id": contract_id,
        "symbol": contract.symbol,
        "strike": strike,
        "option_type": option_type,
        "strategy": strategy,
        "premium": premium,
        "premium_per_share": round(premium_per_share, 4),
        "quantity": num_contracts,
        "days_to_expiry": days_to_expiry,
        "current_underlying_price": current_price,
        "volatility_used": round(volatility * 100, 1),
        "volatility_source": iv_source
    }
    
    if current_price is None:
        response["warning"] = "Could not fetch current price - probability calculations unavailable"
    
    # Calculate close scenario if requested
    if close_price is not None and current_price is not None:
        close_result = calculate_close_scenario_with_probability(
            option_type=option_type,
            strategy=strategy,
            strike=strike,
            premium=premium,
            num_contracts=num_contracts,
            current_price=current_price,
            days_to_expiry=days_to_expiry,
            close_price_per_share=close_price,
            current_option_price=premium_per_share,  # Using original premium as proxy
            volatility=volatility
        )
        
        response["close_scenario"] = {
            "close_price_per_share": close_price,
            "close_cost_total": close_price * multiplier,
            "pnl": close_result["pnl"],
            "pnl_percent": close_result["pnl_percent"],
            "estimated_underlying": close_result["estimated_underlying"],
            "probability": close_result["probability"],
            "price_lower_bound": close_result["price_lower_bound"],
            "price_upper_bound": close_result["price_upper_bound"],
            "description": close_result["description"]
        }
    elif close_price is not None:
        # Fallback without current price
        from app.services.risk_analysis import calculate_close_pnl
        close_pnl = calculate_close_pnl(strategy, premium, close_price, num_contracts)
        if "SHORT" in strategy:
            max_risk = strike * multiplier if option_type == "PUT" else premium * 10
        else:
            max_risk = abs(premium)
        
        response["close_scenario"] = {
            "close_price_per_share": close_price,
            "close_cost_total": close_price * multiplier,
            "pnl": round(close_pnl, 2),
            "pnl_percent": round((close_pnl / max_risk) * 100, 2) if max_risk else 0,
            "description": f"{'Buy' if 'SHORT' in strategy else 'Sell'} to close at ${close_price:.2f}/share",
            "probability": None,
            "note": "Current price unavailable - probability not calculated"
        }
    
    # Calculate assignment scenario if requested
    if assignment_price is not None and current_price is not None:
        assign_result = calculate_assignment_scenario_with_probability(
            option_type=option_type,
            strategy=strategy,
            strike=strike,
            premium=premium,
            num_contracts=num_contracts,
            current_price=current_price,
            days_to_expiry=days_to_expiry,
            assignment_price=assignment_price,
            volatility=volatility
        )
        
        response["assignment_scenario"] = {
            "underlying_price": assignment_price,
            "pnl": assign_result["pnl"],
            "pnl_percent": assign_result["pnl_percent"],
            "assignment_probability": assign_result["assignment_probability"],
            "price_probability": assign_result["price_probability"],
            "expected_range_low": assign_result["expected_range_low"],
            "expected_range_high": assign_result["expected_range_high"],
            "description": assign_result["description"]
        }
    elif assignment_price is not None:
        # Fallback without current price
        from app.services.risk_analysis import calculate_option_pnl_at_expiry, _estimate_delta
        assignment_pnl = calculate_option_pnl_at_expiry(
            option_type, strategy, strike, premium, assignment_price, num_contracts
        )
        assignment_prob = _estimate_delta(option_type, strike, assignment_price, days_to_expiry)
        
        if "SHORT" in strategy:
            max_risk = strike * multiplier if option_type == "PUT" else premium * 10
        else:
            max_risk = abs(premium)
        
        response["assignment_scenario"] = {
            "underlying_price": assignment_price,
            "pnl": round(assignment_pnl, 2),
            "pnl_percent": round((assignment_pnl / max_risk) * 100, 2) if max_risk else 0,
            "assignment_probability": round(assignment_prob * 100, 1),
            "description": f"Assignment at ${assignment_price:.2f}",
            "price_probability": None,
            "note": "Current price unavailable - price probability not calculated"
        }
    
    return response


# ============================================================================
# Speculation Routes
# ============================================================================


class SpeculationLegInput(BaseModel):
    """Single leg input for speculation analysis."""
    option_type: str  # "CALL" or "PUT"
    strike: float
    expiration: str  # "YYYY-MM-DD"
    action: str  # "BUY" or "SELL"
    quantity: int = 1
    premium: float = 0.0  # Premium per share


class SpeculationRequest(BaseModel):
    """Request body for speculation analysis."""
    symbol: str
    strategy_name: str = "Custom Strategy"
    legs: list[SpeculationLegInput]
    implied_volatility: Optional[float] = None


@app.get("/speculation", response_class=HTMLResponse)
async def speculation_page(request: Request, db: AsyncSession = Depends(get_db)):
    """Options speculation analysis page."""
    return templates.TemplateResponse("speculation.html", {
        "request": request,
        "strategy_templates": STRATEGY_TEMPLATES
    })


@app.get("/api/speculation/lookup")
async def api_speculation_lookup(
    symbol: str,
    db: AsyncSession = Depends(get_db)
):
    """
    Lookup symbol data for speculation.
    
    Returns current price, IV, expirations, etc.
    """
    try:
        data = await get_symbol_speculation_data(db, symbol)
        return data
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/speculation/strikes")
async def api_speculation_strikes(
    symbol: str,
    force_refresh: bool = False,
    db: AsyncSession = Depends(get_db)
):
    """
    Get available strike prices for a symbol.
    
    Returns list of valid strikes that can be used for options contracts.
    Useful for auto-correcting user input to valid strike values.
    """
    from app.services.stocknear_service import get_available_strikes
    
    try:
        data = await get_available_strikes(db, symbol, force_refresh)
        return data
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/speculation/nearest-strikes")
async def api_speculation_nearest_strikes(
    symbol: str,
    target_strike: float,
    num_strikes: int = 5,
    force_refresh: bool = False,
    db: AsyncSession = Depends(get_db)
):
    """
    Get the nearest valid strikes to a target value.
    
    Returns the closest valid strikes above and below the target.
    Useful for iron condors where user specifies a target and we auto-select valid strikes.
    
    Args:
        symbol: Stock symbol
        target_strike: The desired strike value
        num_strikes: Number of strikes to return on each side (default 5)
    
    Returns:
        dict with:
        - exact: The exact match if target is a valid strike, else null
        - below: List of valid strikes below target (closest first)
        - above: List of valid strikes above target (closest first)
        - nearest: The single closest valid strike
    """
    from app.services.stocknear_service import get_available_strikes
    
    try:
        data = await get_available_strikes(db, symbol, force_refresh)
        strikes = data.get("strikes", [])
        
        if not strikes:
            raise HTTPException(status_code=404, detail=f"No strikes found for {symbol}")
        
        # Check for exact match
        exact = target_strike if target_strike in strikes else None
        
        # Find strikes below and above
        below = sorted([s for s in strikes if s < target_strike], reverse=True)[:num_strikes]
        above = sorted([s for s in strikes if s > target_strike])[:num_strikes]
        
        # Find nearest
        nearest = min(strikes, key=lambda s: abs(s - target_strike))
        
        return {
            "target": target_strike,
            "exact": exact,
            "below": below,
            "above": above,
            "nearest": nearest
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
async def api_speculation_chain(
    symbol: str,
    db: AsyncSession = Depends(get_db)
):
    """
    Get full options chain for a symbol.
    
    Returns available strikes, bids, asks, greeks for each contract.
    """
    try:
        chain = await get_options_chain(db, symbol)
        if not chain:
            raise HTTPException(status_code=404, detail=f"No options chain found for {symbol}")
        
        return {
            "symbol": chain.symbol,
            "current_price": chain.current_price,
            "expirations": chain.expirations,
            "implied_volatility": chain.implied_volatility,
            "iv_rank": chain.iv_rank,
            "iv_percentile": chain.iv_percentile,
            "contracts": [
                {
                    "strike": c.strike,
                    "option_type": c.option_type,
                    "expiration": c.expiration,
                    "bid": c.bid,
                    "ask": c.ask,
                    "last": c.last,
                    "volume": c.volume,
                    "open_interest": c.open_interest,
                    "implied_volatility": c.implied_volatility,
                    "delta": c.delta,
                    "gamma": c.gamma,
                    "theta": c.theta,
                    "vega": c.vega,
                }
                for c in chain.contracts
            ]
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/speculation/analyze")
async def api_speculation_analyze(
    request: SpeculationRequest,
    db: AsyncSession = Depends(get_db)
):
    """
    Analyze a hypothetical options strategy.
    
    Returns P&L scenarios, breakevens, max profit/loss, profit probability.
    
    Note: Premium values should be provided by the frontend (pre-filled from chain data).
    """
    from datetime import datetime, date
    from app.charts import create_speculation_pnl_chart, create_speculation_theta_chart
    from app.services.price_service import get_stock_price
    from app.services.stocknear_service import get_options_chain, get_options_overview
    
    symbol = request.symbol.upper()
    
    # Get current price - try Yahoo first, fall back to chain data
    price_quote = await get_stock_price(symbol)
    current_price = price_quote.price if price_quote else None
    
    # Get options chain for price fallback
    chain = await get_options_chain(db, symbol)
    
    # Fallback to chain price if Yahoo returned null
    if current_price is None and chain and chain.current_price:
        current_price = chain.current_price
    
    if current_price is None:
        raise HTTPException(status_code=400, detail=f"Could not get price for {symbol}")
    
    # Build OptionLeg objects using premiums provided by frontend
    legs = []
    for leg_input in request.legs:
        # Parse expiration date - handle multiple formats
        exp_str = leg_input.expiration
        exp_date = None
        
        # Try different date formats
        date_formats = [
            "%Y-%m-%d",          # ISO format: 2026-06-18
            "%b %d, %Y",         # Month DD, YYYY: Jun 18, 2026
            "%B %d, %Y",         # Full month: June 18, 2026
            "%m/%d/%Y",          # US format: 06/18/2026
            "%m/%d/%y",          # Short US: 06/18/26
        ]
        
        for fmt in date_formats:
            try:
                exp_date = datetime.strptime(exp_str, fmt).date()
                break
            except ValueError:
                continue
        
        if exp_date is None:
            raise HTTPException(
                status_code=400, 
                detail=f"Could not parse expiration date: {exp_str}"
            )
        
        leg = OptionLeg(
            option_type=leg_input.option_type.upper(),
            strike=leg_input.strike,
            expiration=exp_date,
            action=leg_input.action.upper(),
            quantity=leg_input.quantity,
            premium=leg_input.premium,
        )
        legs.append(leg)
    
    # Get IV from request, chain, or StockNear overview
    iv = request.implied_volatility
    if iv is None:
        if chain and chain.implied_volatility:
            iv = chain.implied_volatility
        else:
            options_data = await get_options_overview(db, symbol)
            if options_data and options_data.implied_volatility:
                iv = options_data.implied_volatility
            else:
                iv = 0.30  # Default
    
    # Perform analysis
    try:
        analysis = analyze_strategy(
            symbol=symbol,
            legs=legs,
            current_price=current_price,
            strategy_name=request.strategy_name,
            implied_volatility=iv,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Analysis failed: {str(e)}")
    
    # Import risk analysis functions for per-leg stats
    from app.services.risk_analysis import (
        _estimate_delta,
        calculate_price_at_delta,
        generate_exit_scenarios,
        calculate_breakeven,
    )
    
    # Generate charts
    pnl_script, pnl_div = create_speculation_pnl_chart(analysis)
    theta_script, theta_div = None, None
    if analysis.days_to_expiry > 7:
        theta_script, theta_div = create_speculation_theta_chart(analysis)
    
    # Build per-leg stats with ITM/OTM, assignment risk, 50% price
    legs_data = []
    for leg in legs:
        days_to_exp = (leg.expiration - date.today()).days
        
        # ITM/OTM status
        if leg.option_type == "CALL":
            is_itm = current_price > leg.strike
        else:  # PUT
            is_itm = current_price < leg.strike
        
        distance_to_strike = abs(current_price - leg.strike)
        distance_pct = (distance_to_strike / leg.strike) * 100 if leg.strike > 0 else 0
        
        # Assignment probability (for short positions)
        assignment_prob = None
        if leg.is_short:
            assignment_prob = _estimate_delta(
                leg.option_type, leg.strike, current_price, days_to_exp, volatility=iv
            ) * 100
        
        # 50% assignment price (for short positions)
        price_at_50pct = None
        if leg.is_short and days_to_exp > 0:
            price_at_50pct = calculate_price_at_delta(
                leg.option_type, leg.strike, days_to_exp, target_delta=0.5, volatility=iv
            )
        
        # Calculate per-leg breakeven
        if leg.is_short:
            if leg.option_type == "PUT":
                breakeven = leg.strike - leg.premium
            else:
                breakeven = leg.strike + leg.premium
        else:
            if leg.option_type == "CALL":
                breakeven = leg.strike + leg.premium
            else:
                breakeven = leg.strike - leg.premium
        
        legs_data.append({
            "option_type": leg.option_type,
            "strike": leg.strike,
            "expiration": leg.expiration.isoformat(),
            "action": leg.action,
            "quantity": leg.quantity,
            "premium": leg.premium,
            "total_premium": leg.total_premium,
            "strategy": leg.strategy,
            "itm": is_itm,
            "distance_to_strike_pct": round(distance_pct, 1),
            "assignment_probability": round(assignment_prob, 1) if assignment_prob is not None else None,
            "price_at_50pct_assignment": round(price_at_50pct, 2) if price_at_50pct else None,
            "breakeven": round(breakeven, 2),
            "days_to_expiry": days_to_exp,
        })
    
    # Build response
    return {
        "strategy_name": analysis.strategy_name,
        "symbol": analysis.symbol,
        "current_price": current_price,
        "net_premium": analysis.net_premium,
        "max_profit": analysis.max_profit,
        "max_loss": analysis.max_loss,
        "breakeven_prices": analysis.breakeven_prices,
        "days_to_expiry": analysis.days_to_expiry,
        "profit_probability": analysis.profit_probability,
        "implied_volatility": iv,
        "legs": legs_data,
        "scenarios": [
            {
                "underlying_price": s.underlying_price,
                "pnl": s.pnl,
                "pnl_percent": s.pnl_percent,
            }
            for s in analysis.scenarios
        ],
        "charts": {
            "pnl_script": pnl_script,
            "pnl_div": pnl_div,
            "theta_script": theta_script,
            "theta_div": theta_div,
        }
    }


@app.get("/api/speculation/calculate-exit")
async def api_speculation_calculate_exit(
    option_type: str,  # CALL or PUT
    action: str,  # BUY or SELL
    strike: float,
    premium: float,  # Premium per share
    quantity: int,
    days_to_expiry: int,
    current_price: float,  # Current underlying price
    close_price: Optional[float] = None,  # Option price to close at (per share)
    assignment_price: Optional[float] = None,  # Underlying price for assignment
    volatility: Optional[float] = 0.30,  # IV as decimal
):
    """
    Calculate P&L and probability for custom exit scenarios on speculation positions.
    
    Unlike /api/risk/calculate-exit, this works with hypothetical positions
    rather than database-backed positions.
    """
    from app.services.risk_analysis import (
        calculate_close_scenario_with_probability,
        calculate_assignment_scenario_with_probability,
        calculate_close_pnl,
        calculate_option_pnl_at_expiry,
        _estimate_delta,
    )
    
    option_type = option_type.upper()
    action = action.upper()
    strategy = f"{'LONG' if action == 'BUY' else 'SHORT'} {option_type}"
    
    # Clamp volatility to reasonable range (5% to 200%)
    vol = volatility if volatility is not None else 0.30
    vol = max(0.05, min(2.0, vol))
    
    multiplier = 100 * quantity
    total_premium = premium * multiplier
    
    # For short positions, premium is received (positive)
    # For long positions, premium is paid (negative)
    if action == "BUY":
        total_premium = -total_premium
    
    response = {
        "option_type": option_type,
        "action": action,
        "strategy": strategy,
        "strike": strike,
        "premium_per_share": premium,
        "premium_total": abs(total_premium),
        "quantity": quantity,
        "days_to_expiry": days_to_expiry,
        "current_underlying_price": current_price,
        "volatility_used": round(vol * 100, 1),
    }
    
    # Calculate close scenario if requested
    if close_price is not None:
        close_result = calculate_close_scenario_with_probability(
            option_type=option_type,
            strategy=strategy,
            strike=strike,
            premium=total_premium if action == "SELL" else -total_premium,
            num_contracts=quantity,
            current_price=current_price,
            days_to_expiry=days_to_expiry,
            close_price_per_share=close_price,
            current_option_price=premium,
            volatility=vol
        )
        
        response["close_scenario"] = {
            "close_price_per_share": close_price,
            "close_cost_total": close_price * multiplier,
            "pnl": close_result["pnl"],
            "pnl_percent": close_result["pnl_percent"],
            "estimated_underlying": close_result["estimated_underlying"],
            "probability": close_result["probability"],
            "price_lower_bound": close_result["price_lower_bound"],
            "price_upper_bound": close_result["price_upper_bound"],
            "description": close_result["description"]
        }
    
    # Calculate assignment scenario if requested
    if assignment_price is not None:
        assign_result = calculate_assignment_scenario_with_probability(
            option_type=option_type,
            strategy=strategy,
            strike=strike,
            premium=total_premium if action == "SELL" else -total_premium,
            num_contracts=quantity,
            current_price=current_price,
            days_to_expiry=days_to_expiry,
            assignment_price=assignment_price,
            volatility=vol
        )
        
        response["assignment_scenario"] = {
            "underlying_price": assignment_price,
            "pnl": assign_result["pnl"],
            "pnl_percent": assign_result["pnl_percent"],
            "assignment_probability": assign_result["assignment_probability"],
            "price_probability": assign_result["price_probability"],
            "expected_range_low": assign_result["expected_range_low"],
            "expected_range_high": assign_result["expected_range_high"],
            "description": assign_result["description"]
        }
    
    return response


@app.get("/api/speculation/contract-quote")
async def api_speculation_contract_quote(
    symbol: str,
    expiration: str,
    strike: float,
    option_type: str,
    force_refresh: bool = False,
    db: AsyncSession = Depends(get_db)
):
    """
    Get real-time quote for a specific option contract.
    
    Returns bid, ask, mid, last prices plus Greeks from StockNear's contract lookup page.
    Uses 5-minute cache for real-time data.
    
    Parameters:
        symbol: Stock ticker (e.g., "AAPL")
        expiration: Expiration date (e.g., "2026-03-20" or "Mar 20, 2026")
        strike: Strike price (e.g., 150.00)
        option_type: "CALL" or "PUT"
        force_refresh: If True, bypass cache
    """
    from app.services.stocknear_service import get_contract_quote
    
    symbol = symbol.upper()
    option_type = option_type.upper()
    
    if option_type not in ("CALL", "PUT"):
        raise HTTPException(status_code=400, detail="option_type must be 'CALL' or 'PUT'")
    
    if strike <= 0:
        raise HTTPException(status_code=400, detail="strike must be positive")
    
    quote = await get_contract_quote(db, symbol, expiration, strike, option_type, force_refresh)
    
    if not quote:
        raise HTTPException(
            status_code=404,
            detail=f"Could not get quote for {symbol} {expiration} ${strike} {option_type}"
        )
    
    return {
        "symbol": quote.symbol,
        "strike": quote.strike,
        "option_type": quote.option_type,
        "expiration": quote.expiration,
        "contract_id": quote.contract_id,
        "bid": quote.bid,
        "ask": quote.ask,
        "mid": quote.mid,
        "last": quote.last,
        "open_price": quote.open_price,
        "volume": quote.volume,
        "open_interest": quote.open_interest,
        "implied_volatility": quote.implied_volatility,
        "delta": quote.delta,
        "gamma": quote.gamma,
        "theta": quote.theta,
        "vega": quote.vega,
    }


class ContractRequest(BaseModel):
    """Single contract specification for batch quote request."""
    symbol: str
    expiration: str
    strike: float
    option_type: str


class BatchQuoteRequest(BaseModel):
    """Request body for batch quote endpoint."""
    contracts: list[ContractRequest]
    force_refresh: bool = False


@app.post("/api/speculation/quotes-batch")
async def api_speculation_quotes_batch(
    request: BatchQuoteRequest,
    db: AsyncSession = Depends(get_db)
):
    """
    Batch fetch quotes for multiple option contracts in a SINGLE browser session.
    
    Much more efficient than calling /contract-quote multiple times.
    Reduces 4 browser launches (for 4-leg strategy) to just 1.
    
    Request body:
        contracts: List of {symbol, expiration, strike, option_type}
        force_refresh: If True, bypass cache
    
    Returns:
        List of quote objects in same order as input
    """
    from app.services.stocknear_service import get_contract_quotes_batch
    
    if not request.contracts:
        return {"quotes": []}
    
    if len(request.contracts) > 10:
        raise HTTPException(status_code=400, detail="Maximum 10 contracts per batch request")
    
    # Validate contracts
    contracts = []
    for i, c in enumerate(request.contracts):
        if c.option_type.upper() not in ("CALL", "PUT"):
            raise HTTPException(
                status_code=400, 
                detail=f"Contract {i}: option_type must be 'CALL' or 'PUT'"
            )
        if c.strike <= 0:
            raise HTTPException(
                status_code=400,
                detail=f"Contract {i}: strike must be positive"
            )
        contracts.append({
            "symbol": c.symbol.upper(),
            "expiration": c.expiration,
            "strike": c.strike,
            "option_type": c.option_type.upper()
        })
    
    quotes = await get_contract_quotes_batch(db, contracts, request.force_refresh)
    
    return {
        "quotes": [
            {
                "symbol": q.symbol,
                "strike": q.strike,
                "option_type": q.option_type,
                "expiration": q.expiration,
                "contract_id": q.contract_id,
                "bid": q.bid,
                "ask": q.ask,
                "mid": q.mid,
                "last": q.last,
                "volume": q.volume,
                "open_interest": q.open_interest,
                "implied_volatility": q.implied_volatility,
                "delta": q.delta,
                "theta": q.theta,
            }
            for q in quotes
        ]
    }


@app.get("/api/debug/stocknear/{symbol}")
async def debug_stocknear(symbol: str, db: AsyncSession = Depends(get_db)):
    """
    Debug endpoint to view raw scraped content from StockNear.
    Useful for debugging regex patterns.
    """
    from app.stocknear import StockNearScraper
    import asyncio
    
    def scrape_sync():
        with StockNearScraper() as scraper:
            # Get the options overview page content
            scraper.navigate(f"/stocks/{symbol.lower()}/options")
            scraper.page.wait_for_timeout(3000)
            overview_content = scraper.get_page_text()
            
            # Get the chain page content
            scraper.navigate(f"/stocks/{symbol.lower()}/options/chain")
            scraper.page.wait_for_timeout(3000)
            chain_content = scraper.get_page_text()
            
            return {
                "overview_url": f"https://stocknear.com/stocks/{symbol.lower()}/options",
                "overview_content": overview_content,
                "overview_length": len(overview_content),
                "chain_url": f"https://stocknear.com/stocks/{symbol.lower()}/options/chain",
                "chain_content": chain_content,
                "chain_length": len(chain_content),
            }
    
    result = await asyncio.to_thread(scrape_sync)
    return result


@app.get("/api/debug/stocknear-api/{symbol}")
async def debug_stocknear_api(
    symbol: str,
    expiration: str,
    strike: float,
    option_type: str,
    db: AsyncSession = Depends(get_db)
):
    """
    Debug endpoint to scrape the contract lookup page using dropdown selection.
    Selects the expiration, strike, and option type, then waits for data to load.
    """
    from app.stocknear import StockNearScraper
    import asyncio
    import json
    
    def scrape_sync():
        with StockNearScraper() as scraper:
            contract_id = scraper._build_contract_id(symbol, expiration, option_type, strike)
            
            # Intercept API requests AND responses to see headers and data
            api_requests = []
            api_responses = []
            
            def handle_request(request):
                if "options-contract" in request.url.lower():
                    try:
                        api_requests.append({
                            "url": request.url,
                            "method": request.method,
                            "headers": dict(request.headers) if request.headers else {},
                            "post_data": request.post_data
                        })
                    except Exception as e:
                        api_requests.append({"error": str(e)})
            
            def handle_response(response):
                if "options-contract" in response.url.lower():
                    try:
                        body = response.text()
                        api_responses.append({
                            "url": response.url,
                            "status": response.status,
                            "body_preview": body[:1000] if body else None
                        })
                    except:
                        api_responses.append({
                            "url": response.url,
                            "status": response.status,
                            "body_preview": "failed to get body"
                        })
            
            scraper.page.on("request", handle_request)
            scraper.page.on("response", handle_response)
            
            # Navigate to the contract lookup page
            url = f"/stocks/{symbol.lower()}/options/contract-lookup"
            scraper.navigate(url)
            scraper.page.wait_for_timeout(3000)
            
            selection_log = []
            
            # Select option type first (Call/Put)
            type_text = "Call" if option_type.upper() == "CALL" else "Put"
            try:
                # Click the Option Type dropdown button
                type_btn = scraper.page.locator('text=Option Type').locator('..').locator('button').first
                if type_btn.count() > 0:
                    type_btn.click()
                    scraper.page.wait_for_timeout(500)
                    # Select the option type
                    scraper.page.locator(f'[role="menuitem"]:has-text("{type_text}")').first.click()
                    scraper.page.wait_for_timeout(1000)
                    selection_log.append(f"Selected option type: {type_text}")
            except Exception as e:
                selection_log.append(f"Option type selection failed: {str(e)[:100]}")
            
            # Select expiration date
            try:
                exp_btn = scraper.page.locator('text=Date Expiration').locator('..').locator('button').first
                if exp_btn.count() > 0:
                    exp_btn.click()
                    scraper.page.wait_for_timeout(500)
                    # Find and click the expiration - format like "Mar 20, 2026"
                    scraper.page.locator(f'[role="menuitem"]:has-text("{expiration}")').first.click()
                    scraper.page.wait_for_timeout(1000)
                    selection_log.append(f"Selected expiration: {expiration}")
            except Exception as e:
                selection_log.append(f"Expiration selection failed: {str(e)[:100]}")
            
            # Select strike price
            strike_str = str(int(strike)) if strike == int(strike) else str(strike)
            try:
                strike_btn = scraper.page.locator('text=Strike Price').locator('..').locator('button').first
                if strike_btn.count() > 0:
                    strike_btn.click()
                    scraper.page.wait_for_timeout(500)
                    # Select the strike
                    scraper.page.locator(f'[role="menuitem"]:has-text("{strike_str}")').first.click()
                    scraper.page.wait_for_timeout(2000)
                    selection_log.append(f"Selected strike: {strike_str}")
            except Exception as e:
                selection_log.append(f"Strike selection failed: {str(e)[:100]}")
            
            # Wait for data to load after selections
            try:
                scraper.page.wait_for_function(
                    """() => {
                        const text = document.body.innerText;
                        return text.includes('Last') && 
                               text.includes('Bid') && 
                               text.includes('Ask') &&
                               text.length > 1500;
                    }""",
                    timeout=15000
                )
                selection_log.append("Data loaded successfully")
            except Exception as e:
                selection_log.append(f"Wait for data timeout: {str(e)[:100]}")
            
            scraper.page.wait_for_timeout(2000)
            
            # Get the full page content
            content = scraper.get_page_text()
            
            # Take screenshot
            screenshot_path = f"/app/data/debug_api_{contract_id}.png"
            try:
                scraper.screenshot(screenshot_path)
            except:
                screenshot_path = None
            
            # Parse quote data from the rendered page
            import re
            quote_data = {}
            
            # Last price
            last_match = re.search(r'\bLast\s*[\n\r]+\s*\$?(\d+\.?\d*)', content, re.IGNORECASE)
            if last_match:
                quote_data['last'] = float(last_match.group(1))
            
            # Bid
            bid_match = re.search(r'\bBid\s*[\n\r]+\s*\$?(\d+\.?\d*)', content, re.IGNORECASE)
            if bid_match:
                quote_data['bid'] = float(bid_match.group(1))
            
            # Mid
            mid_match = re.search(r'\bMid\s*[\n\r]+\s*\$?(\d+\.?\d*)', content, re.IGNORECASE)
            if mid_match:
                quote_data['mid'] = float(mid_match.group(1))
            
            # Ask
            ask_match = re.search(r'\bAsk\s*[\n\r]+\s*\$?(\d+\.?\d*)', content, re.IGNORECASE)
            if ask_match:
                quote_data['ask'] = float(ask_match.group(1))
            
            # Volume
            vol_match = re.search(r'\bVolume\s*[\n\r]+\s*([\d,]+)', content, re.IGNORECASE)
            if vol_match:
                quote_data['volume'] = int(vol_match.group(1).replace(',', ''))
            
            # Open Interest
            oi_match = re.search(r'\bOpen\s*Interest\s*[\n\r]+\s*([\d,]+)', content, re.IGNORECASE)
            if oi_match:
                quote_data['open_interest'] = int(oi_match.group(1).replace(',', ''))
            
            # IV
            iv_match = re.search(r'\bImplied\s*Volatility\s*\(?IV\)?\s*[\n\r]+\s*(\d+\.?\d*)%?', content, re.IGNORECASE)
            if iv_match:
                quote_data['iv'] = float(iv_match.group(1)) / 100
            
            return {
                "contract_id": contract_id,
                "selection_log": selection_log,
                "api_requests": api_requests[:5],
                "api_responses": api_responses,
                "quote_data": quote_data,
                "page_text_length": len(content),
                "page_sample": content[:4000],
                "screenshot_path": screenshot_path
            }
    
    result = await asyncio.to_thread(scrape_sync)
    return result


@app.get("/api/debug/contract-js/{symbol}")
async def debug_contract_js(
    symbol: str,
    expiration: str,
    strike: float,
    option_type: str,
    db: AsyncSession = Depends(get_db)
):
    """
    Debug endpoint to analyze the contract lookup page's JavaScript and network calls.
    """
    from app.stocknear import StockNearScraper
    import asyncio
    import json
    
    def scrape_sync():
        with StockNearScraper() as scraper:
            contract_id = scraper._build_contract_id(symbol, expiration, option_type, strike)
            url = f"/stocks/{symbol.lower()}/options/contract-lookup?contract={contract_id}"
            
            # Set up network request interception to capture API calls
            api_calls = []
            
            def handle_request(request):
                if "api" in request.url.lower() or "query" in request.url.lower() or "fetch" in request.url.lower():
                    api_calls.append({
                        "url": request.url,
                        "method": request.method,
                        "resource_type": request.resource_type
                    })
            
            def handle_response(response):
                if "api" in response.url.lower() or "query" in response.url.lower():
                    try:
                        api_calls.append({
                            "url": response.url,
                            "status": response.status,
                            "response_type": "response"
                        })
                    except:
                        pass
            
            scraper.page.on("request", handle_request)
            scraper.page.on("response", handle_response)
            
            scraper.navigate(url)
            
            # Wait longer and scroll to trigger lazy loading
            scraper.page.wait_for_timeout(3000)
            
            # Try scrolling to trigger any lazy-loaded content
            scraper.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            scraper.page.wait_for_timeout(2000)
            
            # Check for any fetch/XHR calls in the page
            js_info = scraper.page.evaluate("""() => {
                const scripts = Array.from(document.querySelectorAll('script')).map(s => s.src || 'inline');
                
                // Look for relevant data in window object
                const windowKeys = Object.keys(window).filter(k => 
                    k.toLowerCase().includes('data') || 
                    k.toLowerCase().includes('option') ||
                    k.toLowerCase().includes('contract') ||
                    k.toLowerCase().includes('quote')
                );
                
                // Check for __NEXT_DATA__ (if Next.js)
                let nextData = null;
                if (window.__NEXT_DATA__) {
                    nextData = JSON.stringify(window.__NEXT_DATA__).substring(0, 2000);
                }
                
                // Check for SvelteKit/Svelte stores
                let svelteData = null;
                try {
                    // Look for any svelte-related data
                    const svelteElements = document.querySelectorAll('[data-svelte-h]');
                    svelteData = svelteElements.length > 0 ? svelteElements.length + " svelte elements" : null;
                } catch(e) {}
                
                return {
                    scripts: scripts.slice(0, 10),
                    windowKeys: windowKeys.slice(0, 20),
                    nextData: nextData,
                    svelteData: svelteData,
                    bodyLength: document.body.innerText.length,
                    hasLogin: document.body.innerText.includes('Login'),
                    hasNoData: document.body.innerText.includes('No data'),
                    pageSample: document.body.innerText.substring(0, 1500)
                };
            }""")
            
            content = scraper.get_page_text()
            html = scraper.page.content()
            
            # Look for API endpoints in the HTML/JS
            import re
            api_patterns = re.findall(r'["\'](/api/[^"\']+)["\']', html)[:20]
            fetch_patterns = re.findall(r'fetch\(["\']([^"\']+)["\']', html)[:20]
            
            # Take screenshot
            screenshot_path = f"/app/data/debug_js_{contract_id}.png"
            try:
                scraper.screenshot(screenshot_path)
            except:
                screenshot_path = None
            
            return {
                "contract_id": contract_id,
                "url": f"https://stocknear.com{url}",
                "api_calls_captured": api_calls[:20],
                "js_info": js_info,
                "api_patterns_in_html": api_patterns,
                "fetch_patterns_in_html": fetch_patterns,
                "page_text_length": len(content),
                "screenshot_path": screenshot_path
            }
    
    result = await asyncio.to_thread(scrape_sync)
    return result


@app.get("/api/debug/contract/{symbol}")
async def debug_contract(
    symbol: str,
    expiration: str,
    strike: float,
    option_type: str,
    db: AsyncSession = Depends(get_db)
):
    """
    Debug endpoint to see raw content from a specific contract lookup page.
    Shows exactly what StockNear returns.
    """
    from app.stocknear import StockNearScraper
    import asyncio
    
    def scrape_sync():
        with StockNearScraper() as scraper:
            contract_id = scraper._build_contract_id(symbol, expiration, option_type, strike)
            url = f"/stocks/{symbol.lower()}/options/contract-lookup?contract={contract_id}"
            
            scraper.navigate(url)
            
            # Try to wait for price data to load
            wait_result = "none"
            try:
                # Wait for any of these indicators that price data loaded
                scraper.page.wait_for_function(
                    """() => {
                        const text = document.body.innerText;
                        return text.includes('Bid') && text.includes('Ask') ||
                               text.includes('Last') && text.includes('Volume') ||
                               text.includes('No data is available');
                    }""",
                    timeout=15000
                )
                wait_result = "price_data_or_no_data"
            except Exception as e:
                wait_result = f"timeout: {str(e)[:100]}"
            
            scraper.page.wait_for_timeout(2000)  # Extra settle time
            
            content = scraper.get_page_text()
            html = scraper.page.content()  # Get full HTML
            
            # Search for price-related content in HTML
            import re
            bid_in_html = bool(re.search(r'["\']bid["\']', html, re.I))
            ask_in_html = bool(re.search(r'["\']ask["\']', html, re.I))
            price_patterns = re.findall(r'\$[\d.]+', content)[:10]
            
            # Check for API responses in page
            api_data_match = re.search(r'__NEXT_DATA__[^>]*>([^<]+)<', html)
            next_data_preview = api_data_match.group(1)[:500] if api_data_match else None
            
            # Take screenshot
            screenshot_path = f"/app/data/debug_{contract_id}.png"
            try:
                scraper.screenshot(screenshot_path)
            except:
                screenshot_path = None
            
            return {
                "contract_id": contract_id,
                "url": f"https://stocknear.com{url}",
                "page_text": content,
                "page_text_length": len(content),
                "html_length": len(html),
                "first_1000_chars": content[:1000],
                "screenshot_path": screenshot_path,
                "wait_result": wait_result,
                "bid_in_html": bid_in_html,
                "ask_in_html": ask_in_html,
                "price_patterns_found": price_patterns,
                "next_data_preview": next_data_preview
            }
    
    result = await asyncio.to_thread(scrape_sync)
    return result


@app.get("/api/debug/yahoo-option/{symbol}")
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


@app.get("/api/debug/quote/{symbol}")
async def debug_quote_method(
    symbol: str,
    expiration: str,
    strike: float,
    option_type: str,
    db: AsyncSession = Depends(get_db)
):
    """
    Debug endpoint that tests the get_contract_quote method with dropdown selection.
    """
    from app.stocknear import StockNearScraper
    import asyncio
    
    def scrape_sync():
        with StockNearScraper() as scraper:
            quote = scraper.get_contract_quote(symbol, expiration, strike, option_type)
            return {
                "contract_id": quote.contract_id,
                "symbol": quote.symbol,
                "expiration": quote.expiration,
                "strike": quote.strike,
                "option_type": quote.option_type,
                "bid": quote.bid,
                "ask": quote.ask,
                "mid": quote.mid,
                "last": quote.last,
                "volume": quote.volume,
                "open_interest": quote.open_interest,
                "iv": quote.implied_volatility,
                "delta": quote.delta,
                "raw_content_length": len(quote.raw_content) if quote.raw_content else 0,
                "raw_content_sample": quote.raw_content[:2000] if quote.raw_content else None
            }
    
    result = await asyncio.to_thread(scrape_sync)
    return result


@app.get("/api/debug/oi/{symbol}")
async def debug_oi(
    symbol: str,
    expiration: str = None,
    db: AsyncSession = Depends(get_db)
):
    """
    Debug endpoint to see the OI page content for a specific expiration.
    This page may contain bid/ask for each strike.
    """
    from app.stocknear import StockNearScraper
    import asyncio
    import re
    
    def scrape_sync():
        with StockNearScraper() as scraper:
            # Navigate to OI page
            url = f"/stocks/{symbol.lower()}/options/oi"
            scraper.navigate(url)
            
            # Wait for data to load
            try:
                scraper.page.wait_for_function(
                    """() => {
                        const text = document.body.innerText;
                        return text.includes('STRIKE') || 
                               text.includes('CALL OI') || 
                               text.includes('PUT OI') ||
                               text.length > 3000;
                    }""",
                    timeout=15000
                )
            except Exception as e:
                pass
            
            scraper.page.wait_for_timeout(2000)
            
            # If expiration specified, try to click on it
            if expiration:
                try:
                    scraper.page.click(f"text={expiration}", timeout=5000)
                    scraper.page.wait_for_timeout(3000)
                except Exception as e:
                    pass
            
            content = scraper.get_page_text()
            html = scraper.page.content()
            
            # Take screenshot
            screenshot_path = f"/app/data/debug_oi_{symbol}.png"
            try:
                scraper.screenshot(screenshot_path)
            except:
                screenshot_path = None
            
            return {
                "symbol": symbol.upper(),
                "expiration": expiration,
                "url": f"https://stocknear.com{url}",
                "page_text_length": len(content),
                "html_length": len(html),
                "sample_content": content[:4000],
                "screenshot_path": screenshot_path
            }
    
    result = await asyncio.to_thread(scrape_sync)
    return result


@app.get("/api/debug/greeks/{symbol}")
async def debug_greeks(
    symbol: str,
    expiration: str = None,
    db: AsyncSession = Depends(get_db)
):
    """
    Debug endpoint to see the Greeks page content for a specific expiration.
    This page contains bid/ask for each strike.
    """
    from app.stocknear import StockNearScraper
    import asyncio
    import re
    
    def scrape_sync():
        with StockNearScraper() as scraper:
            # Navigate to Greeks page
            url = f"/stocks/{symbol.lower()}/options/greeks"
            scraper.navigate(url)
            
            # Wait for data to load
            try:
                scraper.page.wait_for_function(
                    """() => {
                        const text = document.body.innerText;
                        return text.includes('STRIKE') || 
                               text.includes('BID') || 
                               text.includes('ASK') ||
                               text.includes('DELTA');
                    }""",
                    timeout=15000
                )
            except Exception as e:
                pass
            
            scraper.page.wait_for_timeout(2000)
            
            # If expiration specified, try to click on it
            if expiration:
                try:
                    scraper.page.click(f"text={expiration}", timeout=5000)
                    scraper.page.wait_for_timeout(3000)
                except Exception as e:
                    pass
            
            content = scraper.get_page_text()
            html = scraper.page.content()
            
            # Search for bid/ask patterns
            # Format might be: STRIKE | BID | ASK | LAST | ... or similar
            bid_ask_pattern = re.findall(r'(\d+\.?\d*)\s+(\d+\.\d+)\s+(\d+\.\d+)', content)[:20]
            
            # Take screenshot
            screenshot_path = f"/app/data/debug_greeks_{symbol}.png"
            try:
                scraper.screenshot(screenshot_path)
            except:
                screenshot_path = None
            
            return {
                "symbol": symbol.upper(),
                "expiration": expiration,
                "url": f"https://stocknear.com{url}",
                "page_text_length": len(content),
                "html_length": len(html),
                "sample_content": content[:3000],
                "bid_ask_samples": bid_ask_pattern[:10],
                "screenshot_path": screenshot_path
            }
    
    result = await asyncio.to_thread(scrape_sync)
    return result


@app.delete("/api/debug/cache")
async def clear_all_cache(db: AsyncSession = Depends(get_db)):
    """Clear all StockNear cache entries."""
    from sqlalchemy import delete
    from app.models import StockNearCache
    
    result = await db.execute(delete(StockNearCache))
    await db.commit()
    return {"deleted": result.rowcount}


@app.delete("/api/debug/cache/{symbol}")
async def clear_symbol_cache(symbol: str, db: AsyncSession = Depends(get_db)):
    """Clear cache entries for a specific symbol."""
    from sqlalchemy import delete
    from app.models import StockNearCache
    
    result = await db.execute(
        delete(StockNearCache).where(StockNearCache.symbol == symbol.upper())
    )
    await db.commit()
    return {"symbol": symbol.upper(), "deleted": result.rowcount}


@app.get("/api/debug/quote-api/{symbol}")
async def debug_quote_api(
    symbol: str,
    expiration: str,
    strike: float,
    option_type: str,
    db: AsyncSession = Depends(get_db)
):
    """
    Debug endpoint to test the new direct API quote method.
    This is much faster than browser scraping.
    
    Example: /api/debug/quote-api/AAPL?expiration=Jan%2030,%202026&strike=250&option_type=CALL
    """
    from app.stocknear import StockNearScraper
    import asyncio
    
    def fetch_sync():
        with StockNearScraper() as scraper:
            quote = scraper.get_contract_quote_via_api(
                symbol=symbol,
                expiration=expiration,
                strike=strike,
                option_type=option_type
            )
            return {
                "contract_id": quote.contract_id,
                "symbol": quote.symbol,
                "strike": quote.strike,
                "option_type": quote.option_type,
                "expiration": quote.expiration,
                "bid": quote.bid,
                "ask": quote.ask,
                "mid": quote.mid,
                "last": quote.last,
                "volume": quote.volume,
                "open_interest": quote.open_interest,
                "implied_volatility": quote.implied_volatility,
                "delta": quote.delta,
                "gamma": quote.gamma,
                "theta": quote.theta,
                "vega": quote.vega,
                "raw_content_length": len(quote.raw_content),
                "raw_content_preview": quote.raw_content[:500] if quote.raw_content else None
            }
    
    result = await asyncio.to_thread(fetch_sync)
    return result


@app.get("/api/debug/strikes/{symbol}")
async def debug_strikes(symbol: str, raw_html: bool = False):
    """
    Debug endpoint to test get_available_strikes() method.
    Returns available strike prices and expirations for a symbol.
    """
    from app.stocknear import StockNearScraper
    import asyncio
    
    def fetch_sync():
        with StockNearScraper() as scraper:
            result = scraper.get_available_strikes(symbol, return_raw_html=raw_html)
            
            return {
                "symbol": symbol.upper(),
                "current_price": result.get("current_price"),
                "expiration_count": len(result.get("expirations", [])),
                "expirations": result.get("expirations", [])[:15],
                "strike_count": len(result.get("strikes", [])),
                "strikes_sample": result.get("strikes", [])[:30],  # First 30 strikes
                "debug_info": result.get("_debug", {})
            }
    
    result = await asyncio.to_thread(fetch_sync)
    return result


@app.get("/api/debug/assignment-calc")
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
    if price_at_50pct:
        d1_at_50, d2_at_50 = _calculate_d1_d2(price_at_50pct, strike, T, DEFAULT_RISK_FREE_RATE, volatility)
        if option_type == "PUT":
            prob_at_50_price = _norm_cdf(-d2_at_50)
        else:
            prob_at_50_price = _norm_cdf(d2_at_50)
    else:
        d1_at_50 = d2_at_50 = prob_at_50_price = None
    
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

