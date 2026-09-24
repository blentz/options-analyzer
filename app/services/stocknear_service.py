"""
Async service for fetching and caching StockNear data.

Two sources, both plain HTTP — no browser:
- the MCP client (stocknear_mcp) for symbol-level data: options overview,
  max pain, expirations, the strike list and the OI-only options chain;
- the contract API client (stocknear_contract_api) for per-contract
  bid/ask/IV/greeks, which the MCP server does not expose.

This module adds database-backed caching with configurable TTLs and the
merge-with-stale logic that rides out transient nulls.
"""

import json
import logging
from dataclasses import asdict
from datetime import datetime, timedelta
from typing import Optional, cast

logger = logging.getLogger(__name__)


# Fields that must NOT inherit a cached value when the fresh fetch returns
# null. The merge below exists to ride out transient nulls (markets closed),
# but StockNear's MCP server returns ivRank as null most of the time, so
# preserving it would pin a pre-migration scraped value in place forever with
# its TTL reset on every write. A blank IV Rank is honest; a frozen one is not.
NEVER_PRESERVE_ON_NULL = frozenset({"iv_rank"})

from sqlalchemy import select, delete
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import StockNearCache
from app.stocknear_models import OptionsData, OptionsChain, OptionContract, ContractQuote
from app.services.stocknear_contract_api import fetch_contract_quote, fetch_contract_quotes
from app.services.stocknear_mcp import fetch_options_chain, fetch_options_overview, fetch_strikes


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
    # A DELETE yields a CursorResult; execute() is typed as the general
    # Result, which has no rowcount.
    result = cast(CursorResult, await db.execute(
        delete(StockNearCache).where(StockNearCache.expires_at < datetime.utcnow())
    ))
    await db.commit()
    return result.rowcount


