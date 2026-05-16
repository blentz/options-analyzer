"""Tests for CSV import helpers (pure functions, no DB)."""

from datetime import datetime, date, timedelta
from dataclasses import dataclass
from decimal import Decimal

from app.services.csv_import import (
    parse_option_symbol,
    parse_csv_content,
    _pick_position_for_underlying,
    ParsedUnderlyingTrade,
    AUTO_EXPIRY_GRACE_DAYS,
)


# ----------------------------------------------------------------------------
# Header detection — Fidelity has used multiple header variants over the
# years. The strict `startswith('Run Date,')` check silently rejected entire
# quarterly exports from 2021–2024. These tests pin the supported variants.
# ----------------------------------------------------------------------------

_SAMPLE_ROW = (
    '11/01/2025,11/03/2025,INDIVIDUAL,"YOU SOLD OPENING TRANSACTION ",'
    '-AAPL251121P150,"PUT (AAPL) APPLE INC NOV 21 25 $150 (100 SHS)",'
    '-1,1.50,0.65,0,148.35,Cash'
)
_HEADER_COLUMNS = (
    "Run Date,Settlement Date,Account,Action,Symbol,Description,"
    "Quantity,Price,Commission,Fees,Amount,Type"
)


class TestCsvHeaderVariants:
    def test_unquoted_header(self):
        csv_text = f"{_HEADER_COLUMNS}\n{_SAMPLE_ROW}\n"
        trades, _ = parse_csv_content(csv_text)
        assert len(trades) == 1

    def test_quoted_header(self):
        # Older Fidelity exports wrap every column name in double quotes.
        quoted = ",".join(f'"{c}"' for c in _HEADER_COLUMNS.split(","))
        csv_text = f"{quoted}\n{_SAMPLE_ROW}\n"
        trades, _ = parse_csv_content(csv_text)
        assert len(trades) == 1

    def test_header_with_leading_whitespace(self):
        csv_text = f"   {_HEADER_COLUMNS}\n{_SAMPLE_ROW}\n"
        trades, _ = parse_csv_content(csv_text)
        assert len(trades) == 1

    def test_header_with_leading_metadata_lines(self):
        # Fidelity often prepends a few descriptive lines before the header.
        csv_text = (
            "Brokerage\n"
            "Account # 12345678\n"
            "Date Range: 01/01/2024 - 03/31/2024\n"
            "\n"
            f"{_HEADER_COLUMNS}\n"
            f"{_SAMPLE_ROW}\n"
        )
        trades, _ = parse_csv_content(csv_text)
        assert len(trades) == 1

    def test_no_header_returns_empty(self):
        # Should NOT raise — just return empty so the caller can log.
        csv_text = "Some,Random,CSV\n1,2,3\n"
        trades, underlying = parse_csv_content(csv_text)
        assert trades == [] and underlying == []

    def test_account_filter_disabled_by_default(self):
        # Old default ("INDIVIDUAL") silently dropped trades from
        # differently-labeled accounts. New default = None = no filter.
        row_with_other_account = _SAMPLE_ROW.replace(
            "INDIVIDUAL", "Individual - TOD"
        )
        csv_text = f"{_HEADER_COLUMNS}\n{row_with_other_account}\n"
        trades, _ = parse_csv_content(csv_text)
        assert len(trades) == 1, "should not be filtered without explicit filter"

    def test_account_filter_is_case_insensitive(self):
        row = _SAMPLE_ROW.replace("INDIVIDUAL", "individual")
        csv_text = f"{_HEADER_COLUMNS}\n{row}\n"
        trades, _ = parse_csv_content(csv_text, account_filter="INDIVIDUAL")
        assert len(trades) == 1


# ----------------------------------------------------------------------------
# Fidelity column-shift handling — the actual format Fidelity ships with
# (Quantity column literally containing "USD" for many row types). Regression
# for the bug that silently swallowed years of old options activity.
# ----------------------------------------------------------------------------

# Real columns Fidelity uses across both layouts (verified from a Q121 file)
_FIDELITY_COLUMNS = (
    "Run Date,Account,Account Number,Action,Symbol,Description,Type,"
    "Exchange Quantity,Exchange Currency,Quantity,Currency,Price,Exchange Rate,"
    "Commission,Fees,Accrued Interest,Amount,Settlement Date"
)


