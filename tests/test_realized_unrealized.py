"""Tests for the realized vs unrealized P&L split + chart/headline consistency.

These cover the gap that prompted the rewrite:
  - Dashboard headline = $45k (cycle-aware)
  - Monthly / Cumulative / By-symbol charts = $36k (raw position sum)
A $9k discrepancy meant users saw the headline as one number and the
charts told a different story. After this change every aggregation goes
through the same wheel-aware path.

End-to-end: in-memory SQLite + mocked Yahoo so price lookups stay
deterministic.
"""

from datetime import datetime
from decimal import Decimal
from unittest.mock import patch

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.database import Base
from app.models import (
    OptionContract, OptionPosition, OptionTrade, UnderlyingTrade,
    WheelCycle,
)
from app.services.analytics import (
    DateRange,
    compute_active_cycle_unrealized,
    get_cumulative_pnl,
    get_monthly_pnl,
    get_overall_stats,
    get_pnl_by_symbol,
    resolve_date_range,
)
from app.services.price_service import StockQuote
from app.services.wheel_detection import detect_wheel_cycles_for_symbol


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(text("PRAGMA foreign_keys=ON"))
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as session:
        yield session
    await engine.dispose()


async def _make_full_wheel(db, symbol, put_strike, put_premium,
                            call_strike, call_premium):
    """Helper: closed wheel cycle, one put assigned, one call called away."""
    p_exp = datetime(2025, 1, 17)
    c_exp = datetime(2025, 2, 21)
    put_c = OptionContract(symbol=symbol, expiration=p_exp.date(),
                           strike=Decimal(str(put_strike)), option_type="PUT")
    call_c = OptionContract(symbol=symbol, expiration=c_exp.date(),
                            strike=Decimal(str(call_strike)), option_type="CALL")
    db.add(put_c); db.add(call_c)
    await db.flush()

    put_pos = OptionPosition(
        contract_id=put_c.id, is_closed=True,
        open_date=datetime(2025, 1, 2), close_date=p_exp,
        strategy="SHORT PUT", outcome="ASSIGNED",
        total_premium=Decimal(str(put_premium)), net_pnl=Decimal(str(put_premium)),
        num_contracts=1,
    )
    call_pos = OptionPosition(
        contract_id=call_c.id, is_closed=True,
        open_date=datetime(2025, 1, 20), close_date=c_exp,
        strategy="SHORT CALL", outcome="ASSIGNED",
        total_premium=Decimal(str(call_premium)), net_pnl=Decimal(str(call_premium)),
        num_contracts=1,
    )
    db.add(put_pos); db.add(call_pos)
    await db.flush()
    db.add(UnderlyingTrade(
        position_id=put_pos.id, symbol=symbol, trade_date=p_exp,
        action="BUY", quantity=100, price=Decimal(str(put_strike)),
        amount=Decimal(str(-put_strike * 100)), trade_type="ASSIGNMENT",
    ))
    db.add(UnderlyingTrade(
        position_id=call_pos.id, symbol=symbol, trade_date=c_exp,
        action="SELL", quantity=100, price=Decimal(str(call_strike)),
        amount=Decimal(str(call_strike * 100)), trade_type="ASSIGNMENT",
    ))
    await db.flush()
    return symbol


async def _make_active_wheel_holding(db, symbol, put_strike, put_premium):
    """Helper: ONE put assigned, shares still held, no covered call yet."""
    p_exp = datetime(2025, 3, 21)
    put_c = OptionContract(symbol=symbol, expiration=p_exp.date(),
                           strike=Decimal(str(put_strike)), option_type="PUT")
    db.add(put_c); await db.flush()
    put_pos = OptionPosition(
        contract_id=put_c.id, is_closed=True,
        open_date=datetime(2025, 3, 1), close_date=p_exp,
        strategy="SHORT PUT", outcome="ASSIGNED",
        total_premium=Decimal(str(put_premium)), net_pnl=Decimal(str(put_premium)),
        num_contracts=1,
    )
    db.add(put_pos); await db.flush()
    db.add(UnderlyingTrade(
        position_id=put_pos.id, symbol=symbol, trade_date=p_exp,
        action="BUY", quantity=100, price=Decimal(str(put_strike)),
        amount=Decimal(str(-put_strike * 100)), trade_type="ASSIGNMENT",
    ))
    await db.flush()
    return symbol


def _mock_prices(price_map):
    """Patch get_multiple_prices at its source module so the deferred
    import inside compute_active_cycle_unrealized picks up the fake.
    """
    async def _fake(symbols):
        return {
            s: (StockQuote(symbol=s, price=price_map.get(s), change=0, change_percent=0,
                            timestamp=datetime.now()) if price_map.get(s) is not None else None)
            for s in symbols
        }
    return patch("app.services.price_service.get_multiple_prices", _fake)


