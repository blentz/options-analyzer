"""Wheel-cycle detection.

Walks each symbol's option positions + linked underlying trades in
chronological order and identifies wheel cycles: contiguous chains of
SHORT PUT → (shares acquired by assignment) → SHORT CALL → (shares
released by call assignment) where the cycle's holding period stays
above zero. A symbol can have multiple cycles over its lifetime; they
are independent for win/loss and P&L purposes.

Design choices:

  - Full rebuild per symbol on every detection run. Idempotent and
    simpler than incremental update; cycle counts are O(positions) so
    a rebuild for one ticker is cheap.
  - Detection is invoked from import_csv after positions and underlying
    trades land. A `/api/cycles/rebuild` endpoint also kicks it off for
    every symbol — useful after schema changes or for manual re-runs.
  - Stock cost basis is tracked as a weighted moving average. When a
    sale happens, realized P&L = (sale price − avg basis) × qty.
    Avg basis updates only on BUYs (assignments) so sequential covered
    calls don't perturb basis between fills.
  - Underlying trades only land in our DB when the linker matches them
    to an assigned option position. Manual stock sales unrelated to a
    call assignment are not currently captured — cycle holdings won't
    drain in that case. Document, don't fix in this pass.

Limitations to revisit when motivated:
  - Lot-level basis tracking (FIFO/LIFO/specific-id). Average basis is
    fine for most wheel users.
  - Symbol changes (splits, mergers, ticker renames) aren't followed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import (
    OptionContract,
    OptionPosition,
    UnderlyingTrade,
    WheelCycle,
    WheelCycleMember,
)

logger = logging.getLogger(__name__)


@dataclass(order=True)
class _Event:
    """Sortable timeline event used by the state machine."""
    when: datetime
    # Lower priority numbers happen first within the same timestamp.
    # OPEN before STOCK before CLOSE keeps positions visible before
    # their assignments and stops them being closed prematurely.
    priority: int
    kind: str  # "OPEN" | "STOCK" | "CLOSE"
    position: Optional[OptionPosition] = None
    underlying: Optional[UnderlyingTrade] = None


def _build_timeline(positions: list[OptionPosition]) -> list[_Event]:
    """Build a chronological event stream for one symbol."""
    events: list[_Event] = []
    for p in positions:
        events.append(_Event(p.open_date, 0, "OPEN", position=p))
        for ut in p.underlying_trades:
            events.append(_Event(ut.trade_date, 1, "STOCK", position=p, underlying=ut))
        if p.is_closed and p.close_date:
            events.append(_Event(p.close_date, 2, "CLOSE", position=p))
    events.sort()
    return events


def _is_short_put(p: OptionPosition) -> bool:
    return "SHORT" in p.strategy and "PUT" in p.strategy


def _is_short_call(p: OptionPosition) -> bool:
    return "SHORT" in p.strategy and "CALL" in p.strategy


async def detect_wheel_cycles_for_symbol(db: AsyncSession, symbol: str) -> int:
    """Re-detect every wheel cycle for one symbol.

    Wipes existing WheelCycle rows for the symbol and rebuilds from scratch
    so this is safely re-runnable after any data change. Returns the
    number of cycles persisted.
    """
    symbol = symbol.upper()

    # Wipe existing cycles + their members. SQLAlchemy's `cascade=
    # "all, delete-orphan"` runs in-session, but with PRAGMA foreign_keys
    # ON the database itself rejects the parent DELETE before the cascade
    # runs unless we either (a) ondelete=CASCADE the FK (needs a migration)
    # or (b) delete the children first. We do (b) — explicit, portable,
    # and one extra query per rebuild is trivial.
    existing_ids = [
        cid for (cid,) in (await db.execute(
            select(WheelCycle.id).where(WheelCycle.symbol == symbol)
        )).all()
    ]
    if existing_ids:
        await db.execute(
            delete(WheelCycleMember).where(WheelCycleMember.cycle_id.in_(existing_ids))
        )
        await db.execute(delete(WheelCycle).where(WheelCycle.id.in_(existing_ids)))

    # Pull all positions for this symbol ordered by open_date, with their
    # contracts and underlying trades eager-loaded.
    stmt = (
        select(OptionPosition)
        .join(OptionContract)
        .where(OptionContract.symbol == symbol)
        .options(
            selectinload(OptionPosition.contract),
            selectinload(OptionPosition.underlying_trades),
        )
        .order_by(OptionPosition.open_date)
    )
    positions = list((await db.execute(stmt)).scalars().all())
    if not positions:
        return 0

    events = _build_timeline(positions)

    current: Optional[WheelCycle] = None
    members_buffer: list[tuple[int, str]] = []  # (position_id, role) in order added
    cycles_to_persist: list[tuple[WheelCycle, list[tuple[int, str]]]] = []

    shares_held = 0
    avg_basis = Decimal(0)
    open_positions_in_cycle: set[int] = set()
    # Track positions that contribute options_pnl to the active cycle so a
    # stray covered call attached after the cycle ended doesn't double-count.
    options_pnl_sum = Decimal(0)
    stock_pnl_sum = Decimal(0)

    def _open_new_cycle(at: datetime) -> WheelCycle:
        nonlocal options_pnl_sum, stock_pnl_sum, members_buffer
        c = WheelCycle(
            symbol=symbol,
            started_at=at,
            status="ACTIVE",
            shares_held=0,
            avg_cost_basis=Decimal(0),
        )
        members_buffer = []
        options_pnl_sum = Decimal(0)
        stock_pnl_sum = Decimal(0)
        return c

    def _close_cycle(at: datetime) -> None:
        nonlocal current
        if current is None:
            return
        current.ended_at = at
        current.status = "CLOSED"
        current.options_pnl = options_pnl_sum
        current.stock_pnl = stock_pnl_sum
        current.total_pnl = options_pnl_sum + stock_pnl_sum
        current.num_puts = sum(1 for _, r in members_buffer if r == "CSP")
        current.num_calls = sum(1 for _, r in members_buffer if r == "CC")
        current.shares_held = 0
        current.avg_cost_basis = Decimal(0)
        cycles_to_persist.append((current, list(members_buffer)))
        current = None

    for evt in events:
        if evt.kind == "OPEN":
            p = evt.position
            assert p is not None
            if _is_short_put(p):
                # Starts a cycle if none is open. Otherwise this put is
                # absorbed into the active cycle (e.g., user sold a second
                # CSP while still holding shares from the first).
                if current is None:
                    current = _open_new_cycle(evt.when)
                members_buffer.append((p.id, "CSP"))
                open_positions_in_cycle.add(p.id)
                # Premium is recognised when the position CLOSES so we
                # capture buybacks and assignments uniformly.
            elif _is_short_call(p) and current is not None and shares_held > 0:
                # Covered call against held shares.
                members_buffer.append((p.id, "CC"))
                open_positions_in_cycle.add(p.id)
            # Long options, naked calls when no shares held, etc. are not
            # part of any wheel cycle.

        elif evt.kind == "STOCK":
            ut = evt.underlying
            assert ut is not None
            if current is None:
                # Stock movement with no active cycle — possibly a
                # stand-alone stock trade. Ignore for cycle detection.
                continue
            qty = int(ut.quantity)
            price = Decimal(ut.price)
            if ut.action == "BUY":
                # Acquired shares (typically put assignment). Update the
                # weighted average cost basis.
                new_total_value = avg_basis * shares_held + price * qty
                shares_held += qty
                avg_basis = (
                    new_total_value / shares_held if shares_held > 0 else Decimal(0)
                )
            elif ut.action == "SELL":
                # Realize stock P&L vs current avg basis. Don't change
                # avg basis on a sale.
                sell_qty = min(qty, shares_held)
                realized = (price - avg_basis) * sell_qty
                stock_pnl_sum += realized
                shares_held -= sell_qty
                if shares_held == 0:
                    avg_basis = Decimal(0)

        elif evt.kind == "CLOSE":
            p = evt.position
            assert p is not None
            if p.id in open_positions_in_cycle:
                # Add this position's net premium to the cycle. p.net_pnl
                # is the sum of options trade amounts already, so it
                # correctly captures opening credit minus any buyback.
                options_pnl_sum += Decimal(p.net_pnl)
                open_positions_in_cycle.discard(p.id)

        # After every event, check if the cycle has fully closed: no
        # shares held AND no still-open positions in the cycle.
        if current is not None and shares_held == 0 and not open_positions_in_cycle:
            _close_cycle(evt.when)

    # If a cycle is still active at the end of the timeline, persist it.
    if current is not None:
        current.shares_held = shares_held
        current.avg_cost_basis = avg_basis
        # Sum premiums from all member positions (closed ones already
        # added; open ones contribute their current net_pnl so live
        # P&L is sensible).
        member_ids = [pid for pid, _ in members_buffer]
        pos_by_id = {p.id: p for p in positions}
        current.options_pnl = options_pnl_sum + sum(
            Decimal(pos_by_id[pid].net_pnl)
            for pid in member_ids
            if pid in open_positions_in_cycle and pid in pos_by_id
        )
        current.stock_pnl = stock_pnl_sum
        current.total_pnl = current.options_pnl + current.stock_pnl
        current.num_puts = sum(1 for _, r in members_buffer if r == "CSP")
        current.num_calls = sum(1 for _, r in members_buffer if r == "CC")
        cycles_to_persist.append((current, list(members_buffer)))

    # Persist all cycles + members in one flush.
    for cycle, member_list in cycles_to_persist:
        db.add(cycle)
        await db.flush()  # need cycle.id for FK
        for seq, (pid, role) in enumerate(member_list):
            db.add(WheelCycleMember(cycle_id=cycle.id, position_id=pid, role=role, sequence=seq))

    logger.info(
        "Detected %d wheel cycle(s) for %s (positions=%d)",
        len(cycles_to_persist), symbol, len(positions),
    )
    return len(cycles_to_persist)


async def detect_all_wheel_cycles(db: AsyncSession) -> dict[str, int]:
    """Rebuild wheel cycles for every symbol that has any positions.

    Returns symbol -> cycle count. Use after a bulk re-import or as a
    one-shot recovery from the `/api/cycles/rebuild` endpoint.
    """
    symbols_stmt = select(OptionContract.symbol).distinct()
    symbols = [s for (s,) in (await db.execute(symbols_stmt)).all()]
    counts: dict[str, int] = {}
    for sym in symbols:
        counts[sym] = await detect_wheel_cycles_for_symbol(db, sym)
    await db.commit()
    return counts


async def position_ids_in_cycles(db: AsyncSession) -> set[int]:
    """Set of OptionPosition IDs that participate in any wheel cycle.

    Used by analytics to deduplicate: cycles count as ONE trade for
    win/loss; their constituent option positions don't independently
    contribute to the win-rate denominator.
    """
    stmt = select(WheelCycleMember.position_id)
    return {pid for (pid,) in (await db.execute(stmt)).all()}
