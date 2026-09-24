"""StockNear contract quote client — plain HTTP, no browser.

The MCP server has no bid, ask, or greeks for an arbitrary strike, so
contract quotes and full contract history come from the JSON endpoint
StockNear's contract-lookup page itself calls
(`/api/options-contract-history`). The endpoint answered without cookies
when checked (2026-09-24); the logged-in session cookies from the
Firefox/LibreWolf profile (STOCKNEAR_BROWSER_PROFILE_PATH) are still sent
when available, in case StockNear starts gating it.
"""

import asyncio
import logging
import time
from datetime import datetime
from decimal import Decimal
from typing import Any, Optional

import httpx

from app.config import settings
from app.stocknear_cookies import extract_browser_cookies
from app.stocknear_models import ContractQuote

logger = logging.getLogger(__name__)

API_PATH = "/api/options-contract-history"

# Cloudflare checks the browser signature against cf_clearance, which was
# issued to Firefox. The previous requests-based client sent this UA and
# passed; keep it matched to the browser the profile belongs to.
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64; rv:132.0) Gecko/20100101 Firefox/132.0"

# Cookies change rarely, but reading them copies cookies.sqlite and its WAL
# (megabytes) — cache briefly instead of per request.
_COOKIE_TTL_SECONDS = 300
_cookie_cache: tuple[float, dict[str, str]] | None = None

_semaphore = asyncio.Semaphore(4)


class StockNearAPIError(Exception):
    """The contract API request failed (transport, HTTP status, or bad JSON)."""


def build_contract_id(symbol: str, expiration: str, option_type: str, strike: float) -> str:
    """OCC-style id StockNear uses: {SYMBOL}{YYMMDD}{P|C}{STRIKE*1000:08d}.

    Accepts "2026-10-16", "Oct 16, 2026", "October 16, 2026" or "10/16/2026".
    The strike goes through Decimal(str(...)): float multiplication truncates
    (2.55 * 1000 == 2549.9999999999995). Must agree with
    app.services.contract_history.occ_symbol — see that module's docstring.
    """
    for fmt in ("%Y-%m-%d", "%b %d, %Y", "%B %d, %Y", "%m/%d/%Y"):
        try:
            exp_date = datetime.strptime(expiration, fmt)
            break
        except ValueError:
            continue
    else:
        raise ValueError(f"Could not parse expiration date: {expiration}")

    type_char = "P" if option_type.upper() == "PUT" else "C"
    strike_int = int(Decimal(str(strike)) * 1000)
    return f"{symbol.upper()}{exp_date.strftime('%y%m%d')}{type_char}{strike_int:08d}"


def parse_quote(data: Any, symbol: str, expiration: str, strike: float, option_type: str) -> ContractQuote:
    """Build a ContractQuote from the API's history payload (latest row wins).

    The API answers either {"history": [...]} or a bare list; an empty list
    means the contract has no data (often: it does not exist).
    """
    quote = ContractQuote(
        symbol=symbol.upper(),
        strike=strike,
        option_type=option_type.upper(),
        expiration=expiration,
        contract_id=build_contract_id(symbol, expiration, option_type, strike),
    )
    rows = data.get("history") if isinstance(data, dict) else data
    if not isinstance(rows, list) or not rows:
        logger.warning("No contract history for %s — contract may not exist", quote.contract_id)
        return quote

    latest = rows[-1]
    quote.bid = latest.get("close_bid")
    quote.ask = latest.get("close_ask")
    quote.last = latest.get("close") or latest.get("mark")
    quote.open_price = latest.get("open")
    quote.volume = latest.get("volume")
    quote.open_interest = latest.get("open_interest")
    quote.implied_volatility = latest.get("implied_volatility")
    quote.delta = latest.get("delta")
    quote.gamma = latest.get("gamma")
    quote.theta = latest.get("theta")
    quote.vega = latest.get("vega")
    if quote.bid is not None and quote.ask is not None:
        quote.mid = (quote.bid + quote.ask) / 2
    elif quote.last is not None:
        quote.mid = quote.last
    return quote


def _load_cookies() -> dict[str, str]:
    global _cookie_cache
    now = time.monotonic()
    if _cookie_cache and now - _cookie_cache[0] < _COOKIE_TTL_SECONDS:
        return _cookie_cache[1]

    cookies: dict[str, str] = {}
    if settings.stocknear_browser_profile_path:
        for c in extract_browser_cookies(settings.stocknear_browser_profile_path, "stocknear.com"):
            cookies[c["name"]] = c["value"]
    if not cookies:
        logger.info("No StockNear cookies found — contract requests go unauthenticated")
    _cookie_cache = (now, cookies)
    return cookies


def _make_client() -> httpx.AsyncClient:
    """Seam for tests to install a MockTransport."""
    return httpx.AsyncClient(timeout=10)


async def _post_contract(symbol: str, contract_id: str) -> Any:
    """POST one contract to the history endpoint and return the decoded JSON.

    The same endpoint serves quotes (latest row) and full history.
    """
    payload = {"ticker": symbol.upper(), "contract": contract_id}
    headers = {
        "User-Agent": USER_AGENT,
        "Origin": settings.stocknear_base_url,
        "Referer": f"{settings.stocknear_base_url}/stocks/{symbol.lower()}/options/contract-lookup",
    }
    try:
        async with _semaphore, _make_client() as client:
            response = await client.post(
                settings.stocknear_base_url + API_PATH,
                json=payload, headers=headers, cookies=_load_cookies(),
            )
            response.raise_for_status()
            return response.json()
    except httpx.HTTPError as exc:
        raise StockNearAPIError(f"Contract request failed for {contract_id}: {exc}") from exc
    except ValueError as exc:
        raise StockNearAPIError(f"Contract response for {contract_id} was malformed JSON: {exc}") from exc


async def fetch_contract_quote(symbol: str, expiration: str, strike: float, option_type: str) -> ContractQuote:
    """Latest quote (bid/ask/mid/last, IV, greeks) for one contract.

    Raises StockNearAPIError on transport or HTTP failure so callers do not
    cache a blank quote as if it were real.
    """
    contract_id = build_contract_id(symbol, expiration, option_type, strike)
    data = await _post_contract(symbol, contract_id)
    return parse_quote(data, symbol, expiration, strike, option_type)


async def fetch_contract_history(symbol: str, occ_symbol: str) -> Any:
    """Raw full-history payload for one contract, oldest row first.

    Shape: {"expiration", "strike", "optionType", "history": [...]}, or []
    for a contract StockNear does not know. Parsing and the served-contract
    check live in app.services.contract_history.
    """
    return await _post_contract(symbol, occ_symbol)


async def fetch_contract_quotes(contracts: list[dict]) -> list[Optional[ContractQuote]]:
    """Quotes for many contracts, concurrently, in input order.

    Each dict has symbol, expiration, strike, option_type. A failed
    contract yields None rather than failing the batch.
    """
    async def one(c: dict) -> Optional[ContractQuote]:
        try:
            return await fetch_contract_quote(c["symbol"], c["expiration"], c["strike"], c["option_type"])
        except (StockNearAPIError, ValueError) as exc:
            logger.error("Contract quote failed for %s: %s", c, exc)
            return None

    return list(await asyncio.gather(*(one(c) for c in contracts)))
