"""Parser tests for Stocknear contract-history CSVs.

The None-vs-zero and IV-scale tests are the important ones. Both failure
modes produce plausible numbers that no downstream layer can detect.
"""

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app.services.contract_history import HistoryRow, parse_history_csv

FIXTURE = Path(__file__).parent / "fixtures" / "contract_history" / "HITI261016P00002500.csv"


class TestParseShape:
    def test_row_count(self):
        rows = parse_history_csv(FIXTURE)
        assert len(rows) == 124

    def test_returns_history_rows(self):
        rows = parse_history_csv(FIXTURE)
        assert all(isinstance(r, HistoryRow) for r in rows)

    def test_dates_parse(self):
        rows = parse_history_csv(FIXTURE)
        by_date = {r.date: r for r in rows}
        assert date(2026, 8, 19) in by_date
        assert date(2026, 2, 19) in by_date


class TestFullyPopulatedRow:
    """The most recent row has every column populated."""

    def _row(self):
        return {r.date: r for r in parse_history_csv(FIXTURE)}[date(2026, 8, 19)]

    def test_prices_are_decimal(self):
        r = self._row()
        assert r.close == Decimal("0.3")
        assert r.bid == Decimal("0.15")
        assert r.ask == Decimal("0.35")
        assert r.mark == Decimal("0.25")

    def test_counts_are_int(self):
        r = self._row()
        assert r.volume == 1
        assert r.open_interest == 3784
        assert r.dte == 58

    def test_greeks_are_float(self):
        r = self._row()
        assert r.delta == pytest.approx(-0.4856)
        assert r.gamma == pytest.approx(0.7051)
        assert r.ultima == pytest.approx(-0.0372)

    def test_iv_is_decimal_not_percent(self):
        # The page renders this as 58.12%. The CSV already gives 0.5812.
        # Dividing by 100 here would silently shrink every IV 100x.
        r = self._row()
        assert r.implied_volatility == pytest.approx(0.5812)


class TestSparseRows:
    """Older rows omit the second-order greeks entirely."""

    def _feb20(self):
        return {r.date: r for r in parse_history_csv(FIXTURE)}[date(2026, 2, 20)]

    def test_absent_greeks_are_none_not_zero(self):
        r = self._feb20()
        assert r.charm is None
        assert r.vanna is None
        assert r.ultima is None

    def test_present_greeks_still_parse(self):
        r = self._feb20()
        assert r.delta == pytest.approx(-0.3666)
        assert r.vega == pytest.approx(0.0076)

    def test_absent_quotes_are_none(self):
        r = self._feb20()
        assert r.bid is None
        assert r.ask is None


class TestSourceAnomalyPinned:
    """Feb 19 carries 4 in volume with open_interest empty; Feb 20 carries
    4 in open_interest with volume empty. This is either sparse data or a
    column shift at the source — we do not know which, so we store what we
    were given. This test exists so that changing that is deliberate.
    """

    def test_stored_as_received(self):
        by_date = {r.date: r for r in parse_history_csv(FIXTURE)}
        feb19 = by_date[date(2026, 2, 19)]
        feb20 = by_date[date(2026, 2, 20)]

        assert feb19.volume == 4
        assert feb19.open_interest is None

        assert feb20.volume is None
        assert feb20.open_interest == 4
