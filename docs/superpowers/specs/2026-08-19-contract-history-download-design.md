# Options Contract History Download — Design

**Date:** 2026-08-19
**Status:** Approved, ready for implementation planning

## Problem

The application has no time series for an individual options contract. The
MCP server (`get_ticker_hottest_options_contracts`) returns a single-day
snapshot for the ten busiest contracts on a symbol, and only when the contract
ranks in that top ten. The scraper's `get_contract_quote` likewise returns the
current quote and nothing historical.

Analyzing how a held contract has priced over its life — premium decay against
theta, IV expansion and collapse, open interest building or bleeding away —
requires a per-day series that neither path provides.

Stocknear's web UI does have one. Its contract-lookup page renders a "Contract
History" section with a Download menu offering CSV and Excel, and the CSV is
substantially richer than the table displayed above it.

## Source Investigation

Probes against `https://www.stocknear.com/stocks/HITI/options/contract-lookup`
for `HITI261016P00002500` established the following.

The rendered table shows 11 columns and 50 rows. The CSV behind the Download
menu carries 34 columns and 123 rows, reaching back to the contract's
inception rather than a trailing window:

```
date,open,high,low,close,volume,open_interest,bid,ask,mark,
implied_volatility,delta,gamma,theta,vega,rho,epsilon,lambda,charm,vanna,
vomma,veta,vera,speed,zomma,color,ultima,changeOI,changesPercentageOI,
gex,dex,total_premium,dte
```

Three properties of the source shape the design.

**Each download is a complete history.** The CSV spans inception to the most
recent session, so a fetch is idempotent and self-healing: a skipped week
costs nothing, because the next fetch returns those rows too. This removes any
need for scheduled polling or a snapshot accumulator.

**Older rows are sparse.** By February the second-order greeks (`charm`
through `ultima`) are empty, and `bid`/`ask` are absent on days with no
quotes. Sparsity is normal source behavior, not corruption.

**The page substitutes contracts silently.** Requesting an expiration outside
the current subscription tier does not error. The URL is rewritten to a
nearer expiration, a small banner reads "The requested expiration date
requires a Pro subscription. Showing the nearest available date", and the page
serves that other contract's history with HTTP 200 and well-formed CSV. An
unauthenticated request for `HITI261016P00002500` returned
`HITI260821P00002500` instead. This is the primary correctness hazard in the
feature and is addressed in Error Handling below.

## Decisions

1. **Open positions only.** The sync set is contracts with an open position.
   Watchlist and research contracts are out of scope; `option_contracts` has
   no representation for a contract that was never traded, and adding one is
   not justified by present need.
2. **Full fidelity storage.** All 34 columns persist to a typed table, one row
   per (contract, date). Storing a queryable subset would discard exactly the
   second-order greeks that make historical pricing analysis worth doing, and
   would leave two sources of truth if raw CSVs were archived alongside.
3. **User-triggered sync.** A button in the UI drives a blocking endpoint. An
   earlier lazy-on-read design with stale-while-revalidate was rejected: it
   put a browser launch behind a page render and required background-refresh
   orchestration that a button makes unnecessary.
4. **One browser session per sync.** The sync covers all open positions in a
   single scraper session, amortizing the ~15 second browser launch across the
   batch rather than paying it per contract.
5. **Split at the Playwright boundary.** Browser work stays in
   `app/stocknear.py`; parsing, upsert, and orchestration live in a service
   module that does not import Playwright. This follows the split already made
   for `app/stocknear_cookies.py` and keeps the logic most likely to harbor
   bugs testable without a browser.

## Components

### `app/models.py` — `ContractHistory`

One row per contract per trading day.

| Group | Columns | Type |
| --- | --- | --- |
| Identity | `contract_id` FK → `option_contracts.id`, `date` | unique index `(contract_id, date)` |
| Prices | `open`, `high`, `low`, `close`, `bid`, `ask`, `mark` | `Numeric(12,4)` |
| Activity | `volume`, `open_interest`, `change_oi`, `dte` | `Integer` |
| Volatility | `implied_volatility`, `changes_percentage_oi` | `Float` |
| Greeks | `delta`, `gamma`, `theta`, `vega`, `rho`, `epsilon`, `lambda_`, `charm`, `vanna`, `vomma`, `veta`, `vera`, `speed`, `zomma`, `color`, `ultima` | `Float` |
| Aggregates | `gex`, `dex`, `total_premium` | `Numeric(16,4)` |

