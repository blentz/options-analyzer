"""Service for fetching current stock prices from public APIs."""

import asyncio
import httpx
import pandas as pd
from typing import Optional
from datetime import datetime, timedelta
from dataclasses import dataclass


@dataclass
class StockQuote:
    """Current stock quote data."""
    symbol: str
    price: float
    change: float
    change_percent: float
    timestamp: datetime


@dataclass
class OptionQuote:
    """Option contract quote data."""
    contract_symbol: str
    underlying: str
    strike: float
    expiration: str  # YYYY-MM-DD format
    option_type: str  # "call" or "put"
    bid: Optional[float] = None
    ask: Optional[float] = None
    last: Optional[float] = None
    volume: Optional[int] = None
    open_interest: Optional[int] = None
    implied_volatility: Optional[float] = None
    in_the_money: bool = False
    timestamp: Optional[datetime] = None


# Simple in-memory cache to avoid excessive API calls
_price_cache: dict[str, tuple[StockQuote, datetime]] = {}
_option_cache: dict[str, tuple[list[OptionQuote], datetime]] = {}
CACHE_TTL_SECONDS = 60  # Cache prices for 1 minute
OPTION_CACHE_TTL_SECONDS = 300  # Cache options for 5 minutes


async def get_stock_price(symbol: str) -> Optional[StockQuote]:
    """
    Fetch current stock price from Yahoo Finance.
    Uses caching to avoid excessive API calls.
    """
    # Check cache first
    if symbol in _price_cache:
        quote, cached_at = _price_cache[symbol]
        if datetime.now() - cached_at < timedelta(seconds=CACHE_TTL_SECONDS):
            return quote

    # Fetch from Yahoo Finance
    quote = await _fetch_yahoo_quote(symbol)

    if quote:
        _price_cache[symbol] = (quote, datetime.now())

    return quote


async def _fetch_yahoo_quote(symbol: str, max_attempts: int = 3) -> Optional[StockQuote]:
    """Fetch quote from Yahoo Finance API, retrying on transient failure.

    Retries on 429 (rate limited), 503 (Yahoo blip), and network errors with
    exponential backoff (0.5s, 1s, 2s). 4xx other than 429 are not retried
    because they indicate a permanently bad request (e.g., unknown ticker).
    """
    import logging
    logger = logging.getLogger(__name__)

    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    params = {"interval": "1d", "range": "1d"}
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    last_err: Optional[str] = None
    for attempt in range(max_attempts):
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(url, params=params, headers=headers)

                if response.status_code == 200:
                    data = response.json()
                    result = data.get("chart", {}).get("result", [])
                    if not result:
                        return None
                    meta = result[0].get("meta", {})
                    price = meta.get("regularMarketPrice")
                    prev_close = meta.get("previousClose", price)
                    if price is None:
                        return None
                    change = price - prev_close if prev_close else 0
                    change_percent = (change / prev_close * 100) if prev_close else 0
                    return StockQuote(
                        symbol=symbol.upper(),
                        price=float(price),
                        change=float(change),
                        change_percent=float(change_percent),
                        timestamp=datetime.now(),
                    )

                # Transient: retry
                if response.status_code in (429, 503, 504):
                    last_err = f"HTTP {response.status_code}"
                    if attempt + 1 < max_attempts:
                        await asyncio.sleep(0.5 * (2 ** attempt))
                        continue
                # Permanent failure
                logger.warning("Yahoo returned %s for %s — giving up", response.status_code, symbol)
                return None

        except (httpx.TimeoutException, httpx.NetworkError, httpx.ConnectError) as e:
            last_err = str(e)
            if attempt + 1 < max_attempts:
                await asyncio.sleep(0.5 * (2 ** attempt))
                continue
        except Exception as e:
            logger.exception("Unexpected error fetching Yahoo quote for %s: %s", symbol, e)
            return None

    logger.warning("All %d Yahoo attempts failed for %s (last error: %s)", max_attempts, symbol, last_err)
    return None


async def get_multiple_prices(symbols: list[str]) -> dict[str, Optional[StockQuote]]:
    """Fetch prices for multiple symbols concurrently."""
    tasks = [get_stock_price(symbol) for symbol in symbols]
    results = await asyncio.gather(*tasks)
    return dict(zip(symbols, results))


def clear_cache():
    """Clear the price cache."""
    _price_cache.clear()


async def get_option_chain(symbol: str, expiration_date: str = None) -> list[OptionQuote]:
    """
    Fetch options chain from Yahoo Finance for a given symbol.
    
    Args:
        symbol: Stock ticker (e.g., "AAPL")
        expiration_date: Optional expiration in epoch timestamp or YYYY-MM-DD format
    
    Returns:
        List of OptionQuote objects for both calls and puts
    """
    cache_key = f"{symbol}:{expiration_date or 'all'}"
    
    # Check cache first
    if cache_key in _option_cache:
        quotes, cached_at = _option_cache[cache_key]
        if datetime.now() - cached_at < timedelta(seconds=OPTION_CACHE_TTL_SECONDS):
            return quotes
    
    # Fetch from Yahoo Finance
    quotes = await _fetch_yahoo_options(symbol, expiration_date)
    
    if quotes:
        _option_cache[cache_key] = (quotes, datetime.now())
    
    return quotes


