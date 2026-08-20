# Options Trading Analyzer

A web application for analyzing options trading performance from Fidelity account exports. Built with FastAPI, SQLite, and Bokeh for interactive visualizations.

## Features

- **CSV Import**: Import transaction history from Fidelity CSV exports (batch upload supported)
- **Position Tracking**: Automatically tracks options positions across multiple trades
- **Complete P&L Analysis**: Tracks both options premium P&L and underlying stock P&L from assignments
- **Risk Analysis**: Real-time payoff diagrams with current market prices from Yahoo Finance
- **Performance Dashboard**: Win rate, cumulative P&L, monthly breakdown, and strategy analysis
- **Dark Theme UI**: Modern, responsive interface

## Pages

- **Dashboard** (`/`): Overall statistics, cumulative P&L chart, monthly performance, P&L by symbol
- **Positions** (`/positions`): All positions with filtering (All/Open/Closed)
- **Risk Analysis** (`/risk`): Open position payoff diagrams with real-time prices
- **Import** (`/import`): Upload Fidelity CSV files

## Tech Stack

- **Backend**: Python 3.12, FastAPI, SQLAlchemy (async), aiosqlite
- **Frontend**: Jinja2 templates, Bokeh charts
- **Data**: SQLite database (WAL mode, foreign keys ON)
- **Deployment**: Podman container
- **Dep management**: [uv](https://docs.astral.sh/uv/) (`pyproject.toml` + `uv.lock`)

## Development

```bash
# Sync deps (creates .venv automatically)
uv sync --extra dev

# Run the app locally
uv run uvicorn app.main:app --reload

# Run tests
uv run pytest
```

## Running with Podman

`run.sh` is the supported entry point — it wires up configuration and the
browser profile mount, which a bare `podman run` does not.

```bash
cp .env.example .env      # then fill in STOCKNEAR_MCP_TOKEN
./run.sh build
./run.sh start            # http://127.0.0.1:8000
./run.sh logs
```

Use `127.0.0.1`, not `localhost`. Podman's pasta backend forwards IPv4
only, and on hosts where `localhost` resolves to `::1` first the connection
is simply refused while the app is perfectly healthy.

Point it at a different browser profile with:

```bash
BROWSER_PROFILE=~/.librewolf/abcd1234.default ./run.sh start
```

### What gets mounted, and why

Configuration is **passed in at runtime, never baked into the image**.
`.containerignore` excludes `.env` from the build context because it holds
`STOCKNEAR_MCP_TOKEN`, and an image layer keeps a secret forever.

| Mount | Mode | Purpose |
|---|---|---|
| `./data` → `/app/data` | read-write | SQLite database, persisted across rebuilds |
| browser profile → `/app/browser-profile` | **read-only** | `cookies.sqlite`, for the authenticated StockNear scraper |

The profile is mounted read-only: the cookie reader copies `cookies.sqlite`
to a temporary directory before opening it, so it never needs write access
to your live browser data.

**One container at a time per volume.** Both mounts use `Z`, which assigns
*private* per-container SELinux categories. Running a second container
against `./data` — a one-off `podman run` for debugging, say — relabels it
with that container's categories and locks the running app out, producing
`unable to open database file` from a container that was working moments
before. `./run.sh restart` fixes it by relabelling to match.

**SELinux note.** The profile mount uses `:ro,Z`. The `Z` relabels the
profile directory on the host to `container_file_t` with per-container MCS
categories, and that change persists after the container exits — `:ro`
constrains the container's writes, not podman's relabelling of the source.
Without it the mount is unreadable under enforcing SELinux and fails
quietly: the reader reports no cookies found and every sync fails
authentication, which looks identical to an expired session. Firefox runs
unconfined and keeps access to the relabelled profile. To undo:

```bash
restorecon -R ~/.mozilla/firefox/<profile>
```

Starting without `.env` or without a profile works, but the app runs
degraded — it warns on startup, StockNear data falls back to cache, and
contract-history sync fails to authenticate.

Running the app directly instead (`uv run uvicorn app.main:app --reload`)
requires `DATABASE_PATH` and `STOCKNEAR_BROWSER_PROFILE_PATH` in `.env` to
be **host** paths; the defaults in `.env.example` are container paths.

## Project Structure

```
├── app/
│   ├── main.py              # FastAPI application and routes
│   ├── models.py            # SQLAlchemy models
│   ├── database.py          # Database configuration
│   ├── charts.py            # Bokeh chart generation
│   └── services/
│       ├── csv_import.py    # Fidelity CSV parsing and import
│       ├── analytics.py     # Statistics and reporting
│       ├── risk_analysis.py # Options payoff calculations
│       ├── price_service.py # Yahoo Finance price fetching
│       ├── stocknear_mcp.py # StockNear MCP client (symbol-level data)
│       └── contract_history.py # Contract-history CSV ingest and sync
├── templates/               # Jinja2 HTML templates
├── migrations/              # Alembic migrations
├── data/                    # SQLite database (persistent volume)
├── Containerfile
├── run.sh                   # Container build/start/stop helper
├── .env.example             # Configuration template
└── pyproject.toml           # Dependencies (uv / uv.lock)
```

## Data Model

- **OptionContract**: Unique options contracts (symbol, expiration, strike, type)
- **OptionTrade**: Individual transactions (opening, closing, expired, assigned)
- **OptionPosition**: Aggregated position data with P&L calculations
- **UnderlyingTrade**: Stock transactions from assignments/exercises
- **ImportLog**: CSV import history for duplicate detection

## Importing Data

1. Go to Fidelity.com → Accounts → Activity & Orders → History
2. Download CSV export
3. Upload at `/import` (supports multiple files)

The importer:
- Parses Fidelity's specific CSV format
- Detects duplicate trades automatically
- Links underlying stock trades to assigned options
- Calculates complete P&L including assignment outcomes

## API Endpoints

- `GET /api/stats` - Overall trading statistics
- `GET /api/positions` - Position list with P&L
- `GET /api/risk` - Risk analysis for open positions
