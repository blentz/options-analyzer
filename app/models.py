"""SQLAlchemy models for options trading data."""

from datetime import datetime, date
from decimal import Decimal
from typing import Optional
from sqlalchemy import String, Integer, Numeric, DateTime, Date, ForeignKey, Index, Boolean, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.database import Base


class OptionContract(Base):
    """Represents a unique options contract."""
    __tablename__ = "option_contracts"

    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(20), index=True)  # Underlying symbol
    expiration: Mapped[date] = mapped_column(Date)
    strike: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    option_type: Mapped[str] = mapped_column(String(4))  # CALL or PUT

    # Relationships
    trades: Mapped[list["OptionTrade"]] = relationship(back_populates="contract", cascade="all, delete-orphan")
    position: Mapped[Optional["OptionPosition"]] = relationship(back_populates="contract", uselist=False)

    __table_args__ = (
        Index('ix_contract_unique', 'symbol', 'expiration', 'strike', 'option_type', unique=True),
    )

    @property
    def contract_id(self) -> str:
        return f"{self.symbol} {self.expiration.strftime('%m/%d/%y')} ${self.strike} {self.option_type}"


class OptionTrade(Base):
    """Represents a single options transaction."""
    __tablename__ = "option_trades"

    id: Mapped[int] = mapped_column(primary_key=True)
    contract_id: Mapped[int] = mapped_column(ForeignKey("option_contracts.id"), index=True)
    trade_date: Mapped[datetime] = mapped_column(DateTime, index=True)
    settlement_date: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    action: Mapped[str] = mapped_column(String(100))  # SOLD OPENING, BOUGHT CLOSING, etc.
    quantity: Mapped[int] = mapped_column(Integer)  # Number of contracts
    price: Mapped[Decimal] = mapped_column(Numeric(10, 4))  # Premium per share
    commission: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0)
    fees: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))  # Net cash flow
    raw_symbol: Mapped[str] = mapped_column(String(50))  # Original Fidelity symbol

    # Relationships
    contract: Mapped["OptionContract"] = relationship(back_populates="trades")

    __table_args__ = (
        Index('ix_trade_date_contract', 'trade_date', 'contract_id'),
    )


class OptionPosition(Base):
    """Tracks aggregated position data for a contract."""
    __tablename__ = "option_positions"

    id: Mapped[int] = mapped_column(primary_key=True)
    contract_id: Mapped[int] = mapped_column(ForeignKey("option_contracts.id"), unique=True)

    # Position state
    is_closed: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    open_date: Mapped[datetime] = mapped_column(DateTime)
    close_date: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # Strategy classification
    strategy: Mapped[str] = mapped_column(String(20))  # SHORT PUT, LONG CALL, etc.
    outcome: Mapped[str] = mapped_column(String(20), default="OPEN")  # EXPIRED, ASSIGNED, CLOSED, OPEN

    # Financial metrics
    total_premium: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)
    total_commission: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0)
    total_fees: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0)
    net_pnl: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)

    # Trade counts
    num_contracts: Mapped[int] = mapped_column(Integer, default=0)

    # Underlying stock P&L from assignment/exercise
    underlying_pnl: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)
    total_pnl: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)  # net_pnl + underlying_pnl

    # Per-position implied-volatility override. NULL = use the live IV from
    # StockNear (or the default). Operators can pin a value here when they
    # disagree with the scraped IV (e.g., after a recent earnings event the
    # IV crush hasn't propagated yet, or they want to stress-test).
    volatility_override: Mapped[Optional[Decimal]] = mapped_column(Numeric(6, 4), nullable=True)

    # Audit timestamps — track when the row was first written and last
    # mutated. Particularly useful with the new auto-EXPIRED grace period:
    # operators can see exactly when a position's outcome was changed by
    # the importer vs. directly by a trade.
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
    )

    # Relationships
    contract: Mapped["OptionContract"] = relationship(back_populates="position")
    underlying_trades: Mapped[list["UnderlyingTrade"]] = relationship(back_populates="position", cascade="all, delete-orphan")

    @property
    def is_winner(self) -> bool:
        return self.total_pnl > 0


class UnderlyingTrade(Base):
    """Tracks underlying stock transactions related to options (assignments/exercises)."""
    __tablename__ = "underlying_trades"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Optional link to a specific assigned option position. NULL when the
    # stock movement is a manual buy/sell on an options-active ticker but
    # doesn't correspond to any single assignment — e.g., user sold half
    # the assigned lot manually, or rolled out of stock between covered
    # calls. Cycle detection (wheel_detection.py) walks ALL UnderlyingTrade
    # rows for the symbol regardless of this link.
    position_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("option_positions.id"), index=True, nullable=True
    )
    symbol: Mapped[str] = mapped_column(String(20), index=True)
    trade_date: Mapped[datetime] = mapped_column(DateTime, index=True)
    action: Mapped[str] = mapped_column(String(20))  # BUY, SELL
    quantity: Mapped[int] = mapped_column(Integer)  # Number of shares
    price: Mapped[Decimal] = mapped_column(Numeric(10, 4))  # Price per share
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))  # Net cash flow
    trade_type: Mapped[str] = mapped_column(String(20))  # ASSIGNMENT, EXERCISE, COVER

    # Optional back-reference. Populated only when position_id is set.
    position: Mapped[Optional["OptionPosition"]] = relationship(back_populates="underlying_trades")


