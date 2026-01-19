"""FastAPI application for options trading analysis."""

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