async def get_options_overview(
    db: AsyncSession,
    symbol: str,
    force_refresh: bool = False
) -> Optional[OptionsData]:
    """
    Get options overview data for a symbol.
    
    Uses database cache with 1-hour TTL.
    Falls back to a live MCP fetch if cache miss.

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
    
    # Fetch fresh data via the MCP client
    logger.info("Fetching fresh options overview for %s (force_refresh=%s, has_expired_cache=%s)", symbol, force_refresh, cached is not None)
    try:
        fresh_dict = asdict(await fetch_options_overview(symbol))

        # Log what we got from the MCP fetch
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
                elif value is None and key in NEVER_PRESERVE_ON_NULL:
                    merged_dict[key] = None  # Clear rather than freeze a stale scraped value
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




def _chain_from_dict(d: dict) -> OptionsChain:
    """Build an OptionsChain from a cached/serialised dict.

    Single source of truth so we don't need to remember to update three
    near-identical constructor calls whenever a field is added to the
    dataclass — which is exactly how put_call_ratio, max_pain, total_volume
    and total_open_interest got silently dropped on the cache round-trip
    before this helper existed.
    """
    contracts = [OptionContract(**c) for c in d.get("contracts", [])]
    return OptionsChain(
        symbol=d["symbol"],
        current_price=d.get("current_price"),
        expirations=d.get("expirations", []),
        contracts=contracts,
        iv_rank=d.get("iv_rank"),
        iv_percentile=d.get("iv_percentile"),
        implied_volatility=d.get("implied_volatility"),
        put_call_ratio=d.get("put_call_ratio"),
        total_volume=d.get("total_volume"),
        total_open_interest=d.get("total_open_interest"),
        max_pain=d.get("max_pain"),
    )


def _chain_to_dict(chain: OptionsChain) -> dict:
    d = asdict(chain)
    d["raw_content"] = (chain.raw_content or "")[:1000]
    return d


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
            return _chain_from_dict(valid_cache)
    
    # Get any cached data (including expired) for merging with fresh data
    cached = await get_cached_data(db, cache_key, include_expired=True)
    
    logger.info("Fetching fresh options chain for %s (force_refresh=%s, has_expired_cache=%s)", symbol, force_refresh, cached is not None)
    try:
        fresh_dict = _chain_to_dict(await fetch_options_chain(symbol))
        
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
        return _chain_from_dict(final_dict)
    except Exception as e:
        logger.error("Error fetching options chain for %s: %s", symbol, e)
        if cached:
            logger.info("Falling back to cached chain for %s", symbol)
            return _chain_from_dict(cached)
        return None




async def get_symbol_speculation_data(
    db: AsyncSession,
    symbol: str,
    force_refresh: bool = False,
) -> dict:
    """Get all data needed for options speculation on a symbol.

    Sources:
      - Live underlying price: Yahoo Finance (cheap, fast — ~200ms)
      - Everything else: the MCP-backed options chain (two MCP calls,
        cached 1 hour) — IV/rank/percentile, put-call ratio, total OI,
        expirations, and nearest-expiry max-pain.

    Returns dict with:
      current_price, price_change, price_change_percent (Yahoo),
      implied_volatility, iv_rank, iv_percentile (chain),
      put_call_ratio, total_open_interest, max_pain, expirations (chain).
    """
    symbol = symbol.upper()
    logger.info("Getting speculation data for %s (force_refresh=%s)", symbol, force_refresh)

    from app.services.price_service import get_stock_price

    price_quote = await get_stock_price(symbol)
    current_price = price_quote.price if price_quote else None
    price_change = price_quote.change if price_quote else None
    price_change_percent = price_quote.change_percent if price_quote else None
    logger.debug("Yahoo price for %s: %s", symbol, current_price)

    # The chain carries every symbol-level field. See OptionsChain.
    chain = await get_options_chain(db, symbol, force_refresh)

    # Yahoo is authoritative for price; chain has no reliable price field.
    # If Yahoo failed, log it — callers HTTP 503 from there.
    if current_price is None:
        logger.warning("No live price for %s — Yahoo returned null", symbol)

    if chain is None:
        # MCP failed with no cache to fall back on — return price only.
        return {
            "symbol": symbol,
            "current_price": current_price,
            "price_change": price_change,
            "price_change_percent": price_change_percent,
            "implied_volatility": None,
            "iv_rank": None,
            "iv_percentile": None,
            "put_call_ratio": None,
            "total_open_interest": None,
            "max_pain": None,
            "expirations": [],
        }

    return {
        "symbol": symbol,
        "current_price": current_price,
        "price_change": price_change,
        "price_change_percent": price_change_percent,
        "implied_volatility": chain.implied_volatility,
        "iv_rank": chain.iv_rank,
        "iv_percentile": chain.iv_percentile,
        "put_call_ratio": chain.put_call_ratio,
        "total_open_interest": chain.total_open_interest,
        "max_pain": chain.max_pain,
        "expirations": chain.expirations,
    }


async def get_available_strikes(
    db: AsyncSession,
    symbol: str,
    force_refresh: bool = False
) -> dict:
    """
    Get available strike prices for a symbol.
    
    Strikes carrying open interest, from the MCP server. Cached for 1 hour.
    
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

    data = await fetch_strikes(symbol)

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
    
    Fetches from StockNear's contract JSON API (no browser) to get real
    bid/ask/mid. Uses a short cache TTL (5 minutes) since this is real-time data.
    
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
        quote = await fetch_contract_quote(symbol, expiration, strike, option_type)
        quote_dict = asdict(quote)
        quote_dict.pop("raw_content", None)
        
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


async def get_contract_quotes_batch(
    db: AsyncSession,
    contracts: list[dict],
    force_refresh: bool = False
) -> list[Optional[ContractQuote]]:
    """
    Batch fetch quotes for multiple contracts, concurrently, with caching.
    
    Args:
        db: Database session
        contracts: List of dicts with keys: symbol, expiration, strike, option_type
        force_refresh: If True, bypass cache for all contracts
    
    Returns:
        One entry per input contract, in order; None where the fetch failed.
    """
    if not contracts:
        return []
    
    results: list[tuple[int, Optional[ContractQuote]]] = []
    contracts_to_fetch: list[dict] = []
    cache_indices: dict[int, int] = {}  # Maps fetch index to result index
    
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
    
    # Fetch all missing contracts
    if contracts_to_fetch:
        logger.info("Batch fetching %d contracts (of %d total)", 
                   len(contracts_to_fetch), len(contracts))
        
        try:
            fetched = await fetch_contract_quotes(contracts_to_fetch)

            # Cache and update results. A failed contract stays None and is
            # neither cached nor returned.
            for fetch_idx, quote in enumerate(fetched):
                if quote is None:
                    continue
                quote_dict = asdict(quote)
                quote_dict.pop("raw_content", None)
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
    
    # One slot per input, in input order; a failed contract stays None so
    # callers can match quotes to legs by index.
    results.sort(key=lambda x: x[0])
    return [q for _, q in results]
