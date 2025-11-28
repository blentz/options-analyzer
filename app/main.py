"""FastAPI application for options trading analysis."""

from contextlib import asynccontextmanager
from pathlib import Path

from typing import List
from fastapi import FastAPI, Request, UploadFile, File, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import init_db, get_db
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
    create_risk_summary_chart
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
        position_charts.append({
            "analysis": analysis,
            "script": script,
            "div": div
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
                "scenarios": [
                    {"price": s.underlying_price, "pnl": s.pnl}
                    for s in a.scenarios
                ]
            }
            for a in risk_summary["analyses"]
        ]
    }
