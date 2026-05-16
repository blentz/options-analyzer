"""FastAPI application for options trading analysis."""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from typing import List, Optional
from fastapi import FastAPI, Request, UploadFile, File, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import settings as app_settings
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload

# Configure logging. When LOG_FORMAT=json is set we emit one JSON object
# per line — easier to ingest into log aggregators (Loki/Cloudwatch/etc).
import json as _json
import os as _os


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            base["exc"] = self.formatException(record.exc_info)
        return _json.dumps(base, default=str)


_log_handler = logging.StreamHandler()
if _os.environ.get("LOG_FORMAT", "text").lower() == "json":
    _log_handler.setFormatter(_JsonFormatter())
else:
    _log_handler.setFormatter(
        logging.Formatter(
            "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )

_root = logging.getLogger()
_root.handlers.clear()
_root.addHandler(_log_handler)
_root.setLevel(logging.INFO)

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
    get_strategy_breakdown,
    resolve_date_range,
    PRESET_RANGE_LABELS,
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
    """Initialize database on startup; release persistent browser on shutdown."""
    import asyncio as _asyncio
    from app.services.stocknear_service import (
        shutdown_persistent_scraper, cleanup_expired_cache,
        _get_or_start_persistent_scraper,
    )
    from app.database import async_session

    await init_db()

    # Hourly cache cleanup so the StockNearCache table doesn't grow forever.
    async def _cache_janitor():
        while True:
            try:
                async with async_session() as session:
                    deleted = await cleanup_expired_cache(session)
                    if deleted:
                        logging.getLogger(__name__).info("Cache janitor pruned %d expired entries", deleted)
            except Exception:
                logging.getLogger(__name__).exception("Cache janitor iteration failed")
            await _asyncio.sleep(3600)

    # Pre-warm the Playwright browser in the background. The first scraper
    # call otherwise pays a 5-10s Firefox-launch + cookies-injection cost
    # synchronously inside the user's first /speculation lookup. By spinning
    # it up at startup we let uvicorn finish booting (so /health works
    # immediately) and the browser is hot by the time anyone hits a scraping
    # endpoint. Failure to pre-warm is non-fatal — actual usage will retry.
    async def _prewarm_scraper():
        try:
            await _asyncio.to_thread(_get_or_start_persistent_scraper)
            logging.getLogger(__name__).info("Persistent Playwright scraper pre-warmed")
        except Exception:
            logging.getLogger(__name__).exception(
                "Pre-warm failed — first scraper call will pay the launch cost"
            )

    janitor_task = _asyncio.create_task(_cache_janitor())
    prewarm_task = _asyncio.create_task(_prewarm_scraper())

    try:
        yield
    finally:
        for t in (janitor_task, prewarm_task):
            t.cancel()
            try:
                await t
            except _asyncio.CancelledError:
                pass
        try:
            await _asyncio.to_thread(shutdown_persistent_scraper)
        except Exception:
            pass


app = FastAPI(
    title="Options Trading Analyzer",
    description="Track and analyze options trading performance",
    version="1.0.0",
    lifespan=lifespan
)


# Paths that never require auth — health probes and static assets must work
# even before the API key is sent. Everything else is protected when
# APP_API_KEY is set in the environment.
_OPEN_PATHS = ("/health", "/ready", "/static/")


class APIKeyMiddleware(BaseHTTPMiddleware):
    """If APP_API_KEY is set, require it on every request via either an
    `X-API-Key` header or `?api_key=` query parameter. If unset, the app
    runs open (the previous behaviour) — log this loudly at startup so the
    operator knows.
    """

    def __init__(self, app, api_key: str):
        super().__init__(app)
        self.api_key = api_key

    async def dispatch(self, request: Request, call_next):
        if not self.api_key:
            return await call_next(request)
        path = request.url.path
        if any(path == p or path.startswith(p) for p in _OPEN_PATHS):
            return await call_next(request)
        provided = request.headers.get("x-api-key") or request.query_params.get("api_key")
        if provided != self.api_key:
            return JSONResponse(
                {"detail": "Unauthorized: missing or invalid API key"},
                status_code=401,
            )
        return await call_next(request)


class DebugGateMiddleware(BaseHTTPMiddleware):
    """Block all /api/debug/* routes unless ENABLE_DEBUG_ENDPOINTS=true.

    Debug endpoints return raw scraped HTML, screenshots, and session-bound
    page text. Disabled in production by default to prevent accidental data
    leakage if the app is exposed beyond localhost.
    """

    async def dispatch(self, request: Request, call_next):
        if request.url.path.startswith("/api/debug") and not app_settings.enable_debug_endpoints:
            return JSONResponse(
                {"detail": "Debug endpoints disabled. Set ENABLE_DEBUG_ENDPOINTS=true to enable."},
                status_code=404,
            )
        return await call_next(request)


# Order matters: debug gate first (cheap reject), then API key.
app.add_middleware(APIKeyMiddleware, api_key=app_settings.api_key)
app.add_middleware(DebugGateMiddleware)

# Debug endpoints live in their own router so main.py doesn't carry 900
# lines of scraper-inspection code. The DebugGateMiddleware above 404s
# every route on this router unless ENABLE_DEBUG_ENDPOINTS=true.
from app.routers import debug as _debug_router  # noqa: E402
from app.routers import positions as _positions_router  # noqa: E402
app.include_router(_debug_router.router)
app.include_router(_positions_router.router)

if not app_settings.api_key:
    logging.getLogger(__name__).warning(
        "APP_API_KEY is empty — server is running OPEN. Set api_key in .env "
        "or bind the container to localhost only."
    )
if app_settings.enable_debug_endpoints:
    logging.getLogger(__name__).warning(
        "ENABLE_DEBUG_ENDPOINTS=true — /api/debug/* exposed (raw scraped data)."
    )

# Templates
templates_dir = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(templates_dir))

# Static files
static_dir = Path(__file__).parent.parent / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.post("/api/import/diagnose")
async def import_diagnose(file: UploadFile = File(...)):
    """Upload a single CSV and get back what the parser sees, without
    actually importing anything. Use this to figure out why a file is
    silently producing zero trades.

    Returns: bytes, line count, header detection result, column names,
    first 3 raw rows, first 3 parsed rows, action-keyword tallies.
    """
    from app.services.csv_import import parse_csv_content
    import csv as _csv
    from io import StringIO as _SIO

    content_bytes = await file.read()
    try:
        text = content_bytes.decode('utf-8-sig')
    except UnicodeDecodeError:
        text = content_bytes.decode('latin-1', errors='replace')
    lines = text.splitlines()

    # Mirror parser's header detection so we can report what it found.
    def _first_cell(line: str) -> str:
        s = line.lstrip("﻿").strip()
        if not s:
            return ""
        return s.split(",", 1)[0].strip().strip('"').strip("'").strip()

    header_idx = None
    for i, line in enumerate(lines):
        if _first_cell(line).lower() == "run date":
            header_idx = i
            break

    columns: list[str] = []
    first_rows: list[dict] = []
    action_tally: dict[str, int] = {}
    if header_idx is not None:
        csv_lines = list(lines[header_idx:])
        csv_lines[0] = csv_lines[0].lstrip()
        reader = _csv.DictReader(_SIO('\n'.join(csv_lines)))
        columns = list(reader.fieldnames or [])
        for n, row in enumerate(reader):
            if n < 3:
                first_rows.append({k: (v or '')[:60] for k, v in row.items()})
            action = (row.get('Action') or '').strip()
            if action:
                # Bucket by first keyword to make the tally readable
                kw = action.split()[0] if action else '(blank)'
                action_tally[kw] = action_tally.get(kw, 0) + 1

    parsed_trades, parsed_underlying = parse_csv_content(text)

    return {
        "filename": file.filename,
        "bytes": len(content_bytes),
        "lines": len(lines),
        "first_5_raw_lines": [l[:200] for l in lines[:5]],
        "header_detected_at_line": header_idx,
        "columns": columns,
        "first_3_parsed_rows": first_rows,
        "action_first_word_tally": action_tally,
        "parsed_options_trades": len(parsed_trades),
        "parsed_underlying_trades": len(parsed_underlying),
        "first_options_trade": (
            {"date": str(parsed_trades[0].trade_date), "action": parsed_trades[0].action,
             "symbol": parsed_trades[0].raw_symbol, "amount": str(parsed_trades[0].amount)}
            if parsed_trades else None
        ),
    }


@app.get("/cycles", response_class=HTMLResponse)
async def cycles_page(
    request: Request,
    status: Optional[str] = None,
    range: Optional[str] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    """Wheel cycles list — each cycle is one trade, not N options legs.

    Filters:
      status  ACTIVE | CLOSED | (none = both)
      range   Time window (uses ended_at for CLOSED cycles, started_at
              for ACTIVE ones — both are checked so "Q2 2024" shows
              cycles that overlapped the quarter in either direction).
    """
    from datetime import datetime as _dt
    from app.models import WheelCycle as _WC, WheelCycleMember as _WCM
    from app.services.analytics import _in_range as _inr

    def _parse_iso(s):
        if not s:
            return None
        try:
            return _dt.fromisoformat(s)
        except ValueError:
            return None

    date_range = resolve_date_range(range, _parse_iso(start), _parse_iso(end))

    stmt = select(_WC).options(
        selectinload(_WC.members).selectinload(_WCM.cycle)
    ).order_by(_WC.started_at.desc())
    if status and status.upper() in ("ACTIVE", "CLOSED"):
        stmt = stmt.where(_WC.status == status.upper())

    rows = list((await db.execute(stmt)).scalars().all())

    # Apply the date filter in Python — cycles use started_at vs ended_at
    # depending on status, which is awkward to express in SQL but trivial here.
    if not date_range.is_unbounded:
        kept = []
        for c in rows:
            ref_date = c.ended_at if c.status == "CLOSED" else c.started_at
            if _inr(ref_date, date_range):
                kept.append(c)
        rows = kept

    # Load member positions for each cycle so the UI can render the leg list.
    member_position_ids = [m.position_id for c in rows for m in c.members]
    pos_by_id = {}
    if member_position_ids:
        pstmt = select(OptionPosition).options(
            selectinload(OptionPosition.contract)
        ).where(OptionPosition.id.in_(member_position_ids))
        pos_by_id = {p.id: p for p in (await db.execute(pstmt)).scalars().all()}

    # Build view-models so the template doesn't navigate the SA graph itself.
    # Mark active cycles to market with Yahoo prices so the page shows
    # both realized stock_pnl AND current unrealized exposure on still-
    # held shares. Yahoo is cached 60s so this is cheap.
    from app.services.price_service import get_multiple_prices
    active_symbols = sorted({c.symbol for c in rows if c.status == "ACTIVE" and c.shares_held > 0})
    live_prices = await get_multiple_prices(active_symbols) if active_symbols else {}

    view_rows = []
    for c in rows:
        members_view = []
        for m in sorted(c.members, key=lambda mm: mm.sequence):
            p = pos_by_id.get(m.position_id)
            if not p:
                continue
            members_view.append({
                "role": m.role,
                "sequence": m.sequence,
                "contract": p.contract.contract_id,
                "open_date": p.open_date,
                "close_date": p.close_date,
                "outcome": p.outcome,
                "net_pnl": float(p.net_pnl),
                "is_closed": p.is_closed,
            })
        # Compute unrealized for ACTIVE cycles with held shares.
        live_price = None
        unrealized_stock_pnl = None
        market_value = None
        if c.status == "ACTIVE" and c.shares_held > 0:
            quote = live_prices.get(c.symbol)
            if quote and quote.price is not None:
                live_price = float(quote.price)
                avg_basis_f = float(c.avg_cost_basis or 0)
                unrealized_stock_pnl = (live_price - avg_basis_f) * c.shares_held
                market_value = live_price * c.shares_held

        view_rows.append({
            "id": c.id,
            "symbol": c.symbol,
            "status": c.status,
            "started_at": c.started_at,
            "ended_at": c.ended_at,
            "shares_held": c.shares_held,
            "avg_cost_basis": float(c.avg_cost_basis or 0),
            "options_pnl": float(c.options_pnl),
            "stock_pnl": float(c.stock_pnl),
            "total_pnl": float(c.total_pnl),
            "live_price": live_price,
            "unrealized_stock_pnl": unrealized_stock_pnl,
            "market_value": market_value,
            # Realized + unrealized totals — what the user really wants to see.
            "realized_total": float(c.total_pnl),
            "unrealized_total": unrealized_stock_pnl,
            "grand_total": (
                float(c.total_pnl) + unrealized_stock_pnl
                if unrealized_stock_pnl is not None
                else float(c.total_pnl)
            ),
            "num_puts": c.num_puts,
            "num_calls": c.num_calls,
            "members": members_view,
        })

    return templates.TemplateResponse("cycles.html", {
        "request": request,
        "cycles": view_rows,
        "date_range": date_range,
        "preset_labels": PRESET_RANGE_LABELS,
        "custom_start": start or "",
        "custom_end": end or "",
        "status_filter": (status or "").upper(),
    })


@app.post("/api/cycles/rebuild")
async def rebuild_cycles(db: AsyncSession = Depends(get_db)):
    """One-shot: re-detect wheel cycles for every symbol.

    Useful after a code change to the detector or to recover from any
    state drift. Idempotent — wipes and re-derives each symbol's cycles.
    """
    from app.services.wheel_detection import detect_all_wheel_cycles
    counts = await detect_all_wheel_cycles(db)
    return {"rebuilt": counts, "total_cycles": sum(counts.values())}


@app.get("/health")
async def health():
    """Liveness probe — process is up and accepting requests."""
    return {"status": "ok"}


@app.get("/ready")
async def ready(db: AsyncSession = Depends(get_db)):
    """Readiness probe — DB is reachable. Used by orchestrators to decide
    whether to route traffic to this instance."""
    from sqlalchemy import text as _text
    try:
        await db.execute(_text("SELECT 1"))
        return {"status": "ready"}
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"DB unreachable: {e}")


@app.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    top_n: Optional[int] = 25,
    range: Optional[str] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    """Main dashboard with charts and statistics.

    Query params:
      range  Time window preset: 1M, 3M, 6M, YTD, 1Y, 2Y, 5Y, ALL, or
             "custom" (paired with start/end). Default ALL.
      start  Custom start date "YYYY-MM-DD" (used when range=custom).
      end    Custom end date "YYYY-MM-DD"   (used when range=custom).
      top_n  Max symbols on the P&L-by-symbol chart, ranked by |P&L|.
             Pass `top_n=0` to show every symbol. Default 25.

    Only closed positions are filtered by the time window — the open-
    positions count card always shows what's actually open right now.
    """
    # Parse optional custom start/end dates. Bad input falls through as None
    # rather than 400ing — analytics handles None bounds as "unbounded that side".
    from datetime import datetime as _dt
    def _parse_iso_date(s: Optional[str]) -> Optional[_dt]:
        if not s:
            return None
        try:
            return _dt.fromisoformat(s)
        except ValueError:
            return None

    date_range = resolve_date_range(range, _parse_iso_date(start), _parse_iso_date(end))

    stats = await get_overall_stats(db, date_range=date_range)
    symbol_stats = await get_pnl_by_symbol(db, date_range=date_range)
    monthly_stats = await get_monthly_pnl(db, date_range=date_range)
    cumulative_data = await get_cumulative_pnl(db, date_range=date_range)
    strategy_data = await get_strategy_breakdown(db, date_range=date_range)

    effective_top_n: Optional[int] = top_n if top_n and top_n > 0 else None

    # Generate charts
    cumulative_script, cumulative_div = create_cumulative_pnl_chart(cumulative_data)
    monthly_script, monthly_div = create_monthly_pnl_chart(monthly_stats)
    symbol_script, symbol_div = create_symbol_pnl_chart(symbol_stats, top_n=effective_top_n)
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
        "date_range": date_range,
        "preset_labels": PRESET_RANGE_LABELS,
        "top_n": top_n,
        "custom_start": start or "",
        "custom_end": end or "",
    })


