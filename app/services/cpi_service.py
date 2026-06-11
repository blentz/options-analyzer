"""US CPI (annual) for Reality Gap inflation adjustment.

Source: Bureau of Labor Statistics public timeseries API. We fetch the annual
average (BLS period code ``M13``) of CPI-U series ``CUUR0000SA0`` and cache it
in the existing StockNear cache table under the global key ``cpi:annual`` — CPI
for a closed year never changes, so a long TTL is safe.

Inflation adjustment is best-effort: if BLS is unreachable, the caller falls
back to nominal earnings (the Reality Gap math accepts ``cpi_by_year=None``).
"""

from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings

logger = logging.getLogger(__name__)

_BLS_V1 = "https://api.bls.gov/publicAPI/v1/timeseries/data/"
_BLS_V2 = "https://api.bls.gov/publicAPI/v2/timeseries/data/"
# BLS caps a single query at ~20 years; keep chunks small to also satisfy the
# stricter 10-year window of the unregistered (keyless) tier.
_MAX_SPAN = 10


def _fetch_cpi_range_sync(start_year: int, end_year: int) -> dict[int, float]:
    """Fetch annual-average CPI for [start_year, end_year] from BLS.

    Returns {year: cpi_index}. Empty dict on any failure — inflation adjustment
    is optional, so we degrade to nominal rather than raise.
    """
    import requests

    series = settings.bls_series_id
    api_key = settings.bls_api_key
    url = _BLS_V2 if api_key else _BLS_V1
    payload: dict = {
        "seriesid": [series],
        "startyear": str(start_year),
        "endyear": str(end_year),
        "annualaverage": True,
    }
    if api_key:
        payload["registrationkey"] = api_key

    try:
        resp = requests.post(url, json=payload, timeout=20)
        resp.raise_for_status()
        body = resp.json()
    except Exception as exc:
        logger.warning("BLS CPI fetch %d-%d failed: %s", start_year, end_year, exc)
        return {}

    if body.get("status") != "REQUEST_SUCCEEDED":
        logger.warning("BLS CPI request not successful: %s", body.get("message"))
        return {}

    out: dict[int, float] = {}
    for s in body.get("Results", {}).get("series", []):
        for item in s.get("data", []):
            # Prefer the annual-average row (period M13); fall back to averaging
            # monthly values when M13 isn't present for a year.
            period = item.get("period")
            try:
                year = int(item["year"])
                value = float(item["value"])
            except (KeyError, ValueError, TypeError):
                continue
            if period == "M13":
                out[year] = value
            elif period and period.startswith("M") and year not in out:
                out.setdefault(("_monthly", year), []).append(value)  # type: ignore[arg-type]

    # Resolve monthly fallbacks for years lacking an M13 row.
    monthly: dict[int, list] = {}
    for key, val in list(out.items()):
        if isinstance(key, tuple):
            monthly.setdefault(key[1], val)  # type: ignore[index]
            del out[key]
    for year, values in monthly.items():
        if year not in out and values:
            out[year] = sum(values) / len(values)
    return out


def _fetch_cpi_years_sync(min_year: int, max_year: int) -> dict[int, float]:
    """Fetch CPI across an arbitrary span, chunked to satisfy BLS per-query
    year limits."""
    result: dict[int, float] = {}
    start = min_year
    while start <= max_year:
        end = min(start + _MAX_SPAN - 1, max_year)
        result.update(_fetch_cpi_range_sync(start, end))
        start = end + 1
    return result


async def get_cpi_by_year(
    db: AsyncSession,
    years: set[int],
    force_refresh: bool = False,
) -> dict[int, float]:
    """Return {year: cpi_index} covering at least `years`, cached globally.

    Refetches when the cache is missing or doesn't cover every requested year
    (e.g. a newly-needed older year). Returns {} if BLS is unavailable, which
    the caller treats as "use nominal earnings".
    """
    import asyncio

    from app.services.stocknear_service import get_cached_data, set_cached_data

    if not years:
        return {}

    cache_key = "cpi:annual"
    cached: dict[int, float] = {}
    if not force_refresh:
        raw = await get_cached_data(db, cache_key, include_expired=True)
        if raw:
            cached = {int(y): float(v) for y, v in raw.items()}
            if years.issubset(cached.keys()):
                return cached

    # Need a (re)fetch. Widen the span to the union of requested + cached years
    # so we keep prior coverage.
    span_years = years | set(cached.keys())
    fresh = await asyncio.to_thread(_fetch_cpi_years_sync, min(span_years), max(span_years))
    merged = {**cached, **fresh}
    if merged:
        await set_cached_data(
            db, cache_key, "cpi", "_CPI", merged,
            ttl_seconds=settings.cpi_cache_ttl_seconds,
        )
    return merged
