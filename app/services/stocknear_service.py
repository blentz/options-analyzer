"""
Async service for fetching and caching StockNear data.

This service wraps the synchronous StockNear scraper and provides:
- Async interface for FastAPI integration
- Database-backed caching with configurable TTL
- Thread pool execution for the sync scraper
"""

import asyncio
import json
import logging
import threading
import time as _time
from datetime import datetime, timedelta
from typing import Optional, Callable
from dataclasses import dataclass, asdict

logger = logging.getLogger(__name__)


# Serialize Playwright browser launches. Multiple concurrent calls to the
# scraper (e.g., user has /risk open while making a calculate-exit request)
# would each spawn a Firefox instance, contend on the LibreWolf cookies.sqlite
# read, and fight for CPU/memory. A semaphore of 1 keeps everything serial;
# raise the value if you've benchmarked your host with parallel browsers.
_SCRAPER_CONCURRENCY = 1
_scraper_semaphore = asyncio.Semaphore(_SCRAPER_CONCURRENCY)


# Persistent Playwright scraper. Launching Firefox + injecting cookies costs
# ~3-5s per call; that was paid on every single request to /risk and every
# exit-calc click. We keep one StockNearScraper alive across requests and
# auto-recycle it periodically to recover from gradual browser-state issues
# (memory growth, cookie staleness, etc).
_persistent_scraper = None              # type: ignore[assignment]
_persistent_lock = threading.Lock()
_persistent_started_at: float = 0.0
_persistent_request_count: int = 0
_PERSISTENT_MAX_AGE_SECONDS = 60 * 60       # 1 hour
_PERSISTENT_MAX_REQUESTS = 200              # rotate after 200 calls


def _get_or_start_persistent_scraper():
    """Lazy-singleton accessor. Must be called from a thread (not async ctx).

    Recycles the scraper when it's older than _PERSISTENT_MAX_AGE_SECONDS or
    has served _PERSISTENT_MAX_REQUESTS calls — whichever comes first. This
    keeps long-running processes from accumulating Firefox memory / stale
    cookies indefinitely.
    """
    from app.stocknear import StockNearScraper
    global _persistent_scraper, _persistent_started_at, _persistent_request_count

    with _persistent_lock:
        recycle = False
        now = _time.time()
        if _persistent_scraper is None:
            recycle = True
        elif now - _persistent_started_at > _PERSISTENT_MAX_AGE_SECONDS:
            logger.info("Persistent scraper exceeded max age — recycling")
            recycle = True
        elif _persistent_request_count >= _PERSISTENT_MAX_REQUESTS:
            logger.info("Persistent scraper hit max request count — recycling")
            recycle = True

        if recycle:
            if _persistent_scraper is not None:
                try:
                    _persistent_scraper.close()
                except Exception as e:
                    logger.warning("Error closing old persistent scraper: %s", e)
            _persistent_scraper = StockNearScraper()
            _persistent_scraper.start()
            _persistent_started_at = now
            _persistent_request_count = 0

        _persistent_request_count += 1
        return _persistent_scraper


def shutdown_persistent_scraper():
    """Call from app shutdown hook to release the browser."""
    global _persistent_scraper
    with _persistent_lock:
        if _persistent_scraper is not None:
            try:
                _persistent_scraper.close()
            except Exception:
                pass
            _persistent_scraper = None


async def run_scraper(sync_fn: Callable, *args, **kwargs):
    """Run a synchronous scraper function in a thread, serialized by the
    global scraper semaphore. All callers in this module should funnel through
    this helper rather than calling asyncio.to_thread directly so that the
    concurrency cap is enforced uniformly.

    If `sync_fn` accepts a `scraper` keyword (introspection-style), we pass
    the persistent singleton in. Older sync helpers that build their own
    `with StockNearScraper():` context are unaffected — they just won't get
    the persistent scraper, which is a perf regression rather than a
    correctness one. New code should accept `scraper=`.
    """
    async with _scraper_semaphore:
        return await asyncio.to_thread(sync_fn, *args, **kwargs)

