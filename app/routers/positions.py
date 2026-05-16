"""Position-management endpoints: manual groups + per-position IV override.

These are routes that mutate a single OptionPosition's metadata (its group
membership or its volatility-override). They were previously inline in
main.py; extracted here for clarity and to keep main.py focused on the
core dashboard / risk / speculation flows.
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models import OptionPosition

router = APIRouter(tags=["positions"])

class PositionGroupCreate(BaseModel):
    """Body for POST /api/position-groups — create a manual strategy group."""
    label: str
    position_ids: list[int]


@router.post("/api/position-groups")
async def create_position_group(
    body: PositionGroupCreate,
    db: AsyncSession = Depends(get_db),
):
    """Pin a set of positions as one multi-leg strategy. Overrides the
    same-symbol-same-day heuristic that runs by default on the risk page.
    """
    from app.models import PositionGroup as _PG, PositionGroupMember as _PGM

    if not body.position_ids:
        raise HTTPException(400, "position_ids must be non-empty")
    if not body.label.strip():
        raise HTTPException(400, "label must be non-empty")

    # Reject if any of the target positions are already in another manual group
    existing = (await db.execute(
        select(_PGM).where(_PGM.position_id.in_(body.position_ids))
    )).scalars().all()
    if existing:
        raise HTTPException(
            409,
            f"Position(s) {[m.position_id for m in existing]} already belong to "
            f"another manual group. Delete that group first or omit them."
        )

    group = _PG(label=body.label.strip())
    db.add(group)
    await db.flush()
    for pid in body.position_ids:
        db.add(_PGM(group_id=group.id, position_id=pid))
    await db.commit()
    return {"id": group.id, "label": group.label, "position_ids": body.position_ids}


@router.delete("/api/position-groups/{group_id}")
async def delete_position_group(group_id: int, db: AsyncSession = Depends(get_db)):
    """Break a manual group; affected positions fall back to heuristic grouping."""
    from app.models import PositionGroup as _PG
    grp = await db.get(_PG, group_id)
    if not grp:
        raise HTTPException(404, "group not found")
    await db.delete(grp)
    await db.commit()
    return {"deleted": group_id}


@router.get("/api/position-groups")
async def list_position_groups(db: AsyncSession = Depends(get_db)):
    """List all manual position groups."""
    from app.models import PositionGroup as _PG
    res = await db.execute(select(_PG).options(selectinload(_PG.members)))
    groups = res.scalars().all()
    return [
        {
            "id": g.id,
            "label": g.label,
            "created_at": g.created_at.isoformat() if g.created_at else None,
            "position_ids": [m.position_id for m in g.members],
        }
        for g in groups
    ]


class IVOverrideRequest(BaseModel):
    """Body for setting/clearing a per-position IV override.

    `volatility` is decimal (0.45 = 45%). Pass null/None to clear the
    override and revert to live-IV behavior.
    """
    volatility: Optional[float] = None


@router.post("/api/positions/{contract_id_str}/iv-override")
async def set_iv_override(
    contract_id_str: str,
    body: IVOverrideRequest,
    db: AsyncSession = Depends(get_db),
):
    """Set or clear a per-position IV override.

    contract_id_str is the same format the risk page uses, e.g.
    "AAPL 03/20/26 $150.00 PUT". This endpoint is the operator's way to
    say "I don't trust the scraped IV for this contract — use mine."
    """
    import re as _re
    from datetime import datetime as _dt
    from app.models import OptionContract as _OC, OptionPosition as _OP

    match = _re.match(
        r'^(\w+)\s+(\d{2}/\d{2}/\d{2})\s+\$(\d+\.?\d*)\s+(PUT|CALL)$',
        contract_id_str,
    )
    if not match:
        raise HTTPException(400, f"Invalid contract id: {contract_id_str}")
    symbol = match.group(1)
    exp_date = _dt.strptime(match.group(2), "%m/%d/%y").date()
    strike = float(match.group(3))
    option_type = match.group(4)

    if body.volatility is not None and not (0.01 <= body.volatility <= 5.0):
        raise HTTPException(400, "volatility must be between 0.01 and 5.0 (i.e., 1% to 500%)")

    stmt = select(OptionPosition).join(_OC).where(
        _OC.symbol == symbol,
        _OC.expiration == exp_date,
        _OC.strike == strike,
        _OC.option_type == option_type,
    )
    res = await db.execute(stmt)
    pos = res.scalar_one_or_none()
    if not pos:
        raise HTTPException(404, f"Position not found: {contract_id_str}")

    # body.volatility is float|None; the column is Numeric(6,4) which SA maps
    # to Decimal. SQLAlchemy auto-coerces but mypy wants the explicit Decimal.
    from decimal import Decimal as _Decimal
    pos.volatility_override = _Decimal(str(body.volatility)) if body.volatility is not None else None
    await db.commit()
    return {
        "contract_id": contract_id_str,
        "volatility_override": body.volatility,
        "status": "cleared" if body.volatility is None else "set",
    }