@pytest.mark.asyncio
async def test_closed_wheel_cycle_realized_only(db):
    """A finished wheel cycle contributes ONLY realized P&L. Unrealized = 0."""
    await _make_full_wheel(db, "XYZ", put_strike=95, put_premium=200,
                            call_strike=100, call_premium=150)
    await detect_wheel_cycles_for_symbol(db, "XYZ")

    with _mock_prices({}):
        stats = await get_overall_stats(db, DateRange(None, None, "ALL"))

    assert stats.unrealized_pnl == Decimal(0)
    assert stats.realized_pnl == Decimal("850")  # 200 + 500 + 150
    assert stats.total_pnl == Decimal("850")
    assert stats.active_cycle_count == 0


@pytest.mark.asyncio
async def test_active_cycle_unrealized_priced_correctly(db):
    """Active cycle with 100 shares held at $50 basis, marked at $55:
    unrealized = +$500. Closed-options premium still flows to realized."""
    await _make_active_wheel_holding(db, "PEND", put_strike=50, put_premium=100)
    await detect_wheel_cycles_for_symbol(db, "PEND")

    with _mock_prices({"PEND": 55.0}):
        stats = await get_overall_stats(db, DateRange(None, None, "ALL"))

    assert stats.unrealized_pnl == Decimal("500")  # (55 - 50) * 100
    assert stats.realized_pnl == Decimal("100")    # just the premium so far
    assert stats.total_pnl == Decimal("600")
    assert stats.active_cycle_count == 1
    assert stats.active_cycle_shares_held == 100
    assert stats.unrealized_priced_symbols == 1
    assert stats.unrealized_missing_symbols == 0


@pytest.mark.asyncio
async def test_unpriced_symbol_tallied_not_silenced(db):
    """If Yahoo can't price an active cycle's symbol, it's reported as
    a missing-prices count, NOT silently treated as zero unrealized."""
    await _make_active_wheel_holding(db, "BAD", put_strike=10, put_premium=20)
    await detect_wheel_cycles_for_symbol(db, "BAD")

    with _mock_prices({"BAD": None}):
        stats = await get_overall_stats(db, DateRange(None, None, "ALL"))

    assert stats.unrealized_pnl == Decimal(0)
    assert stats.unrealized_missing_symbols == 1
    assert stats.unrealized_priced_symbols == 0


@pytest.mark.asyncio
async def test_charts_and_headline_agree(db):
    """Regression for the $9k discrepancy: per-symbol, monthly, and
    cumulative chart totals must equal the dashboard realized total
    (cycles count as one event each, member positions excluded)."""
    await _make_full_wheel(db, "AAA", 95, 200, 100, 150)   # +850
    await _make_full_wheel(db, "BBB", 50, 80, 55, 60)      # +80 + 500 + 60 = +640
    await detect_wheel_cycles_for_symbol(db, "AAA")
    await detect_wheel_cycles_for_symbol(db, "BBB")

    with _mock_prices({}):
        stats = await get_overall_stats(db, DateRange(None, None, "ALL"))
        sym = await get_pnl_by_symbol(db, DateRange(None, None, "ALL"))
        monthly = await get_monthly_pnl(db, DateRange(None, None, "ALL"))
        cumul = await get_cumulative_pnl(db, DateRange(None, None, "ALL"))

    sym_sum = sum((s.pnl for s in sym), Decimal(0))
    monthly_sum = sum((m.pnl for m in monthly), Decimal(0))
    cumul_final = cumul[-1][1] if cumul else Decimal(0)

    assert stats.realized_pnl == Decimal("1490")  # 850 + 640
    # Every chart must equal the realized total — no $9k drift.
    assert sym_sum == stats.realized_pnl
    assert monthly_sum == stats.realized_pnl
    assert cumul_final == stats.realized_pnl


@pytest.mark.asyncio
async def test_per_symbol_carries_realized_and_unrealized(db):
    """SymbolStats must expose both axes so the chart can stack them."""
    await _make_full_wheel(db, "DONE", 100, 200, 110, 150)  # realized only
    await _make_active_wheel_holding(db, "LIVE", 80, 100)   # unrealized too
    await detect_wheel_cycles_for_symbol(db, "DONE")
    await detect_wheel_cycles_for_symbol(db, "LIVE")

    with _mock_prices({"LIVE": 90.0}):
        sym = {s.symbol: s for s in await get_pnl_by_symbol(db, DateRange(None, None, "ALL"))}

    assert "DONE" in sym and "LIVE" in sym
    # DONE: realized only (200 premium + 1000 stock + 150 premium = 1350).
    assert sym["DONE"].realized == Decimal("1350")
    assert sym["DONE"].unrealized == Decimal("0")
    assert sym["DONE"].pnl == Decimal("1350")
    # LIVE: realized = 100 premium; unrealized = (90-80)*100 = 1000.
    assert sym["LIVE"].realized == Decimal("100")
    assert sym["LIVE"].unrealized == Decimal("1000")
    assert sym["LIVE"].pnl == Decimal("1100")


@pytest.mark.asyncio
async def test_compute_active_cycle_unrealized_handles_no_active(db):
    """Empty case — no active cycles → returns zeros, no Yahoo calls."""
    snap = await compute_active_cycle_unrealized(db)
    assert snap.total == Decimal(0)
    assert snap.active_cycle_count == 0
    assert snap.symbols_priced == 0 and snap.symbols_missing == 0