from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import StockNearCache
from app.stocknear import StockNearScraper, OptionsData, StockData, OptionsChain, OptionContract, ContractQuote


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
    cache_key: str,
    include_expired: bool = False
) -> Optional[dict]:
    """
    Get cached data.
    
    Args:
        db: Database session
        cache_key: The cache key to look up
        include_expired: If True, return data even if expired (for fallback/merging)
    
    Returns None if cache miss.
    """
    if include_expired:
        # Get any cached data, regardless of expiration
        stmt = select(StockNearCache).where(
            StockNearCache.cache_key == cache_key
        )
    else:
        # Only get non-expired data
        stmt = select(StockNearCache).where(
            StockNearCache.cache_key == cache_key,
            StockNearCache.expires_at > datetime.utcnow()
        )
    
    result = await db.execute(stmt)
    cached = result.scalar_one_or_none()
    
    if cached:
        is_expired = cached.expires_at < datetime.utcnow()
        logger.debug(
            "Cache HIT for %s (expired=%s, include_expired=%s)",
            cache_key, is_expired, include_expired
        )
        return json.loads(cached.data_json)
    
    logger.debug("Cache MISS for %s (include_expired=%s)", cache_key, include_expired)
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
    logger.debug("Cached %s for %s (TTL=%ds)", data_type, symbol, ttl)


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
    """Synchronous fetch of options overview - runs in thread pool.

    Uses the persistent browser context so a hot app doesn't pay the
    3-5s Firefox launch cost on every call.
    """
    scraper = _get_or_start_persistent_scraper()
    data = scraper.get_options_overview(symbol)
    return asdict(data)


def _fetch_max_pain_sync(symbol: str) -> dict:
    scraper = _get_or_start_persistent_scraper()
    data = scraper.get_max_pain(symbol)
    return asdict(data)