Every column except `contract_id` and `date` is nullable. The February rows
demonstrate that absent values are routine; a NOT NULL constraint would reject
valid source data.

Greeks use `Float` rather than the `Numeric` this codebase applies to money.
They are not money, and exact decimal semantics gain nothing for a vanna
value. `lambda` is a Python keyword, so the mapped attribute is `lambda_`
against column name `lambda`.

### `app/models.py` — `ContractHistorySync`

Ingest bookkeeping, one row per contract: `contract_id` (unique FK),
`last_attempt_at`, `last_success_at`, `last_error`, `row_count`. Kept in its
own table so that sync state does not accumulate on the domain model.

### Alembic migration

Creates both tables and their indexes.

### `app/stocknear.py` — download support

`start()` gains `accept_downloads=True` on `new_context()`. The context
currently cannot receive downloads at all.

```
download_contract_history(occ_symbol: str, dest_dir: Path) -> Path
```

Rate limit, navigate to
`/stocks/{underlying}/options/contract-lookup?contract={occ_symbol}`, verify
the served contract matches the requested one, click `Download`, click
`Download to CSV` in the menu that opens, capture through `expect_download()`,
save, return the path.

The underlying is taken from `OptionContract.symbol`, not parsed off the front
of the OCC symbol. Splitting an OCC symbol on its date component is ambiguous
for tickers containing digits, and the caller already holds the contract row.

The Download control is a menu, not a direct link — a single click opens a
group containing `Download to CSV` and `Download to Excel`. Both clicks are
required.

`_build_contract_id()` already produces the OCC symbol this URL expects
(`{SYMBOL}{YYMMDD}{P|C}{STRIKE*1000:08d}`) and is reused rather than
duplicated.

### `app/services/contract_history.py`

No Playwright import.

- `parse_history_csv(path) -> list[HistoryRow]` — pure, no I/O beyond the read
- `upsert_history(db, contract, rows) -> int` — upsert on `(contract_id, date)`
- `sync_open_positions(db, force=False) -> SyncSummary` — orchestrator

`HistoryRow` and `SyncSummary` are dataclasses defined in this module.
`HistoryRow` mirrors the `ContractHistory` columns without the foreign key, so
that parsing is independent of the ORM and testable against a bare CSV.
`SyncSummary` carries the counts and the per-contract error list the endpoint
returns.

The orchestrator selects contracts with open positions, skips any whose
`last_success_at` falls within the TTL unless `force` is set, then makes one
`asyncio.to_thread` call that opens a single scraper session and iterates the
batch. Per-contract failures are caught inside the loop so that one bad
contract does not abort the remainder.

The worker thread performs no database access. `sync_playwright()` cannot run
on a thread with a running event loop, and the session is aiosqlite and bound
to the loop; the thread returns parsed rows or exceptions, and all writes
happen on the async side.

### `app/main.py` — endpoint

`POST /api/contract-history/sync`, beside the existing `/api/positions/heal`
and `/api/cycles/rebuild`, which establish the pattern of a user-triggered,
idempotent maintenance action returning a summary. Returns
`{contracts_total, synced, skipped, failed, rows_upserted, errors[]}`.

### `templates/positions.html` — sync button

Posts to the endpoint and renders the summary. Per-contract `last_success_at`
and `last_error` display on the position rows.

### `app/config.py`

`stocknear_history_ttl_seconds: int = 43200` (12 hours). The source updates
once per trading day; a 12-hour TTL fetches each day's data about once without
requiring a market calendar the application does not have.

## Error Handling

**Contract substitution.** After navigation the served contract must be
verified against the requested one. The authoritative check parses the page
URL and compares its `contract` query parameter for exact, case-insensitive
equality with the requested OCC symbol. A mismatch raises `ProGatedError`, as
does a URL carrying no `contract` parameter at all — being unable to confirm
which contract was served must never be treated as confirmation that it was
the right one.

