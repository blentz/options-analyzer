"""
Async service for fetching and caching StockNear data.

This service wraps the synchronous StockNear scraper and provides:
- Async interface for FastAPI integration
- Database-backed caching with configurable TTL
- Thread pool execution for the sync scraper
"""

import asyncio
import json
from datetime import datetime, timedelta
from typing import Optional
from dataclasses import dataclass, asdict

from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import StockNearCache
from app.stocknear import StockNearScraper, OptionsData, StockData


@dataclass
class EnrichedQuote:
    """Stock quote enriched with options data from StockNear."""
    symbol: str
    price: Optional[float] = None
    change: Optional[float] = None
    change_percent: Optional[float] = None
    
    # Options data
    implied_volatility: Optional[float] = None  # As decimal (0.35 = 35%)
    iv_rank: Optional[float] = None  # 0-100
    iv_percentile: Optional[float] = None  # 0-100
    put_call_ratio: Optional[float] = None
    max_pain: Optional[float] = None
    total_open_interest: Optional[int] = None
    
    fetched_at: Optional[datetime] = None


async def get_cached_data(
    db: AsyncSession,
    cache_key: str
) -> Optional[dict]:
    """
    Get cached data if not expired.
    
    Returns None if cache miss or expired.
    """
    stmt = select(StockNearCache).where(
        StockNearCache.cache_key == cache_key,
        StockNearCache.expires_at > datetime.utcnow()
    )
    result = await db.execute(stmt)
    cached = result.scalar_one_or_none()
    
    if cached:
        return json.loads(cached.data_json)
    return None


async def set_cached_data(
    db: AsyncSession,
    cache_key: str,
    data_type: str,
    symbol: str,
    data: dict,
    ttl_seconds: Optional[int] = None
) -> None:
    """
    Store data in cache with TTL.
    
    Uses upsert pattern to handle existing keys.
    """
    ttl = ttl_seconds or settings.stocknear_cache_ttl_seconds
    now = datetime.utcnow()
    expires_at = now + timedelta(seconds=ttl)
    
    # Delete existing entry if any
    await db.execute(
        delete(StockNearCache).where(StockNearCache.cache_key == cache_key)
    )
    
    # Insert new entry
    cache_entry = StockNearCache(
        cache_key=cache_key,
        data_type=data_type,
        symbol=symbol.upper(),
        data_json=json.dumps(data, default=str),
        fetched_at=now,
        expires_at=expires_at
    )
    db.add(cache_entry)
    await db.commit()


async def cleanup_expired_cache(db: AsyncSession) -> int:
    """
    Remove expired cache entries.
    
    Returns number of entries deleted.
    """
    result = await db.execute(
        delete(StockNearCache).where(StockNearCache.expires_at < datetime.utcnow())
    )
    await db.commit()
    return result.rowcount


def _fetch_options_overview_sync(symbol: str) -> dict:
    """Synchronous fetch of options overview - runs in thread pool."""
    with StockNearScraper() as scraper:
        data = scraper.get_options_overview(symbol)
        return asdict(data)


def _fetch_max_pain_sync(symbol: str) -> dict:
    """Synchronous fetch of max pain - runs in thread pool."""
    with StockNearScraper() as scraper:
        data = scraper.get_max_pain(symbol)
        return asdict(data)


def _fetch_stock_overview_sync(symbol: str) -> dict:
    """Synchronous fetch of stock overview - runs in thread pool."""
    with StockNearScraper() as scraper:
        data = scraper.get_stock_overview(symbol)
        return asdict(data)