class ImportLog(Base):
    """Tracks CSV import history to avoid duplicates."""
    __tablename__ = "import_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    filename: Mapped[str] = mapped_column(String(255))
    imported_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    records_imported: Mapped[int] = mapped_column(Integer, default=0)
    records_skipped: Mapped[int] = mapped_column(Integer, default=0)


class WheelCycle(Base):
    """One wheel-strategy cycle on a single symbol.

    A cycle starts when you sell a cash-secured put on a symbol you don't
    already own (or already have an active cycle for). It accumulates
    shares via put assignments, sheds them via covered-call assignments
    (or manual sales), and is considered CLOSED when shares_held drops
    back to zero AND every option position in the cycle has closed.

    A symbol can have multiple cycles over its lifetime (cycle A closes
    cleanly, weeks later you start cycle B with a new CSP). They're
    independent — P&L, win/loss, holding period are all per-cycle.

    The win-rate problem this solves: a profitable wheel ($200 put
    premium + $500 stock gain + $150 call premium = +$850) was previously
    stored as ONE LOSS (the put: +200 premium − 9500 buy = −9300) and
    ONE WIN (the call: +150 premium + 10000 sell = +10150). Aggregate
    summed right; the per-position view and the win rate were both wrong.
    """
    __tablename__ = "wheel_cycles"

    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(20), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime)
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(10), default="ACTIVE", index=True)  # ACTIVE | CLOSED

    # Cost basis tracking (running state for ACTIVE cycles, final value
    # is meaningless for CLOSED cycles since shares_held is always 0).
    shares_held: Mapped[int] = mapped_column(Integer, default=0)
    avg_cost_basis: Mapped[Decimal] = mapped_column(Numeric(12, 4), default=0)

    # Realized P&L components — populated by the detection service.
    options_pnl: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)
    stock_pnl: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)
    total_pnl: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)

    # Counts for quick UI display without joining members.
    num_puts: Mapped[int] = mapped_column(Integer, default=0)
    num_calls: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.current_timestamp())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
    )

    members: Mapped[list["WheelCycleMember"]] = relationship(
        back_populates="cycle", cascade="all, delete-orphan"
    )

    @property
    def is_winner(self) -> bool:
        return self.total_pnl > 0


class WheelCycleMember(Base):
    """Association: which OptionPositions participate in which WheelCycle.

    Unique on position_id — a single position can belong to at most one
    cycle. `sequence` is the chronological order within the cycle so the
    UI can render legs in the order they happened.
    """
    __tablename__ = "wheel_cycle_members"

    id: Mapped[int] = mapped_column(primary_key=True)
    cycle_id: Mapped[int] = mapped_column(ForeignKey("wheel_cycles.id"), index=True)
    position_id: Mapped[int] = mapped_column(
        ForeignKey("option_positions.id"), unique=True, index=True
    )
    role: Mapped[str] = mapped_column(String(4))  # CSP | CC
    sequence: Mapped[int] = mapped_column(Integer, default=0)

    cycle: Mapped["WheelCycle"] = relationship(back_populates="members")


class PositionGroup(Base):
    """Operator-defined grouping of OptionPositions into one strategy.

    The risk page's heuristic (same symbol + expiration + open-day) handles
    the obvious cases, but it both over-groups (two unrelated covered calls
    opened the same day) and under-groups (legs of one trade entered across
    days). PositionGroup lets the operator pin a definitive grouping that
    overrides the heuristic.

    When a position belongs to a manual group, the heuristic is ignored for
    that position. Members of explicit groups never get absorbed into a
    heuristic group of a different membership.
    """
    __tablename__ = "position_groups"

    id: Mapped[int] = mapped_column(primary_key=True)
    label: Mapped[str] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    members: Mapped[list["PositionGroupMember"]] = relationship(
        back_populates="group", cascade="all, delete-orphan"
    )


class PositionGroupMember(Base):
    """Association: which OptionPositions belong to which PositionGroup."""
    __tablename__ = "position_group_members"

    id: Mapped[int] = mapped_column(primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("position_groups.id"), index=True)
    position_id: Mapped[int] = mapped_column(
        ForeignKey("option_positions.id"), unique=True, index=True
    )

    group: Mapped["PositionGroup"] = relationship(back_populates="members")


class StockNearCache(Base):
    """Cache for StockNear API data to avoid excessive scraping."""
    __tablename__ = "stocknear_cache"

    id: Mapped[int] = mapped_column(primary_key=True)
    cache_key: Mapped[str] = mapped_column(String(255), unique=True, index=True)  # e.g., "options_overview:AAPL"
    data_type: Mapped[str] = mapped_column(String(50), index=True)  # options_overview, options_chain, stock_quote, etc.
    symbol: Mapped[str] = mapped_column(String(20), index=True)
    data_json: Mapped[str] = mapped_column(String)  # JSON serialized data
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)

    __table_args__ = (
        Index('ix_stocknear_cache_lookup', 'cache_key', 'expires_at'),
    )