async def _fetch_yahoo_options(symbol: str, expiration_date: str = None) -> list[OptionQuote]:
    """Fetch options chain from Yahoo Finance using yfinance library."""
    import yfinance as yf
    
    try:
        ticker = yf.Ticker(symbol)
        
        # Get available expirations if none specified
        expirations = ticker.options
        if not expirations:
            print(f"No options expirations available for {symbol}")
            return []
        
        # Find matching expiration or use first available
        target_exp = None
        if expiration_date:
            for exp in expirations:
                if exp == expiration_date:
                    target_exp = exp
                    break
        
        if not target_exp:
            # Use closest expiration if exact match not found
            target_exp = expirations[0]
            if expiration_date:
                print(f"Expiration {expiration_date} not found, using {target_exp}")
        
        # Get option chain for this expiration
        opt_chain = ticker.option_chain(target_exp)
        
        quotes = []
        
        # Parse calls
        for _, row in opt_chain.calls.iterrows():
            quote = _parse_yfinance_option(row, symbol, target_exp, "call")
            if quote:
                quotes.append(quote)
        
        # Parse puts
        for _, row in opt_chain.puts.iterrows():
            quote = _parse_yfinance_option(row, symbol, target_exp, "put")
            if quote:
                quotes.append(quote)
        
        return quotes
    
    except Exception as e:
        print(f"Error fetching options for {symbol}: {e}")
        import traceback
        traceback.print_exc()
        return []


def _parse_yfinance_option(row, underlying: str, expiration: str, option_type: str) -> Optional[OptionQuote]:
    """Parse a single option from yfinance DataFrame row."""
    try:
        return OptionQuote(
            contract_symbol=row.get("contractSymbol", ""),
            underlying=underlying.upper(),
            strike=float(row.get("strike", 0)),
            expiration=expiration,
            option_type=option_type,
            bid=row.get("bid") if not pd.isna(row.get("bid")) else None,
            ask=row.get("ask") if not pd.isna(row.get("ask")) else None,
            last=row.get("lastPrice") if not pd.isna(row.get("lastPrice")) else None,
            volume=int(row.get("volume")) if not pd.isna(row.get("volume")) else None,
            open_interest=int(row.get("openInterest")) if not pd.isna(row.get("openInterest")) else None,
            implied_volatility=row.get("impliedVolatility") if not pd.isna(row.get("impliedVolatility")) else None,
            in_the_money=bool(row.get("inTheMoney", False)),
            timestamp=datetime.now()
        )
    except Exception as e:
        print(f"Error parsing option: {e}")
        return None


async def get_option_quote(
    symbol: str,
    expiration: str,
    strike: float,
    option_type: str
) -> Optional[OptionQuote]:
    """
    Get quote for a specific option contract.
    
    Args:
        symbol: Stock ticker (e.g., "AAPL")
        expiration: Expiration date in various formats (YYYY-MM-DD, "Mar 20, 2026", etc.)
        strike: Strike price
        option_type: "CALL" or "PUT"
    
    Returns:
        OptionQuote if found, None otherwise
    """
    # Convert expiration to YYYY-MM-DD format
    exp_date = _normalize_expiration(expiration)
    if not exp_date:
        return None
    
    # Fetch the chain for this expiration
    chain = await get_option_chain(symbol, exp_date)
    
    # Find the matching contract
    option_type_lower = option_type.lower()
    for quote in chain:
        if (quote.option_type == option_type_lower and 
            abs(quote.strike - strike) < 0.01 and
            quote.expiration == exp_date):
            return quote
    
    return None


def _normalize_expiration(expiration: str) -> Optional[str]:
    """Convert various expiration formats to YYYY-MM-DD."""
    # Try multiple formats
    formats = [
        "%Y-%m-%d",      # 2026-03-20
        "%b %d, %Y",     # Mar 20, 2026
        "%B %d, %Y",     # March 20, 2026
        "%m/%d/%Y",      # 03/20/2026
    ]
    
    for fmt in formats:
        try:
            dt = datetime.strptime(expiration, fmt)
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            continue
    
    return None


async def get_option_quotes_batch(
    contracts: list[dict]
) -> list[Optional[OptionQuote]]:
    """
    Get quotes for multiple option contracts.
    
    Args:
        contracts: List of dicts with keys: symbol, expiration, strike, option_type
    
    Returns:
        List of OptionQuote objects (None for contracts not found)
    """
    # Group by symbol to minimize API calls
    by_symbol: dict[str, list[tuple[int, dict]]] = {}
    for i, contract in enumerate(contracts):
        symbol = contract["symbol"].upper()
        if symbol not in by_symbol:
            by_symbol[symbol] = []
        by_symbol[symbol].append((i, contract))
    
    results: list[Optional[OptionQuote]] = [None] * len(contracts)
    
    # Fetch each symbol's chain and match contracts
    for symbol, indexed_contracts in by_symbol.items():
        # Get unique expirations for this symbol
        expirations = set()
        for _, contract in indexed_contracts:
            exp = _normalize_expiration(contract["expiration"])
            if exp:
                expirations.add(exp)
        
        # Fetch chain for each expiration
        for exp_date in expirations:
            chain = await get_option_chain(symbol, exp_date)
            
            # Match contracts
            for idx, contract in indexed_contracts:
                exp = _normalize_expiration(contract["expiration"])
                if exp != exp_date:
                    continue
                
                strike = contract["strike"]
                opt_type = contract["option_type"].lower()
                
                for quote in chain:
                    if (quote.option_type == opt_type and
                        abs(quote.strike - strike) < 0.01 and
                        quote.expiration == exp_date):
                        results[idx] = quote
                        break
    
    return results
