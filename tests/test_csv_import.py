"""Tests for CSV import helpers (pure functions, no DB)."""

from datetime import datetime, date, timedelta
from dataclasses import dataclass
from decimal import Decimal

from app.services.csv_import import (
    parse_option_symbol,
    _pick_position_for_underlying,
    ParsedUnderlyingTrade,
    AUTO_EXPIRY_GRACE_DAYS,
)


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