async def get_options_overview(
    db: AsyncSession,
    symbol: str,
    force_refresh: bool = False
) -> Optional[OptionsData]:
    """
    Get options overview data for a symbol.
    
    Uses database cache with 1-hour TTL.
    Falls back to live scrape if cache miss.
    
    Args:
        db: Database session
        symbol: Stock ticker symbol
        force_refresh: If True, bypass cache and fetch fresh data
    
    Returns:
        OptionsData with IV, OI, volume, etc. or None if fetch fails
    """
    symbol = symbol.upper()
    cache_key = f"options_overview:{symbol}"
    
    # Check cache first (unless force refresh)
    if not force_refresh:
        cached = await get_cached_data(db, cache_key)
        if cached:
            return OptionsData(**cached)
    
    # Fetch fresh data in thread pool
    try:
        data_dict = await asyncio.to_thread(_fetch_options_overview_sync, symbol)
        
        # Cache the result
        await set_cached_data(db, cache_key, "options_overview", symbol, data_dict)
        
        return OptionsData(**data_dict)
    except Exception as e:
        print(f"Error fetching options overview for {symbol}: {e}")
        return None


async def get_max_pain(
    db: AsyncSession,
    symbol: str,
    force_refresh: bool = False
) -> Optional[float]:
    """
    Get max pain price for a symbol.
    
    Returns the max pain strike price or None if unavailable.
    """
    symbol = symbol.upper()
    cache_key = f"max_pain:{symbol}"
    
    if not force_refresh:
        cached = await get_cached_data(db, cache_key)
        if cached and cached.get("max_pain"):
            return cached["max_pain"]
    
    try:
        data_dict = await asyncio.to_thread(_fetch_max_pain_sync, symbol)
        await set_cached_data(db, cache_key, "max_pain", symbol, data_dict)
        return data_dict.get("max_pain")
    except Exception as e:
        print(f"Error fetching max pain for {symbol}: {e}")
        return None


async def get_stock_data(
    db: AsyncSession,
    symbol: str,
    force_refresh: bool = False
) -> Optional[StockData]:
    """
    Get stock overview data from StockNear.
    
    Note: For real-time prices, prefer price_service.get_stock_price()
    which uses Yahoo Finance. This is for additional StockNear-specific data.
    """
    symbol = symbol.upper()
    cache_key = f"stock_overview:{symbol}"
    
    if not force_refresh:
        cached = await get_cached_data(db, cache_key)
        if cached:
            return StockData(**cached)
    
    try:
        data_dict = await asyncio.to_thread(_fetch_stock_overview_sync, symbol)
        await set_cached_data(db, cache_key, "stock_overview", symbol, data_dict)
        return StockData(**data_dict)
    except Exception as e:
        print(f"Error fetching stock data for {symbol}: {e}")
        return None


async def get_enriched_quote(
    db: AsyncSession,
    symbol: str,
    current_price: Optional[float] = None,
    force_refresh: bool = False
) -> EnrichedQuote:
    """
    Get a quote enriched with options data.
    
    Combines price data with IV, max pain, and other options metrics.
    
    Args:
        db: Database session
        symbol: Stock ticker
        current_price: If provided, use this price instead of fetching
        force_refresh: Bypass cache
    
    Returns:
        EnrichedQuote with all available data
    """
    symbol = symbol.upper()
    
    # Fetch options overview (includes IV)
    options_data = await get_options_overview(db, symbol, force_refresh)
    
    # Create enriched quote
    quote = EnrichedQuote(
        symbol=symbol,
        price=current_price,
        fetched_at=datetime.utcnow()
    )
    
    if options_data:
        quote.implied_volatility = options_data.implied_volatility
        quote.iv_rank = options_data.iv_rank
        quote.iv_percentile = options_data.iv_percentile
        quote.put_call_ratio = options_data.put_call_ratio
        quote.total_open_interest = options_data.total_open_interest
    
    # Get max pain separately (different page)
    max_pain = await get_max_pain(db, symbol, force_refresh)
    if max_pain:
        quote.max_pain = max_pain
    
    return quote


async def get_live_iv_for_symbols(
    db: AsyncSession,
    symbols: list[str],
    force_refresh: bool = False
) -> dict[str, Optional[float]]:
    """
    Get implied volatility for multiple symbols.
    
    Returns dict mapping symbol -> IV (as decimal, e.g., 0.35 for 35%)
    """
    results = {}
    
    for symbol in symbols:
        options_data = await get_options_overview(db, symbol, force_refresh)
        if options_data and options_data.implied_volatility:
            results[symbol] = options_data.implied_volatility
        else:
            results[symbol] = None
    
    return results
