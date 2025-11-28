"""Service for fetching current stock prices from public APIs."""

import asyncio
import httpx
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


# Simple in-memory cache to avoid excessive API calls
_price_cache: dict[str, tuple[StockQuote, datetime]] = {}
CACHE_TTL_SECONDS = 60  # Cache prices for 1 minute


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


async def _fetch_yahoo_quote(symbol: str) -> Optional[StockQuote]:
    """Fetch quote from Yahoo Finance API."""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    params = {
        "interval": "1d",
        "range": "1d"
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(url, params=params, headers=headers)

            if response.status_code != 200:
                return None

            data = response.json()

            # Parse Yahoo Finance response
            result = data.get("chart", {}).get("result", [])
            if not result:
                return None

            quote_data = result[0]
            meta = quote_data.get("meta", {})

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
                timestamp=datetime.now()
            )

    except Exception as e:
        print(f"Error fetching price for {symbol}: {e}")
        return None


async def get_multiple_prices(symbols: list[str]) -> dict[str, Optional[StockQuote]]:
    """Fetch prices for multiple symbols concurrently."""
    tasks = [get_stock_price(symbol) for symbol in symbols]
    results = await asyncio.gather(*tasks)
    return dict(zip(symbols, results))


def clear_cache():
    """Clear the price cache."""
    _price_cache.clear()
