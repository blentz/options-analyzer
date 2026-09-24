"""StockNear command-line helper and historical re-exports.

The Playwright browser that used to live here is gone. All StockNear data
is plain HTTP now: symbol-level data from the MCP server
(app.services.stocknear_mcp), per-contract quotes and history from the
contract JSON API (app.services.stocknear_contract_api). The data shapes
live in app.stocknear_models and are re-exported below so the historical
`from app.stocknear import OptionContract` etc. keep working.
"""

import json
import sys
from dataclasses import asdict
from typing import Any

from app.stocknear_cookies import extract_browser_cookies
from app.stocknear_models import (
    ContractQuote,
    OptionContract,
    OptionsChain,
    OptionsData,
    StockData,
)

__all__ = [
    "OptionContract", "ContractQuote", "OptionsChain", "OptionsData",
    "StockData", "extract_browser_cookies",
]


def main():
    """CLI interface.

    All commands go through the MCP server. Run with
    `python -m app.stocknear <command>`.
    """
    import asyncio

    from app.services.stocknear_mcp import (
        call_tool,
        fetch_expirations,
        fetch_options_chain,
        fetch_options_overview,
        fetch_stock_overview,
    )

    if len(sys.argv) < 3:
        print("Usage: python -m app.stocknear <command> <symbol>")
        print("\nMCP-backed commands:")
        print("  stock <symbol>             - Get stock overview")
        print("  options-overview <symbol>  - Get options overview (IV, OI, volume)")
        print("  max-pain <symbol>          - Get max pain analysis")
        print("  expirations <symbol>       - List available option expiries")
        print("  ratings <symbol>           - Get analyst ratings")
        print("  flow <symbol>              - Get options flow (unusual orders)")
        print("  options-chain <symbol>     - Expirations and strikes with open interest")
        sys.exit(1)

    command = sys.argv[1].lower()
    symbol = sys.argv[2]

    result: Any
    if command == "options-chain":
        result = asdict(asyncio.run(fetch_options_chain(symbol)))
    elif command == "stock":
        stock_data = asyncio.run(fetch_stock_overview(symbol))
        result = {"symbol": stock_data.symbol, "price": stock_data.price, "change": stock_data.change}
    elif command == "options-overview":
        options_data = asyncio.run(fetch_options_overview(symbol))
        result = {
            "symbol": options_data.symbol,
            "iv_rank": options_data.iv_rank,
            "iv_percentile": options_data.iv_percentile,
            "implied_volatility": options_data.implied_volatility,
            "put_call_ratio": options_data.put_call_ratio,
            "total_volume": options_data.total_volume,
            "total_open_interest": options_data.total_open_interest,
        }
    elif command == "max-pain":
        options_data = asyncio.run(fetch_options_overview(symbol))
        result = {"symbol": options_data.symbol, "max_pain": options_data.max_pain}
    elif command == "expirations":
        result = {"symbol": symbol.upper(),
                  "expirations": asyncio.run(fetch_expirations(symbol))}
    elif command == "ratings":
        result = asyncio.run(
            call_tool("get_ticker_analyst_rating", {"tickers": [symbol.upper()]})
        )
    elif command == "flow":
        result = asyncio.run(
            call_tool("get_ticker_unusual_activity", {"tickers": [symbol.upper()]})
        )
    else:
        print(f"Unknown command or missing arguments: {command}")
        sys.exit(1)

    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