def _fetch_stock_overview_sync(symbol: str) -> dict:
    scraper = _get_or_start_persistent_scraper()
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
    
    IMPORTANT: When fetching fresh data, merges with cached data to preserve
    last-known values for fields that come back null (e.g., when markets closed).
    
    Args:
        db: Database session
        symbol: Stock ticker symbol
        force_refresh: If True, bypass cache and fetch fresh data
    
    Returns:
        OptionsData with IV, OI, volume, etc. or None if fetch fails
    """
    symbol = symbol.upper()
    cache_key = f"options_overview:{symbol}"
    
    # Check for valid (non-expired) cache first
    if not force_refresh:
        valid_cache = await get_cached_data(db, cache_key, include_expired=False)
        if valid_cache:
            logger.debug("Returning valid cached options overview for %s", symbol)
            return OptionsData(**valid_cache)
    
    # Get any cached data (including expired) for merging with fresh data
    cached = await get_cached_data(db, cache_key, include_expired=True)
    
    # Fetch fresh data in thread pool
    logger.info("Fetching fresh options overview for %s (force_refresh=%s, has_expired_cache=%s)", symbol, force_refresh, cached is not None)
    try:
        fresh_dict = await run_scraper(_fetch_options_overview_sync, symbol)
        
        # Log what we got from the scraper
        fresh_iv = fresh_dict.get('implied_volatility')
        fresh_iv_rank = fresh_dict.get('iv_rank')
        logger.debug(
            "Fresh data for %s: IV=%s, IV_rank=%s",
            symbol, fresh_iv, fresh_iv_rank
        )
        
        # Merge fresh data with cached data - preserve last-known values for null fields
        if cached:
            merged_dict = dict(cached)  # Start with cached values
            merged_fields = []
            for key, value in fresh_dict.items():
                if value is not None and key != 'raw_content':
                    if cached.get(key) != value:
                        merged_fields.append(f"{key}: {cached.get(key)} -> {value}")
                    merged_dict[key] = value  # Only overwrite if fresh value is not null
                elif value is None and cached.get(key) is not None:
                    logger.debug("Preserving cached %s=%s (fresh was null)", key, cached.get(key))
            # Always update raw_content if present
            if fresh_dict.get('raw_content'):
                merged_dict['raw_content'] = fresh_dict['raw_content']
            final_dict = merged_dict
            if merged_fields:
                logger.debug("Merged fields for %s: %s", symbol, ", ".join(merged_fields[:5]))
        else:
            final_dict = fresh_dict
        
        # Cache the merged result
        await set_cached_data(db, cache_key, "options_overview", symbol, final_dict)
        
        return OptionsData(**final_dict)
    except Exception as e:
        logger.error("Error fetching options overview for %s: %s", symbol, e)
        # On error, return cached data if available
        if cached:
            logger.info("Falling back to cached data for %s", symbol)
            return OptionsData(**cached)
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
            logger.debug("Returning cached max pain for %s: %s", symbol, cached["max_pain"])
            return cached["max_pain"]
    
    logger.info("Fetching fresh max pain for %s", symbol)
    try:
        data_dict = await run_scraper(_fetch_max_pain_sync, symbol)
        await set_cached_data(db, cache_key, "max_pain", symbol, data_dict)
        logger.debug("Max pain for %s: %s", symbol, data_dict.get("max_pain"))
        return data_dict.get("max_pain")
    except Exception as e:
        logger.error("Error fetching max pain for %s: %s", symbol, e)
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
            logger.debug("Returning cached stock data for %s", symbol)
            return StockData(**cached)
    
    logger.info("Fetching fresh stock data for %s", symbol)
    try:
        data_dict = await run_scraper(_fetch_stock_overview_sync, symbol)
        await set_cached_data(db, cache_key, "stock_overview", symbol, data_dict)
        return StockData(**data_dict)
    except Exception as e:
        logger.error("Error fetching stock data for %s: %s", symbol, e)
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


def _fetch_options_chain_sync(symbol: str) -> dict:
    """Synchronous fetch of options chain - runs in thread pool."""
    scraper = _get_or_start_persistent_scraper()
    chain = scraper.get_options_chain_parsed(symbol)
    return {
        "symbol": chain.symbol,
        "current_price": chain.current_price,
        "expirations": chain.expirations,
        "iv_rank": chain.iv_rank,
        "iv_percentile": chain.iv_percentile,
        "implied_volatility": chain.implied_volatility,
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
        ],
        "raw_content": chain.raw_content[:1000] if chain.raw_content else "",
    }


def _fetch_expirations_sync(symbol: str) -> list[str]:
    """Synchronous fetch of available expirations - runs in thread pool."""
    scraper = _get_or_start_persistent_scraper()
    return scraper.get_available_expirations(symbol)


async def get_options_chain(
    db: AsyncSession,
    symbol: str,
    force_refresh: bool = False
) -> Optional[OptionsChain]:
    """
    Get full options chain data for a symbol.
    
    Uses database cache with 1-hour TTL.
    Merges fresh data with cached data to preserve last-known values.
    
    Returns:
        OptionsChain with available expirations, strikes, and contract data
    """
    symbol = symbol.upper()
    cache_key = f"options_chain:{symbol}"
    
    # Check for valid (non-expired) cache first
    if not force_refresh:
        valid_cache = await get_cached_data(db, cache_key, include_expired=False)
        if valid_cache:
            logger.debug(
                "Returning valid cached options chain for %s (%d contracts)",
                symbol, len(valid_cache.get("contracts", []))
            )
            contracts = [
                OptionContract(**c) for c in valid_cache.get("contracts", [])
            ]
            return OptionsChain(
                symbol=valid_cache["symbol"],
                current_price=valid_cache.get("current_price"),
                expirations=valid_cache.get("expirations", []),
                contracts=contracts,
                iv_rank=valid_cache.get("iv_rank"),
                iv_percentile=valid_cache.get("iv_percentile"),
                implied_volatility=valid_cache.get("implied_volatility"),
            )
    
    # Get any cached data (including expired) for merging with fresh data
    cached = await get_cached_data(db, cache_key, include_expired=True)
    
    logger.info("Fetching fresh options chain for %s (force_refresh=%s, has_expired_cache=%s)", symbol, force_refresh, cached is not None)
    try:
        fresh_dict = await run_scraper(_fetch_options_chain_sync, symbol)
        
        # Log what we got
        logger.debug(
            "Fresh chain for %s: price=%s, IV=%s, contracts=%d",
            symbol,
            fresh_dict.get("current_price"),
            fresh_dict.get("implied_volatility"),
            len(fresh_dict.get("contracts", []))
        )
        
        # Merge fresh data with cached data - preserve last-known values for null fields
        if cached:
            merged_dict = dict(cached)  # Start with cached values
            for key, value in fresh_dict.items():
                if key == 'contracts' and value:
                    merged_dict[key] = value  # Always update contracts if present
                elif key == 'expirations' and value:
                    merged_dict[key] = value  # Always update expirations if present
                elif value is not None and key != 'raw_content':
                    merged_dict[key] = value  # Only overwrite if fresh value is not null
                elif value is None and cached.get(key) is not None:
                    logger.debug("Chain: preserving cached %s=%s (fresh was null)", key, cached.get(key))
            if fresh_dict.get('raw_content'):
                merged_dict['raw_content'] = fresh_dict['raw_content']
            final_dict = merged_dict
        else:
            final_dict = fresh_dict
        
        await set_cached_data(db, cache_key, "options_chain", symbol, final_dict)
        
        contracts = [
            OptionContract(**c) for c in final_dict.get("contracts", [])
        ]
        return OptionsChain(
            symbol=final_dict["symbol"],
            current_price=final_dict.get("current_price"),
            expirations=final_dict.get("expirations", []),
            contracts=contracts,
            iv_rank=final_dict.get("iv_rank"),
            iv_percentile=final_dict.get("iv_percentile"),
            implied_volatility=final_dict.get("implied_volatility"),
        )
    except Exception as e:
        logger.error("Error fetching options chain for %s: %s", symbol, e)
        # On error, return cached data if available
        if cached:
            logger.info("Falling back to cached chain for %s", symbol)
            contracts = [
                OptionContract(**c) for c in cached.get("contracts", [])
            ]
            return OptionsChain(
                symbol=cached["symbol"],
                current_price=cached.get("current_price"),
                expirations=cached.get("expirations", []),
                contracts=contracts,
                iv_rank=cached.get("iv_rank"),
                iv_percentile=cached.get("iv_percentile"),
                implied_volatility=cached.get("implied_volatility"),
            )
        return None


async def get_available_expirations(
    db: AsyncSession,
    symbol: str,
    force_refresh: bool = False
) -> list[str]:
    """
    Get list of available expiration dates for a symbol.
    
    Returns list of expiration date strings.
    """
    symbol = symbol.upper()
    cache_key = f"expirations:{symbol}"
    
    if not force_refresh:
        cached = await get_cached_data(db, cache_key)
        if cached and isinstance(cached, list):
            logger.debug("Returning cached expirations for %s (%d dates)", symbol, len(cached))
            return cached
        if cached and isinstance(cached, dict) and "expirations" in cached:
            logger.debug("Returning cached expirations for %s (%d dates)", symbol, len(cached["expirations"]))
            return cached["expirations"]
    
    logger.info("Fetching fresh expirations for %s", symbol)
    try:
        expirations = await run_scraper(_fetch_expirations_sync, symbol)
        await set_cached_data(db, cache_key, "expirations", symbol, {"expirations": expirations})
        logger.debug("Got %d expirations for %s", len(expirations), symbol)
        return expirations
    except Exception as e:
        logger.error("Error fetching expirations for %s: %s", symbol, e)
        return []


async def get_symbol_speculation_data(
    db: AsyncSession,
    symbol: str,
    force_refresh: bool = False
) -> dict:
    """
    Get all data needed for options speculation on a symbol.
    
    Uses fallbacks to ensure data is available even when markets are closed:
    - Price: Yahoo Finance -> StockNear chain -> StockNear stock overview
    - IV data: StockNear options overview (cached)
    
    Returns dict with:
    - current_price: float
    - implied_volatility: float (as decimal)
    - iv_rank: float (0-100)
    - iv_percentile: float (0-100)
    - expirations: list[str]
    - max_pain: float
    - put_call_ratio: float
    """
    symbol = symbol.upper()
    logger.info("Getting speculation data for %s (force_refresh=%s)", symbol, force_refresh)
    
    # Get enriched quote (price + options data)
    from app.services.price_service import get_stock_price
    
    # Try Yahoo Finance first for live price
    price_quote = await get_stock_price(symbol)
    current_price = price_quote.price if price_quote else None
    price_change = price_quote.change if price_quote else None
    price_change_percent = price_quote.change_percent if price_quote else None
    logger.debug("Yahoo price for %s: %s", symbol, current_price)
    
    # Get options chain data (cached) - this has last-known price and option data
    chain = await get_options_chain(db, symbol, force_refresh)
    
    # No silent fallback to scraped chain price — if Yahoo returns null the
    # caller must surface the failure to the user. (Using regex on the options
    # page text routinely produced bogus prices, e.g. picking up market-cap
    # dollar amounts as the stock price.)
    if current_price is None:
        logger.warning("No live price for %s — Yahoo returned null", symbol)
    
    # Get options overview for IV data
    options_data = await get_options_overview(db, symbol, force_refresh)
    
    # Fallback to chain's IV data if options overview is missing
    implied_volatility = None
    iv_rank = None
    iv_percentile = None
    
    if options_data:
        implied_volatility = options_data.implied_volatility
        iv_rank = options_data.iv_rank
        iv_percentile = options_data.iv_percentile
    
    if implied_volatility is None and chain:
        logger.debug("Using chain IV fallback for %s", symbol)
        implied_volatility = chain.implied_volatility
    if iv_rank is None and chain:
        iv_rank = chain.iv_rank
    if iv_percentile is None and chain:
        iv_percentile = chain.iv_percentile
    
    logger.debug(
        "Speculation data for %s: price=%s, IV=%s, IV_rank=%s",
        symbol, current_price, implied_volatility, iv_rank
    )
    
    # Get max pain
    max_pain = await get_max_pain(db, symbol, force_refresh)
    
    # Get available expirations - prefer chain data
    expirations = []
    if chain and chain.expirations:
        expirations = chain.expirations
    else:
        expirations = await get_available_expirations(db, symbol, force_refresh)
    
    return {
        "symbol": symbol,
        "current_price": current_price,
        "price_change": price_change,
        "price_change_percent": price_change_percent,
        "implied_volatility": implied_volatility,
        "iv_rank": iv_rank,
        "iv_percentile": iv_percentile,
        "put_call_ratio": options_data.put_call_ratio if options_data else None,
        "total_open_interest": options_data.total_open_interest if options_data else None,
        "max_pain": max_pain,
        "expirations": expirations,
    }


async def get_available_strikes(
    db: AsyncSession,
    symbol: str,
    force_refresh: bool = False
) -> dict:
    """
    Get available strike prices for a symbol.
    
    Uses StockNear's contract-lookup page to extract valid strikes.
    Results are cached for 1 hour.
    
    Returns:
        dict with:
        - strikes: list[float] - All available strikes sorted
        - expirations: list[str] - Available expiration dates
    """
    symbol = symbol.upper()
    cache_key = f"strikes:{symbol}"

    # Check cache first (uses the same StockNearCache schema as everything else)
    if not force_refresh:
        stmt = select(StockNearCache).where(StockNearCache.cache_key == cache_key)
        cached_row = (await db.execute(stmt)).scalar_one_or_none()

        if cached_row and cached_row.expires_at > datetime.utcnow():
            logger.debug("Using cached strikes for %s", symbol)
            try:
                return json.loads(cached_row.data_json)
            except Exception as e:
                logger.warning("Cached strikes for %s unreadable, refetching: %s", symbol, e)

    # Fetch fresh data using the persistent scraper
    def fetch_sync():
        scraper = _get_or_start_persistent_scraper()
        return scraper.get_available_strikes(symbol)

    data = await run_scraper(fetch_sync)

    result = {
        "strikes": data.get("strikes", []),
        "expirations": data.get("expirations", []),
    }

    # Cache for 1 hour using the canonical StockNearCache fields
    try:
        await db.execute(
            delete(StockNearCache).where(StockNearCache.cache_key == cache_key)
        )
        now = datetime.utcnow()
        cache_row = StockNearCache(
            cache_key=cache_key,
            data_type="strikes",
            symbol=symbol,
            data_json=json.dumps(result),
            fetched_at=now,
            expires_at=now + timedelta(hours=1),
        )
        db.add(cache_row)
        await db.commit()
        logger.debug("Cached strikes for %s (count=%d)", symbol, len(result["strikes"]))
    except Exception as e:
        logger.warning("Failed to cache strikes for %s: %s", symbol, e)

    return result


async def get_contract_premium(
    db: AsyncSession,
    symbol: str,
    expiration: str,
    strike: float,
    option_type: str,
    force_refresh: bool = False
) -> Optional[float]:
    """
    Get the premium (mid-price or last) for a specific contract.
    
    Returns the mid-price if bid/ask available, otherwise the last traded price.
    Returns None if contract not found.
    """
    chain = await get_options_chain(db, symbol, force_refresh)
    if not chain:
        logger.debug("No chain available for %s when looking up contract", symbol)
        return None
    
    contract = chain.get_contract(expiration, strike, option_type.upper())
    if not contract:
        logger.debug(
            "Contract not found: %s %s %s %s",
            symbol, expiration, strike, option_type
        )
        return None
    
    # Explicit fallback chain: mid (best) → last (potentially stale).
    # mid_price now returns None when there is no real bid/ask spread, so
    # callers see the staleness rather than getting a silently last-trade.
    premium = contract.mid_price
    if premium is None and contract.last is not None and contract.last > 0:
        logger.warning(
            "No bid/ask for %s %s $%s %s — using last-trade price (%s) which may be stale",
            symbol, expiration, strike, option_type, contract.last
        )
        premium = contract.last
    logger.debug(
        "Found contract %s %s %s %s: premium=%s",
        symbol, expiration, strike, option_type, premium
    )
    return premium


def _fetch_contract_quote_sync(symbol: str, expiration: str, strike: float, option_type: str) -> dict:
    """Synchronous fetch of contract quote using direct API - runs in thread pool."""
    scraper = _get_or_start_persistent_scraper()
    quote = scraper.get_contract_quote_via_api(symbol, expiration, strike, option_type)
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


async def get_contract_quote(
    db: AsyncSession,
    symbol: str,
    expiration: str,
    strike: float,
    option_type: str,
    force_refresh: bool = False
) -> Optional[ContractQuote]:
    """
    Get real-time quote for a specific option contract.
    
    Fetches directly from StockNear's contract lookup page to get real bid/ask/mid.
    Uses a short cache TTL (5 minutes) since this is real-time data.
    
    Args:
        db: Database session
        symbol: Stock ticker
        expiration: Expiration date string
        strike: Strike price
        option_type: "CALL" or "PUT"
        force_refresh: If True, bypass cache
    
    Returns:
        ContractQuote with real bid/ask/mid/last prices and Greeks
    """
    symbol = symbol.upper()
    option_type = option_type.upper()
    
    # Create a cache key specific to this contract
    cache_key = f"contract_quote:{symbol}:{expiration}:{strike}:{option_type}"
    
    # Check cache first (short TTL for real-time data)
    if not force_refresh:
        cached = await get_cached_data(db, cache_key, include_expired=False)
        if cached:
            logger.debug("Returning cached contract quote for %s", cache_key)
            return ContractQuote(**cached)
    
    logger.info(
        "Fetching fresh contract quote for %s %s %s %s",
        symbol, expiration, strike, option_type
    )
    
    try:
        quote_dict = await run_scraper(
            _fetch_contract_quote_sync, symbol, expiration, strike, option_type
        )
        
        # Cache with short TTL (5 minutes for real-time data)
        await set_cached_data(
            db, cache_key, "contract_quote", symbol, quote_dict, ttl_seconds=300
        )
        
        return ContractQuote(**quote_dict)
    except Exception as e:
        logger.error(
            "Error fetching contract quote for %s %s %s %s: %s",
            symbol, expiration, strike, option_type, e
        )
        return None


def _fetch_contract_quotes_batch_sync(contracts: list[dict]) -> list[dict]:
    """Synchronous batch fetch of contract quotes using direct API - runs in thread pool."""
    scraper = _get_or_start_persistent_scraper()
    quotes = scraper.get_contract_quotes_via_api(contracts)
    return [
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
            "open_price": q.open_price,
            "volume": q.volume,
            "open_interest": q.open_interest,
            "implied_volatility": q.implied_volatility,
            "delta": q.delta,
            "gamma": q.gamma,
            "theta": q.theta,
            "vega": q.vega,
        }
        for q in quotes
    ]


async def get_contract_quotes_batch(
    db: AsyncSession,
    contracts: list[dict],
    force_refresh: bool = False
) -> list[ContractQuote]:
    """
    Batch fetch quotes for multiple contracts using a SINGLE browser session.
    
    Much more efficient than calling get_contract_quote() multiple times.
    
    Args:
        db: Database session
        contracts: List of dicts with keys: symbol, expiration, strike, option_type
        force_refresh: If True, bypass cache for all contracts
    
    Returns:
        List of ContractQuote objects (in same order as input)
    """
    if not contracts:
        return []
    
    results = []
    contracts_to_fetch = []
    cache_indices = {}  # Maps fetch index to result index
    
    # Check cache for each contract
    for i, contract in enumerate(contracts):
        symbol = contract["symbol"].upper()
        expiration = contract["expiration"]
        strike = contract["strike"]
        option_type = contract["option_type"].upper()
        
        cache_key = f"contract_quote:{symbol}:{expiration}:{strike}:{option_type}"
        
        if not force_refresh:
            cached = await get_cached_data(db, cache_key, include_expired=False)
            if cached:
                logger.debug("Cache hit for contract %d: %s", i, cache_key)
                results.append((i, ContractQuote(**cached)))
                continue
        
        # Need to fetch this one
        cache_indices[len(contracts_to_fetch)] = i
        contracts_to_fetch.append(contract)
        results.append((i, None))  # Placeholder
    
    # Fetch all missing contracts in one browser session
    if contracts_to_fetch:
        logger.info("Batch fetching %d contracts (of %d total)", 
                   len(contracts_to_fetch), len(contracts))
        
        try:
            fetched_dicts = await run_scraper(
                _fetch_contract_quotes_batch_sync, contracts_to_fetch
            )
            
            # Cache and update results
            for fetch_idx, quote_dict in enumerate(fetched_dicts):
                result_idx = cache_indices[fetch_idx]
                contract = contracts_to_fetch[fetch_idx]
                
                symbol = contract["symbol"].upper()
                expiration = contract["expiration"]
                strike = contract["strike"]
                option_type = contract["option_type"].upper()
                cache_key = f"contract_quote:{symbol}:{expiration}:{strike}:{option_type}"
                
                # Cache with 5 minute TTL
                await set_cached_data(
                    db, cache_key, "contract_quote", symbol, quote_dict, ttl_seconds=300
                )
                
                # Update result
                for j, (idx, _) in enumerate(results):
                    if idx == result_idx:
                        results[j] = (result_idx, ContractQuote(**quote_dict))
                        break
                        
        except Exception as e:
            logger.error("Error in batch fetch: %s", e)
    
    # Sort by original index and return quotes only
    results.sort(key=lambda x: x[0])
    return [q for _, q in results if q is not None]
