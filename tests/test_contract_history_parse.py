"""Parser tests for StockNear contract-history JSON.

The fixture is the live /api/options-contract-history payload for
HITI261016P00002500 captured 2026-09-24 (149 rows, 2026-02-19..2026-09-23).
"""

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app.services.contract_history import (
    ContractHistoryParseError,
    HistoryRow,
    parse_history_json,
)
from app.stocknear_models import ContractHistoryError

OCC = "HITI261016P00002500"
FIXTURE = Path(__file__).parent / "fixtures" / "contract_history" / f"{OCC}.json"


@pytest.fixture(scope="module")
def payload():
    return json.loads(FIXTURE.read_text())


@pytest.fixture(scope="module")
def by_date(payload):
    return {r.date: r for r in parse_history_json(payload, OCC)}


def _one(row: dict) -> dict:
    """A minimal valid payload wrapping a single history row."""
    return {"expiration": "2026-10-16", "strike": 2.5, "optionType": "put",
            "history": [{"date": "2026-09-23", **row}]}


class TestFixture:
    def test_row_count(self, payload):
        assert len(parse_history_json(payload, OCC)) == 149

    def test_rows_are_history_rows_oldest_first(self, payload):
        rows = parse_history_json(payload, OCC)
        assert all(isinstance(r, HistoryRow) for r in rows)
        assert rows[0].date == date(2026, 2, 19)
        assert rows[-1].date == date(2026, 9, 23)


class TestLatestRow:
    """The most recent row has every field populated."""

    def test_prices_are_exact_decimal(self, by_date):
        r = by_date[date(2026, 9, 23)]
        assert r.bid == Decimal("0.05")
        assert r.ask == Decimal("0.15")
        assert r.mark == Decimal("0.1")
        assert r.gex == Decimal("261566.208")

    def test_counts_are_int(self, by_date):
        r = by_date[date(2026, 9, 23)]
        assert r.volume == 0
        assert r.open_interest == 3852
        assert r.change_oi == 0

    def test_dte_is_derived_from_expiration(self, by_date):
        assert by_date[date(2026, 9, 23)].dte == 23
        assert by_date[date(2026, 8, 17)].dte == 60

    def test_greeks_are_float(self, by_date):
        r = by_date[date(2026, 9, 23)]
        assert r.delta == pytest.approx(-0.28674)
        assert r.gamma == pytest.approx(0.67904)
        assert r.ultima == pytest.approx(-0.27066)

    def test_iv_is_decimal_not_percent(self, by_date):
        assert by_date[date(2026, 9, 23)].implied_volatility == pytest.approx(0.73437)


class TestSparseRows:
    """Older rows omit the second-order greeks; null is None, never 0."""

    def test_absent_greeks_are_none_not_zero(self, by_date):
        r = by_date[date(2026, 2, 19)]
        assert r.charm is None
        assert r.ultima is None
        assert r.gex is None

    def test_present_values_still_parse(self, by_date):
        r = by_date[date(2026, 2, 20)]
        assert r.delta == pytest.approx(-0.3666)
        assert r.total_premium == Decimal("0")

    def test_counts_stored_as_received(self, by_date):
        feb19, feb20 = by_date[date(2026, 2, 19)], by_date[date(2026, 2, 20)]
        assert (feb19.volume, feb19.open_interest) == (4, None)
        assert (feb20.volume, feb20.open_interest) == (None, 4)


class TestValueCoercion:
    def test_decimal_uses_the_printed_value_not_binary_float(self):
        [r] = parse_history_json(_one({"close": 0.3}), OCC)
        assert r.close == Decimal("0.3")

    def test_integral_float_count(self):
        [r] = parse_history_json(_one({"volume": 4.0}), OCC)
        assert r.volume == 4

    def test_truncates_non_integral_count_and_warns(self, caplog):
        [r] = parse_history_json(_one({"volume": 4.7}), OCC)
        assert r.volume == 4
        assert "Truncating non-integral count value 4.7 to 4" in caplog.text

    def test_unparseable_values_warn_and_become_none(self, caplog):
        [r] = parse_history_json(_one({"open": "$0.30", "volume": "N/A", "delta": "N/A"}), OCC)
        assert (r.open_, r.volume, r.delta) == (None, None, None)
        assert "Could not parse decimal value" in caplog.text
        assert "Could not parse integer value" in caplog.text
        assert "Could not parse float value" in caplog.text

    def test_nulls_do_not_warn(self, caplog):
        [r] = parse_history_json(_one({"open": None, "volume": None}), OCC)
        assert r.open_ is None and r.volume is None
        assert "Could not parse" not in caplog.text

    def test_duplicate_date_keeps_later_row(self, caplog):
        payload = _one({"close": 0.1})
        payload["history"].append({"date": "2026-09-23", "close": 0.2})
        [r] = parse_history_json(payload, OCC)
        assert r.close == Decimal("0.2")
        assert "Duplicate date" in caplog.text

    def test_skips_row_with_bad_date(self, caplog):
        payload = _one({"close": 0.1})
        payload["history"].append({"date": "yesterday", "close": 0.2})
        assert len(parse_history_json(payload, OCC)) == 1
        assert "unparseable date" in caplog.text


class TestRejections:
    def test_unknown_contract(self):
        with pytest.raises(ContractHistoryError, match="unknown contract"):
            parse_history_json([], OCC)

    @pytest.mark.parametrize("field,value", [
        ("expiration", "2026-10-23"),
        ("strike", 3.0),
        ("optionType", "call"),
    ])
    def test_refuses_a_different_contract(self, field, value):
        payload = _one({"close": 0.1})
        payload[field] = value
        with pytest.raises(ContractHistoryError, match="refusing to store"):
            parse_history_json(payload, OCC)

    def test_substitution_is_not_reported_as_a_parse_error(self):
        payload = _one({"close": 0.1})
        payload["strike"] = 3.0
        with pytest.raises(ContractHistoryError) as exc:
            parse_history_json(payload, OCC)
        assert not isinstance(exc.value, ContractHistoryParseError)

    def test_payload_without_identity(self):
        with pytest.raises(ContractHistoryParseError, match="does not identify"):
            parse_history_json({"history": [{"date": "2026-09-23"}]}, OCC)

    def test_payload_without_history_list(self):
        with pytest.raises(ContractHistoryParseError, match="no history list"):
            parse_history_json({"expiration": "2026-10-16"}, OCC)

    def test_no_parseable_rows(self):
        payload = _one({})
        payload["history"] = [{"date": "garbage"}]
        with pytest.raises(ContractHistoryParseError, match="no parseable rows"):
            parse_history_json(payload, OCC)
