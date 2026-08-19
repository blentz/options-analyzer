"""Parser tests for Stocknear contract-history CSVs.

The None-vs-zero and IV-scale tests are the important ones. Both failure
modes produce plausible numbers that no downstream layer can detect.
"""

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app.services.contract_history import (
    ContractHistoryParseError,
    HistoryRow,
    parse_history_csv,
)

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


class TestNonIntegralCountTruncation:
    """Non-integral count values are truncated with a warning."""

    def test_truncates_and_warns(self, tmp_path, caplog):
        """A non-integral count (e.g. '4.7') is truncated to int with a logged warning."""
        csv_file = tmp_path / "test.csv"
        csv_file.write_text(
            "date,open,high,low,close,volume,open_interest\n"
            "2026-08-19,0.3,0.3,0.3,0.3,4.7,3784\n"
        )

        rows = parse_history_csv(csv_file)
        assert len(rows) == 1
        assert rows[0].volume == 4
        assert "Truncating non-integral count value '4.7' to 4" in caplog.text


class TestMissingDateColumn:
    """CSV with no date column raises ContractHistoryParseError."""

    def test_raises_on_missing_date_column(self, tmp_path):
        """CSV with no date column raises ContractHistoryParseError."""
        csv_file = tmp_path / "no_date.csv"
        csv_file.write_text(
            "open,high,low,close,volume,open_interest\n"
            "0.3,0.3,0.3,0.3,1,3784\n"
        )

        with pytest.raises(ContractHistoryParseError, match="no 'date' column"):
            parse_history_csv(csv_file)


class TestHeaderOnlyCSV:
    """Header-only CSV with no data rows raises ContractHistoryParseError."""

    def test_raises_on_header_only(self, tmp_path):
        """CSV with only headers and no data rows raises ContractHistoryParseError."""
        csv_file = tmp_path / "header_only.csv"
        csv_file.write_text(
            "date,open,high,low,close,volume,open_interest\n"
        )

        with pytest.raises(ContractHistoryParseError, match="contained no parseable rows"):
            parse_history_csv(csv_file)
