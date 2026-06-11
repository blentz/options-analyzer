"""Tests for the StockNear financials (__data.json) parser.

Uses captured real ``__data.json`` fixtures (AAPL, anonymous = ~5 annual
years) so the devalue decoding, annual-vs-quarterly selection, and FMP field
mapping are pinned without any network or auth. Premium auth only adds more
years to the same shape.
"""

from pathlib import Path

import pytest

from app.services.stocknear_financials import (
    _unflatten,
    parse_income_statement,
    parse_balance_sheet,
    parse_market_cap,
)

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def income_text() -> str:
    return (FIXTURES / "aapl_income__data.json").read_text()


@pytest.fixture
def balance_text() -> str:
    return (FIXTURES / "aapl_balance__data.json").read_text()


# ---------------------------------------------------------------------------
# devalue decoder
# ---------------------------------------------------------------------------

class TestUnflatten:
    def test_resolves_index_references(self):
        # root(0) -> object whose 'a' points to index 1 ("hi"), 'b' to index 2 (5)
        values = [{"a": 1, "b": 2}, "hi", 5]
        assert _unflatten(values) == {"a": "hi", "b": 5}

    def test_array_of_references(self):
        values = [[1, 2, 3], 10, 20, 30]
        assert _unflatten(values) == [10, 20, 30]

    def test_negative_sentinels(self):
        values = [{"x": -1}]   # -1 == undefined -> None
        assert _unflatten(values) == {"x": None}

    def test_shared_reference(self):
        # two keys pointing at the same node
        values = [{"a": 1, "b": 1}, "shared"]
        assert _unflatten(values) == {"a": "shared", "b": "shared"}


# ---------------------------------------------------------------------------
# income statement
# ---------------------------------------------------------------------------

class TestIncome:
    def test_extracts_annual_net_income(self, income_text):
        ni = parse_income_statement(income_text)
        # AAPL reported net income (USD), known public figures.
        assert ni[2021] == pytest.approx(94_680_000_000, rel=0.01)
        assert ni[2025] == pytest.approx(112_010_000_000, rel=0.01)

    def test_is_annual_not_quarterly(self, income_text):
        ni = parse_income_statement(income_text)
        # One row per fiscal year — no duplicate years from quarterly data.
        assert len(ni) == len(set(ni))
        assert all(2000 < y < 2100 for y in ni)
        # Anonymous payload exposes the most recent ~5 fiscal years.
        assert len(ni) >= 4


# ---------------------------------------------------------------------------
# balance sheet
# ---------------------------------------------------------------------------

class TestBalance:
    def test_latest_year_equity_and_tangible_items(self, balance_text):
        b = parse_balance_sheet(balance_text)
        assert b["year"] >= 2024
        assert b["book_equity"] is not None and b["book_equity"] > 0
        # AAPL reports zero goodwill; goodwill/intangibles never come back None.
        assert b["goodwill"] == 0.0
        assert b["intangibles"] is not None


# ---------------------------------------------------------------------------
# market cap
# ---------------------------------------------------------------------------

class TestMarketCap:
    def test_present_and_large(self, income_text):
        mc = parse_market_cap(income_text)
        assert mc is not None and mc > 1e11  # mega-cap


# ---------------------------------------------------------------------------
# graceful degradation
# ---------------------------------------------------------------------------

class TestRobustness:
    def test_empty_or_garbage_returns_empty(self):
        assert parse_income_statement('{"nodes":[]}') == {}
        assert parse_balance_sheet('{"nodes":[]}') == {}
        assert parse_market_cap('{"nodes":[]}') is None