An earlier draft of this design asserted only that the URL *contained* the
requested symbol. That is bypassable and was replaced: substring containment
passes a URL that serves one contract while echoing the requested one in a
second parameter, and passes a longer symbol that merely has the requested one
as a prefix. Exact parameter comparison closes both.

The Pro-subscription banner is checked too, before the URL, so that a page
rendering the banner before rewriting its URL still raises. It is a secondary
signal only: the banner is prose Stocknear controls and can be reworded at any
time, whereas a substitution necessarily changes the `contract` parameter.
Detection must not depend on the wording.

The guard also rejects an empty or non-string requested symbol, raising
`ContractHistoryError`. An empty symbol would otherwise make the comparison
vacuous and silently approve whatever was served.

This check is not defensive padding. The substitution returns HTTP 200 with
valid, well-formed CSV for a real contract, so every layer downstream — the
parser, the upsert, the analysis — will accept it. Without the guard, an
expired cookie or a downgraded subscription does not fail; it writes another
contract's prices into the requested contract's history, and no later check
can detect the corruption.

**Authentication failure.** A redirect to `/login` or the Google OAuth flow
raises `AuthExpiredError`. Observed behavior: unauthenticated sessions on this
page are bounced to login within seconds of load.

**Download failure.** A missing menu, absent CSV item, or no download event
within the timeout raises `DownloadTimeoutError`.

All three record to `ContractHistorySync.last_error` against the contract and
surface in the endpoint's `errors[]`, so a failed sync is visible in the UI
rather than only in logs. Failures do not halt the batch.

**Parse failure.** The downloaded file is parsed from a temporary directory
and discarded on success. On parse failure the file is retained and its path
logged, so the malformed input remains available for diagnosis.

## Testing

Nearly all coverage runs without a browser, which is the purpose of the
Playwright split.

| Test | Asserts |
| --- | --- |
| Parse fixture | The 123-row HITI CSV parses to 123 rows with correct types |
| Sparse greeks | Absent second-order greeks parse to `None`, never `0` |
| Column shift | The Feb 19/20 volume-vs-OI anomaly stores as given |
| Idempotency | Parse and upsert twice; row count unchanged |
| Correction | A restated value in a later fetch overwrites the earlier row |
| TTL skip | Contracts inside the TTL are skipped; `force` overrides |
| Error recording | Each error type persists to `last_error` and appears in the summary |
| Batch isolation | One failing contract does not prevent others from syncing |

The distinction between `None` and `0` matters more than it appears: a delta
of zero is a meaningful value for a deep out-of-the-money contract, and
collapsing an absent greek into zero would silently manufacture data.

One integration test performs a real authenticated download. It is marked and
skipped by default, since it requires valid cookies and a live subscription.

## Risks

**Markup dependence.** The download path depends on Stocknear's button and
menu structure. A redesign breaks it. The failure is loud — `DownloadTimeout`,
recorded and surfaced — rather than silent, which is the acceptable form for
this class of risk.

**Subscription coupling.** History for a given expiration is available only
within the current subscription tier. Downgrading silently narrows what can be
fetched; the substitution guard converts that into an explicit `ProGatedError`
per contract.

**Unresolved source anomaly.** In the HITI fixture, `2026-02-20` carries `4`
in `open_interest` with `volume` empty, while `2026-02-19` carries `4` in
`volume` with `open_interest` empty. This is either genuinely sparse
early-contract data or a column-alignment bug at the source. The two demand
opposite handling, and the evidence available does not distinguish them.

Rows are therefore stored exactly as received, with no repair. A test pins
that behavior so a future decision to correct it is deliberate rather than
accidental. Resolving it requires a second contract's history to compare
against — worth revisiting once several contracts have synced.

**Sync duration.** A blocking sync across a large book grows linearly at
roughly 2–4 seconds per contract after the initial launch. At twenty open
positions this is about a minute. Should the book grow enough for this to
strain a request timeout, the escalation is a background job with progress
polling, deliberately deferred as unnecessary at current scale.
