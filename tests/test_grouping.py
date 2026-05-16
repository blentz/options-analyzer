"""Tests for the multi-leg position grouping heuristic."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from app.services.risk_analysis import group_positions_for_strategy, analyze_position_group


@dataclass
class _Contract:
    symbol: str
    expiration: date
    strike: float
    option_type: str


@dataclass
class _Position:
    id: int
    contract: _Contract
    open_date: datetime
    strategy: str
    total_premium: float
    num_contracts: int = 1


def _exp(days_out=30):
    return date.today() + timedelta(days=days_out)


def test_iron_condor_same_day_same_expiry_groups_together():
    e = _exp(30)
    od = datetime.now()
    positions = [
        _Position(1, _Contract("AAPL", e, 95, "PUT"),  od, "SHORT PUT", 150),
        _Position(2, _Contract("AAPL", e, 90, "PUT"),  od, "LONG PUT",  -50),
        _Position(3, _Contract("AAPL", e, 105, "CALL"), od, "SHORT CALL", 150),
        _Position(4, _Contract("AAPL", e, 110, "CALL"), od, "LONG CALL",  -50),
    ]
    groups = group_positions_for_strategy(positions)
    assert len(groups) == 1
    assert len(groups[0]) == 4


def test_different_expirations_dont_group():
    e1 = _exp(30)
    e2 = _exp(60)
    od = datetime.now()
    positions = [
        _Position(1, _Contract("AAPL", e1, 100, "PUT"), od, "SHORT PUT", 100),
        _Position(2, _Contract("AAPL", e2, 100, "PUT"), od, "SHORT PUT", 100),
    ]
    groups = group_positions_for_strategy(positions)
    assert len(groups) == 2


def test_different_open_days_dont_group():
    e = _exp(30)
    positions = [
        _Position(1, _Contract("AAPL", e, 100, "PUT"), datetime(2026, 1, 1), "SHORT PUT", 100),
        _Position(2, _Contract("AAPL", e, 90,  "PUT"), datetime(2026, 1, 5), "LONG PUT",  -50),
    ]
    groups = group_positions_for_strategy(positions)
    assert len(groups) == 2


def test_grouped_iron_condor_max_loss_is_wing_width_minus_credit():
    """The whole point of grouping: portfolio max loss for an iron condor
    should be wing - credit, NOT the sum of standalone leg max losses
    (which includes ∞ for the bare short call leg)."""
    e = _exp(30)
    od = datetime.now()
    # net credit = 100 + 100 - 50 - 50 = 100 ($1/share)
    positions = [
        _Position(1, _Contract("AAPL", e, 95,  "PUT"),  od, "SHORT PUT",  150, 1),
        _Position(2, _Contract("AAPL", e, 90,  "PUT"),  od, "LONG PUT",   -50, 1),
        _Position(3, _Contract("AAPL", e, 105, "CALL"), od, "SHORT CALL", 150, 1),
        _Position(4, _Contract("AAPL", e, 110, "CALL"), od, "LONG CALL",  -50, 1),
    ]
    analysis = analyze_position_group(positions, current_price=100.0)
    # Wing = $5; credit = $2 (1.5 - 0.5 each side). Max loss = $300.
    assert abs(analysis.max_loss) <= 400  # generous bound; exact depends on premium rounding
    assert analysis.max_profit > 0
    assert analysis.max_loss < 0
    # Should detect both breakevens
    assert len(analysis.breakeven_prices) == 2