class TestFidelityColumnShift:
    def test_expired_call_with_shifted_layout(self):
        """Q421-style EXPIRED CALL row: Quantity='USD', Price=qty (5 contracts)."""
        row = (
            '12/20/2021,"INDIVIDUAL","X35956562","EXPIRED CALL (PBI) PITNEY BOWES INC COMDEC 17 21 $9 as of Dec-17-2021 CALL (PBI) PITNEY BOWES INC COMDEC 17 21 $9 (100 SHS) (Cash)",'
            ' -PBI211217C9,"CALL (PBI) PITNEY BOWES INC COMDEC 17 21 $9 (100 SHS)",'
            'Cash,0,,USD,,5,0,,,,0.00,'
        )
        csv_text = f"{_FIDELITY_COLUMNS}\n{row}\n"
        trades, _ = parse_csv_content(csv_text)
        assert len(trades) == 1, "expired option in shifted layout must parse"
        t = trades[0]
        assert t.contract.symbol == "PBI"
        assert t.contract.option_type == "CALL"
        # Quantity is in the Price column for shifted rows.
        assert t.quantity == 5
        # Expired worthless — premium per share is 0 (Currency column was empty).
        assert t.price == Decimal("0")
        assert t.amount == Decimal("0.00")

    def test_assigned_put_with_shifted_layout(self):
        """Q421-style ASSIGNED PUT — same shifted layout, premium=0."""
        row = (
            '12/20/2021,"INDIVIDUAL","X35956562","ASSIGNED as of Dec-17-2021 PUT (HITI) HIGH TIDE INC COM DEC 17 21 $7.5 (100 SHS) (Cash)",'
            ' -HITI211217P7.5,"PUT (HITI) HIGH TIDE INC COM DEC 17 21 $7.5 (100 SHS)",'
            'Cash,0,,USD,,1,0,,,,0.00,'
        )
        csv_text = f"{_FIDELITY_COLUMNS}\n{row}\n"
        trades, _ = parse_csv_content(csv_text)
        assert len(trades) == 1
        t = trades[0]
        assert t.contract.symbol == "HITI"
        assert t.contract.option_type == "PUT"
        assert t.quantity == 1
        assert t.contract.strike == Decimal("7.5")

    def test_modern_layout_still_parses(self):
        """Q126-style YOU SOLD OPENING: Quantity is a real integer, Price is premium."""
        row = (
            '11/01/2025,11/03/2025,"INDIVIDUAL","X35956562","YOU SOLD OPENING TRANSACTION ",'
            '-AAPL251121P150,"PUT (AAPL) APPLE INC NOV 21 25 $150 (100 SHS)",'
            'Cash,0,,-1,USD,1.50,1.00,0.65,0,,148.35,Cash'
        )
        # This row uses the OLD column ordering (no Account Number after Settlement)
        # but the parser only cares about the named columns; rebuild with the
        # standard Fidelity header.
        row_with_qty_in_qty = (
            '11/01/2025,"INDIVIDUAL","X35956562","YOU SOLD OPENING TRANSACTION CALL (AAPL) ...",'
            ' -AAPL251121P150,"PUT (AAPL) APPLE INC NOV 21 25 $150 (100 SHS)",'
            'Cash,0,,-1,USD,1.50,1.00,0.65,0,,148.35,11/03/2025'
        )
        csv_text = f"{_FIDELITY_COLUMNS}\n{row_with_qty_in_qty}\n"
        trades, _ = parse_csv_content(csv_text)
        assert len(trades) == 1
        t = trades[0]
        assert t.quantity == -1
        assert t.price == Decimal("1.50")
        assert t.amount == Decimal("148.35")

    def test_stock_trade_shifted_layout_parses_quantity_correctly(self):
        """Q121-style YOU BOUGHT stock — Quantity='USD', Currency=0.45, Price=200.
        Stock trades only persist when linked to an assignment, but the
        parsed underlying-trade record still needs the right qty/price."""
        from app.services.csv_import import _parse_underlying_trade
        row_dict = {
            'Run Date': '02/23/2021',
            'Account': 'INDIVIDUAL',
            'Action': 'YOU BOUGHT HIGH TIDE INC',
            'Symbol': '42981E104',
            'Description': 'HIGH TIDE INC COM',
            'Quantity': 'USD',
            'Currency': '0.45',
            'Price': '200',
            'Amount': '-90',
        }
        ut = _parse_underlying_trade(row_dict, row_dict['Action'], row_dict['Symbol'], row_dict['Description'])
        assert ut is not None
        assert ut.quantity == 200
        assert ut.price == Decimal("0.45")
        assert ut.amount == Decimal("-90")


