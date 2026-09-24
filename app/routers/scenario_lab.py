"""Scenario Lab endpoint: one-call holistic analysis for a single open position.

The risk page loads this lazily per position card. It gathers the live
contract market (bid/ask/mid, scraped IV, greeks), picks a volatility that
is consistent with that market, and returns the target-price conditions and
a spot x time P&L matrix from app.services.exit_conditions.
"""

import logging
import re
from dataclasses import asdict
from datetime import date, datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models import OptionContract, OptionPosition
from app.services.bs_math import (
    DEFAULT_RISK_FREE_RATE,
    calculate_option_greeks,
    calculate_option_price,
    solve_iv_for_option_price,
)
from app.services.exit_conditions import (
    build_target_conditions,
    build_value_matrix,
    parse_grid_percents,
    select_volatility,
)
from app.services.risk_analysis import calculate_close_pnl

logger = logging.getLogger(__name__)

router = APIRouter(tags=["risk"])

_CONTRACT_ID_RE = re.compile(r'^(\w+)\s+(\d{2}/\d{2}/\d{2})\s+\$(\d+\.?\d*)\s+(PUT|CALL)$')


async def _find_open_position(db: AsyncSession, contract_id: str) -> OptionPosition:
    match = _CONTRACT_ID_RE.match(contract_id)
    if not match:
        raise HTTPException(400, f"Invalid contract id: {contract_id}")
    stmt = (
        select(OptionPosition)
        .options(selectinload(OptionPosition.contract))
        .join(OptionContract)
        .where(
            OptionContract.symbol == match.group(1),
            OptionContract.expiration == datetime.strptime(match.group(2), "%m/%d/%y").date(),
            OptionContract.strike == float(match.group(3)),
            OptionContract.option_type == match.group(4),
        )
        # A rolled contract can have closed positions too; prefer the open one.
        .order_by(OptionPosition.is_closed)
    )
    position = (await db.execute(stmt)).scalars().first()
    if position is None:
        raise HTTPException(404, f"Position not found: {contract_id}")
    return position


@router.get("/api/risk/scenario-lab")
async def api_scenario_lab(
    contract_id: str,
    target_price: Optional[float] = None,
    volatility: Optional[float] = None,
    grid_pct: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    """
    Holistic analysis for one position.

    Args:
        contract_id: Risk-page id, e.g. "HITI 10/16/26 $2.50 PUT".
        target_price: Option price per share to solve conditions for.
        volatility: Manual IV as decimal. Omit to auto-select from live data.
        grid_pct: Checkpoints as percents of days to expiry, e.g. "75,50,33,20".
            Omit for the default. Fixed 7/5/3/1-day points are always added.
    """
    from app.services.price_service import get_stock_price
    from app.services.stocknear_service import get_contract_quote, get_options_overview

    try:
        fractions = parse_grid_percents(grid_pct)
    except ValueError as e:
        raise HTTPException(400, str(e))

    position = await _find_open_position(db, contract_id)
    contract = position.contract
    symbol = contract.symbol
    strike = float(contract.strike)
    option_type = contract.option_type
    strategy = position.strategy
    premium = float(position.total_premium)
    num_contracts = position.num_contracts or 1
    premium_per_share = abs(premium) / (100 * num_contracts)
    today = date.today()
    days_to_expiry = (contract.expiration - today).days

    stock = await get_stock_price(symbol)
    if stock is None:
        raise HTTPException(503, f"No underlying price for {symbol}")
    spot = stock.price

    overview = None
    try:
        overview = await get_options_overview(db, symbol)
    except Exception as e:
        logger.warning("scenario-lab: options overview failed for %s: %s", symbol, e)

    quote = None
    try:
        quote = await get_contract_quote(
            db, symbol, contract.expiration.strftime("%b %d, %Y"), strike, option_type
        )
    except Exception as e:
        logger.warning("scenario-lab: contract quote failed for %s: %s", contract_id, e)

    market_price, market_price_source = None, None
    if quote and quote.mid:
        market_price, market_price_source = quote.mid, "mid"
    elif quote and quote.last:
        market_price, market_price_source = quote.last, "last"
    mid_iv = (
        solve_iv_for_option_price(option_type, spot, strike, days_to_expiry, market_price)
        if market_price else None
    )

    candidates = {
        "override": float(position.volatility_override) if position.volatility_override is not None else None,
        "contract": quote.implied_volatility if quote else None,
        "implied_from_mid": mid_iv,
        "symbol": overview.implied_volatility if overview else None,
    }
    if volatility is not None:
        vol, vol_source = max(0.01, min(5.0, volatility)), "manual"
    else:
        vol, vol_source = select_volatility(
            candidates["override"], candidates["contract"], mid_iv,
            quote.spread_quality if quote else None, candidates["symbol"],
        )

    model_price = calculate_option_price(option_type, spot, strike, days_to_expiry, vol)
    greeks = calculate_option_greeks(option_type, spot, strike, days_to_expiry, vol)

    response = {
        "contract_id": contract_id,
        "position": {
            "symbol": symbol,
            "strike": strike,
            "option_type": option_type,
            "strategy": strategy,
            "num_contracts": num_contracts,
            "premium": premium,
            "premium_per_share": round(premium_per_share, 4),
            "expiration": contract.expiration.isoformat(),
            "days_to_expiry": days_to_expiry,
        },
        "underlying": {
            "price": spot,
            "change_percent": stock.change_percent,
            "iv_rank": overview.iv_rank if overview else None,
            "iv_percentile": overview.iv_percentile if overview else None,
            "historical_volatility": overview.historical_volatility if overview else None,
            "max_pain": overview.max_pain if overview else None,
        },
        "contract_market": {
            "bid": quote.bid, "ask": quote.ask, "mid": quote.mid, "last": quote.last,
            "volume": quote.volume, "open_interest": quote.open_interest,
            "spread_quality": quote.spread_quality,
            "delta": quote.delta, "theta": quote.theta,
        } if quote else None,
        "market_price": market_price,
        "market_price_source": market_price_source,
        "volatility": {
            "used": vol,
            "source": vol_source,
            "candidates": candidates,
        },
        "model": {
            "price": round(model_price, 4),
            "vs_market": round(model_price - market_price, 4) if market_price else None,
            **{k: round(v, 5) for k, v in greeks.items()},
            "risk_free_rate": DEFAULT_RISK_FREE_RATE,
        },
        "grid_percents": [round(f * 100, 4) for f in fractions],
        "value_matrix": asdict(build_value_matrix(
            option_type, strike, spot, days_to_expiry, vol, strategy,
            premium_per_share, num_contracts, today, fractions=fractions,
        )) if days_to_expiry > 0 else None,
    }

    if target_price is not None and target_price > 0 and days_to_expiry > 0:
        response["target"] = {
            "close_pnl": round(calculate_close_pnl(strategy, premium, target_price, num_contracts), 2),
            **asdict(build_target_conditions(
                option_type, strike, spot, days_to_expiry, vol, target_price, today,
                fractions=fractions,
            )),
        }

    return response
