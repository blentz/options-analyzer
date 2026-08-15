# StockNear MCP Ingest — Design

**Date:** 2026-08-15
**Status:** Approved, ready for implementation planning

## Problem

StockNear data reaches the application through a Playwright browser scraper
(`app/stocknear.py`, ~1400 lines). The scraper drives a persistent Firefox
context, navigates rendered pages, and parses text out of the DOM. It is slow
(10–30 seconds for a symbol lookup), fragile against markup changes, and
depends on browser session cookies extracted from a local Firefox/LibreWolf
profile.

StockNear now publishes an MCP server at `https://mcp.stocknear.com/mcp` that
serves the same market data as structured JSON. Where the MCP server covers a
data need, it should replace the scraper.

## Coverage Investigation

Live probes against the MCP server established what it does and does not
provide.

Covered, with a direct equivalent to an existing scraper method:

| Scraper method | MCP tool |
| --- | --- |
| `get_options_overview` | `get_ticker_options_overview_data` |
| `get_max_pain` | `get_ticker_options_overview_data` (per-expiry `maxPain`) |
| `get_stock_overview` | `get_ticker_quote` |
| `get_available_expirations` | `get_ticker_options_overview_data` (`table[].expiration`) |

Not covered. The MCP server exposes no per-contract quote for an arbitrary
strike. Its only contract-level tool, `get_ticker_hottest_options_contracts`,
returns the top ten contracts by volume with fields `iv`, `last`, `low`,
`high`, `volume`, `open_interest` — no bid, no ask, no greeks. The application
depends on bid/ask for exit-price calculation and for the spread-quality
classification in `app/stocknear_models.py` (`wide`, `no_bid`, `no_quote`).

The scraper therefore remains the source for contract quotes.

## Decisions

1. **Hybrid ingest.** MCP serves symbol-level data. Playwright continues to
   serve contract-level quotes, available strikes, and the parsed options
   chain.
2. **Direct MCP client, no Claude CLI.** The application speaks JSON-RPC to
   the MCP server over HTTP. An earlier proposal routed fetches through the
   `claude` CLI; it was rejected because it places a language model in the
   data path, adds subprocess and authentication dependencies, and costs
   latency for no gain over a direct call.
3. **No new user interface.** The existing refresh controls — "Refresh Quotes"
   on the speculation page and the `force_refresh` flows on the risk page —
   keep their current behavior and become MCP-backed.

## Transport

The MCP server is stateless. A `tools/call` request succeeds cold: no
`initialize` handshake, no `Mcp-Session-Id` header, no session to maintain.
Responses are plain JSON rather than server-sent events. The tool payload
arrives as a JSON string in `result.content[0].text`.

Consequently the client needs no MCP SDK. `httpx`, already a dependency,
is sufficient.

Two behaviors of the server govern the client's error handling:

- **A non-empty `User-Agent` header is required.** Requests carrying the
  default urllib or httpx user agent are rejected by Cloudflare with
  `403 error 1010` ("Access denied ... based on your browser's signature").
  Any non-empty value passes. This is easy to break accidentally and must
  carry an explanatory comment at the call site.
- **An unknown ticker returns `{}` with `isError: false`.** The absence of
  data is not reported as an error, so the client must detect it explicitly.

## Components

### `app/services/stocknear_mcp.py` (new)

Transport and typed fetchers, mirroring the existing separation between
fetching (`app/stocknear.py`) and cache orchestration
(`app/services/stocknear_service.py`).

Transport:

```
async def _rpc(method: str, params: dict) -> dict
async def call_tool(name: str, arguments: dict) -> Any
```

`call_tool` posts the JSON-RPC envelope, raises `StockNearMCPError` when the
response carries `isError` or lacks a text content block, and otherwise
returns `json.loads` of `result.content[0].text`.

Fetchers, each returning the dataclass the application already uses:

```
async def fetch_options_overview(symbol: str) -> OptionsData
async def fetch_stock_overview(symbol: str) -> StockData
async def fetch_max_pain(symbol: str) -> float | None
async def fetch_expirations(symbol: str) -> list[str]
```

Each raises `StockNearMCPNoData` when the payload for the requested symbol is
empty, so that a missing symbol never overwrites a populated cache entry with
nulls.

Both exceptions derive from a common `StockNearMCPError` base, with
`StockNearMCPNoData` a subclass of it. Callers that only need "the fetch did
not produce data" catch the base; callers that must distinguish a missing
symbol from a transport failure catch the subclass first.

### Configuration

Added to `app/config.py` and documented in `.env.example`:

- `STOCKNEAR_MCP_URL` — defaults to `https://mcp.stocknear.com/mcp`
- `STOCKNEAR_MCP_TOKEN` — bearer token, read from the environment only
- `STOCKNEAR_MCP_TIMEOUT` — request timeout in seconds, default 30

The token is a credential. It belongs in `.env`, which is gitignored.
`.env.example` documents the variable name with an empty value and must never
carry a real token.

### Field mapping