# ----------------------------------------------------------------------------
# parse_option_symbol — Fidelity description format
# ----------------------------------------------------------------------------

class TestParseOptionSymbol:
    def test_put_parses(self):
        c = parse_option_symbol(
            symbol="-AAPL251121P150",
            description="PUT (AAPL) APPLE INC NOV 21 25 $150 (100 SHS)",
        )
        assert c is not None
        assert c.symbol == "AAPL"
        assert c.option_type == "PUT"
        assert c.strike == Decimal("150")
        assert c.expiration == datetime(2025, 11, 21)

    def test_call_parses(self):
        c = parse_option_symbol(
            symbol="-NVDA260116C500",
            description="CALL (NVDA) NVIDIA CORP JAN 16 26 $500 (100 SHS)",
        )
        assert c.option_type == "CALL"
        assert c.strike == Decimal("500")

    def test_non_option_symbol_returns_none(self):
        # Stock trades don't start with '-'
        assert parse_option_symbol("AAPL", "AAPL APPLE INC COMMON") is None


# ----------------------------------------------------------------------------
# _pick_position_for_underlying — direction + strike + expiration disambig.
# ----------------------------------------------------------------------------

@dataclass
class _Contract:
    symbol: str
    expiration: date
    strike: float
    option_type: str


@dataclass
class _Pos:
    id: int
    contract: _Contract


def _trade(action="BUY", price=95.0, when=None):
    return ParsedUnderlyingTrade(
        symbol="AAPL",
        trade_date=when or datetime(2026, 3, 20, 16, 0),
        action=action,
        quantity=100,
        price=Decimal(str(price)),
        amount=Decimal("-9500"),
        trade_type="ASSIGNMENT",
        linked_option_symbol=None,
    )


class TestUnderlyingLinker:
    def test_put_assignment_matches_put_position(self):
        e = date(2026, 3, 20)
        candidates = [
            _Pos(1, _Contract("AAPL", e, 95, "PUT")),
            _Pos(2, _Contract("AAPL", e, 95, "CALL")),
        ]
        picked = _pick_position_for_underlying(_trade(action="BUY"), candidates)
        assert picked.id == 1  # PUT, not CALL

    def test_call_assignment_matches_call_position(self):
        e = date(2026, 3, 20)
        candidates = [
            _Pos(1, _Contract("AAPL", e, 95, "PUT")),
            _Pos(2, _Contract("AAPL", e, 95, "CALL")),
        ]
        picked = _pick_position_for_underlying(_trade(action="SELL"), candidates)
        assert picked.id == 2

    def test_disambiguates_by_strike_when_multiple_same_direction(self):
        # Two short puts assigned at different strikes.
        e = date(2026, 3, 20)
        candidates = [
            _Pos(1, _Contract("AAPL", e, 90, "PUT")),
            _Pos(2, _Contract("AAPL", e, 95, "PUT")),
        ]
        # User got assigned at the $95 strike (trade price $95).
        picked = _pick_position_for_underlying(_trade(action="BUY", price=95), candidates)
        assert picked.id == 2

        # User got assigned at the $90 strike (trade price $90).
        picked = _pick_position_for_underlying(_trade(action="BUY", price=90), candidates)
        assert picked.id == 1

    def test_no_directional_match_returns_none(self):
        # User has only a CALL position but a BUY trade arrives (PUT assignment).
        e = date(2026, 3, 20)
        candidates = [_Pos(1, _Contract("AAPL", e, 100, "CALL"))]
        picked = _pick_position_for_underlying(_trade(action="BUY"), candidates)
        assert picked is None


# ----------------------------------------------------------------------------
# Grace period: regression — verify the constant is reasonable.
# ----------------------------------------------------------------------------

def test_grace_period_covers_weekend():
    # 5 days covers Friday expiration + weekend + a settlement day.
    assert AUTO_EXPIRY_GRACE_DAYS >= 3
    assert AUTO_EXPIRY_GRACE_DAYS <= 14  # Sanity: not unbounded