@app.get("/positions", response_class=HTMLResponse)
async def positions_page(
    request: Request,
    closed: bool = False,
    open: bool = False,
    range: Optional[str] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    """View all positions, optionally filtered by status and time window.

    Time-range filter uses overlap semantics — a position is included if
    it was active at any point during the window. See
    `_position_active_in_range` in analytics.py for the exact rule.
    """
    from datetime import datetime as _dt

    def _parse_iso_date(s: Optional[str]) -> Optional[_dt]:
        if not s:
            return None
        try:
            return _dt.fromisoformat(s)
        except ValueError:
            return None

    date_range = resolve_date_range(range, _parse_iso_date(start), _parse_iso_date(end))
    positions = await get_positions(
        db, closed_only=closed, open_only=open, date_range=date_range,
    )

    return templates.TemplateResponse("positions.html", {
        "request": request,
        "positions": positions,
        "show_closed_only": closed,
        "show_open_only": open,
        "date_range": date_range,
        "preset_labels": PRESET_RANGE_LABELS,
        "custom_start": start or "",
        "custom_end": end or "",
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
    from app.config import settings as _settings
    max_bytes = _settings.max_upload_mb * 1024 * 1024

    results = []
    total_imported = 0
    total_skipped = 0
    files_processed = 0
    files_failed = 0

    for file in files:
        # file.filename is Optional per Starlette — guard before .endswith.
        fname = file.filename or "(unnamed)"
        if not fname.endswith('.csv'):
            results.append({
                "filename": fname,
                "success": False,
                "error": "Not a CSV file"
            })
            files_failed += 1
            continue

        try:
            content = await file.read()
            if len(content) > max_bytes:
                results.append({
                    "filename": fname,
                    "success": False,
                    "error": (
                        f"File exceeds {_settings.max_upload_mb}MB limit "
                        f"(got {len(content)/(1024*1024):.1f}MB). "
                        "Raise MAX_UPLOAD_MB if this is legitimate."
                    ),
                })
                files_failed += 1
                continue
            content_str = content.decode('utf-8-sig')
            imported, skipped = await import_csv(db, content_str, fname)

            results.append({
                "filename": fname,
                "success": True,
                "imported": imported,
                "skipped": skipped
            })
            total_imported += imported
            total_skipped += skipped
            files_processed += 1

        except Exception as e:
            results.append({
                "filename": fname,
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
        "position_charts": position_charts,
        "settings": app_settings,
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
        # Grouped totals (treat each spread/condor as one unit). These are
        # the trustworthy portfolio-wide numbers; per-leg sums above can
        # double-count or over-estimate naked risk on spread legs.
        "total_max_profit_grouped": risk_summary.get("total_max_profit_grouped"),
        "total_max_loss_grouped": risk_summary.get("total_max_loss_grouped"),
        "any_group_unbounded": risk_summary.get("any_group_unbounded", False),
        "positions_expiring_soon": risk_summary["positions_expiring_soon"],
        "groups": [
            {
                "symbol": g.symbol,
                "expiration": g.expiration.isoformat(),
                "days_to_expiry": g.days_to_expiry,
                "position_ids": g.position_ids,
                "leg_descriptions": g.leg_descriptions,
                "net_premium": g.net_premium,
                "max_profit": g.max_profit,
                "max_loss": g.max_loss,
                "breakeven_prices": g.breakeven_prices,
                "current_pnl": g.current_pnl,
            }
            for g in risk_summary.get("groups", [])
        ],
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

    # Try to fetch a live contract quote — without it, close-scenario
    # probability uses the OPENING premium as "current option price", which
    # is wildly wrong once the position has moved. The previous behaviour
    # was to silently use opening premium; we now prefer mid > last and
    # report which source the calculation used.
    current_option_price = premium_per_share
    option_price_source = "premium_at_open"
    try:
        from app.services.stocknear_service import get_contract_quote
        exp_str = contract.expiration.strftime("%b %d, %Y")
        live_quote = await get_contract_quote(db, symbol, exp_str, strike, option_type)
        if live_quote and live_quote.mid is not None and live_quote.mid > 0:
            current_option_price = live_quote.mid
            option_price_source = "stocknear_mid"
        elif live_quote and live_quote.last is not None and live_quote.last > 0:
            current_option_price = live_quote.last
            option_price_source = "stocknear_last"
    except Exception as e:
        print(f"Warning: Could not fetch live option price for {contract_id}: {e}")

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
        "current_option_price": round(current_option_price, 4),
        "option_price_source": option_price_source,
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
            current_option_price=current_option_price,
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
        import math as _math
        from app.services.risk_analysis import calculate_close_pnl, calculate_max_risk
        close_pnl = calculate_close_pnl(strategy, premium, close_price, num_contracts)
        max_risk, _ = calculate_max_risk(option_type, strategy, strike, premium, num_contracts, None)
        denom_ok = max_risk and not _math.isinf(max_risk)
        response["close_scenario"] = {
            "close_price_per_share": close_price,
            "close_cost_total": close_price * multiplier,
            "pnl": round(close_pnl, 2),
            "pnl_percent": round((close_pnl / max_risk) * 100, 2) if denom_ok else None,
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
        import math as _math
        from app.services.risk_analysis import (
            calculate_option_pnl_at_expiry, _estimate_delta, calculate_max_risk
        )
        assignment_pnl = calculate_option_pnl_at_expiry(
            option_type, strategy, strike, premium, assignment_price, num_contracts
        )
        assignment_prob = _estimate_delta(option_type, strike, assignment_price, days_to_expiry)
        max_risk, _ = calculate_max_risk(option_type, strategy, strike, premium, num_contracts, None)
        denom_ok = max_risk and not _math.isinf(max_risk)

        response["assignment_scenario"] = {
            "underlying_price": assignment_price,
            "pnl": round(assignment_pnl, 2),
            "pnl_percent": round((assignment_pnl / max_risk) * 100, 2) if denom_ok else None,
            "assignment_probability": round(assignment_prob * 100, 1) if assignment_prob is not None else None,
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
    
    # Live underlying price MUST come from a quote source we trust.
    # Chain-page scraping is too noisy to use as a fallback (it routinely
    # picked up wrong dollar amounts and corrupted every downstream number).
    price_quote = await get_stock_price(symbol)
    current_price = price_quote.price if price_quote else None

    # Options chain (still useful for IV/expirations even though we don't trust its price)
    chain = await get_options_chain(db, symbol)

    if current_price is None:
        raise HTTPException(
            status_code=503,
            detail=(
                f"Could not get live price for {symbol}. Yahoo Finance returned "
                "no quote. Refusing to analyze with a stale/unknown price."
            ),
        )
    
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
        
        # Per-leg breakeven is only meaningful for single-leg strategies.
        # For spreads/condors/butterflies, the leg's breakeven is not a
        # tradeable exit — only the strategy's aggregate breakevens are.
        if len(legs) == 1:
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
        else:
            breakeven = None
        
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
            "breakeven": round(breakeven, 2) if breakeven is not None else None,
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
        # total_premium is already correctly signed: positive for short (received),
        # negative for long (paid). calculate_close_pnl expects signed premium.
        close_result = calculate_close_scenario_with_probability(
            option_type=option_type,
            strategy=strategy,
            strike=strike,
            premium=total_premium,
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
            premium=total_premium,
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
        "spread_quality": quote.spread_quality,
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
                "spread_quality": q.spread_quality,
            }
            for q in quotes
        ]
    }