`get_ticker_options_overview_data[SYMBOL]` to `OptionsData`:

| Target field | Source | Transform |
| --- | --- | --- |
| `implied_volatility` | `impliedVolatility.current` | divide by 100 |
| `historical_volatility` | `impliedVolatility.historicalVolatility` | divide by 100 |
| `iv_rank` | `impliedVolatility.ivRank` | none (0–100 scale, frequently null) |
| `iv_percentile` | `impliedVolatility.ivPercentile` | none (0–100 scale) |
| `put_call_ratio` | `overview.putCallRatio` | none |
| `total_volume` | `overview.totalVolume` | none |
| `total_open_interest` | `overview.totalOpenInterest` | none |
| `max_pain` | `table[].maxPain` | nearest future expiry, skipping rows where `maxPain` is 0 |
| `raw_content` | whole payload | `json.dumps`, so debug endpoints keep working |

The volatility conversion is load-bearing. The MCP server reports implied
volatility as a percentage (`33.65`); every consumer in this repository treats
`implied_volatility` as a decimal fraction (`app/services/risk_analysis.py`
line 115, `app/services/speculation_analysis.py` line 366). Omitting the
division would inflate every Black-Scholes input by a factor of one hundred
without raising an error.

`get_ticker_quote[SYMBOL]` to `StockData`:

| Target field | Source |
| --- | --- |
| `price` | `price` |
| `change` | `change` |
| `change_percent` | `changesPercentage` |
| `market_cap` | `marketCap` |
| `volume` | `volume` |

### `app/services/stocknear_service.py` (modified)

The three synchronous scrape thunks — `_fetch_options_overview_sync`,
`_fetch_max_pain_sync`, `_fetch_stock_overview_sync` — and their
`run_scraper(...)` wrappers are replaced by direct `await` calls on the new
fetchers.

Cache keys, TTLs, and the merge-with-expired-cache logic are unchanged. Two
consequences follow, both intended: cache rows written by the scraper stay
valid after the switch, and an MCP outage degrades to stale cached data by the
same path a scrape failure does today.

`get_available_expirations` moves to MCP; the remaining functions in this
module that depend on contract quotes continue to call the scraper.

### Removals

From `app/stocknear.py`: `get_options_overview`, `get_max_pain`,
`get_stock_overview`, `get_available_expirations`.

Also from `app/stocknear.py`: `get_options_flow`, `get_dark_pool`,
`get_analyst_ratings`, `get_options_gex`, `get_options_dex`. These are
reachable only from the module's `main()` command-line entry point and are
never called by the application. The CLI is repointed at the corresponding MCP
tools (`get_latest_options_flow_feed`, `get_ticker_analyst_rating`, and
siblings) rather than retaining scraping code for them.

The `/debug/stocknear/{symbol}` endpoint in `app/routers/debug.py` is updated
to dump the MCP payload instead of scraped page text. The remaining debug
endpoints, which inspect contract-quote scraping, are untouched.

### Retained

Playwright remains a dependency. `get_contract_quote`,
`get_contract_quotes_batch`, `get_available_strikes`,
`get_options_chain_parsed`, the persistent scraper in
`app/services/stocknear_service.py`, and cookie extraction in
`app/stocknear_cookies.py` all stay as they are.

## Error Handling

| Condition | Behavior |
| --- | --- |
| HTTP error, timeout, or malformed JSON-RPC | `StockNearMCPError`; caller falls back to expired cache |
| `result.isError` set | `StockNearMCPError` |
| Payload `{}` or symbol key absent | `StockNearMCPNoData`; cache is not written |
| Individual field null (for example `ivRank`) | Field set to `None`, never coerced to 0 |

Failures are logged at the existing `app.services.stocknear_service` logger,
which `app/main.py` already raises to DEBUG.

## Testing

No tests currently cover StockNear ingest, so this coverage is entirely new.
Development is test-first. All tests mock `httpx`; none reach the network.

- Volatility conversion: percentage input yields decimal output.
- Empty payload raises `StockNearMCPNoData` and leaves the cache untouched.
- Null `ivRank` maps to `None` rather than 0.
- `max_pain` selects the nearest future expiry and skips rows where `maxPain`
  is 0.
- Every outbound request carries a non-empty `User-Agent`.
- `isError` responses raise `StockNearMCPError`.
- One recorded-fixture test per fetcher, using payloads captured from live
  calls and stored under `tests/fixtures/stocknear_mcp/`.

## Risks

**`ivRank` is frequently null.** The probe returned null for AAPL. This value
feeds the IV-rank display on the risk page but not any pricing calculation, so
the field renders blank rather than producing a wrong number.

**MCP rate limits are undocumented.** The server publishes no limit and the
probe did not exercise one. The existing cache TTL of one hour bounds request
volume, but a limit encountered in practice will surface as an HTTP error and
degrade to stale cache.

**Credential exposure.** The bearer token currently lives in
`~/.claude.json`. Moving it into the application's `.env` widens the number of
places it is stored. Rotation at StockNear is advisable.
