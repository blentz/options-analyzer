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
- **Data**: SQLite database
- **Deployment**: Podman container

## Running with Podman

```bash
# Build the container
podman build -t options-analyzer .

# Run with persistent data
podman run -d --name options-analyzer \
  -p 8000:8000 \
  -v ./data:/app/data \
  options-analyzer

# Access at http://localhost:8000
```

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
│       └── price_service.py # Yahoo Finance price fetching
├── templates/               # Jinja2 HTML templates
├── data/                    # SQLite database (persistent volume)
├── Dockerfile
└── requirements.txt
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
