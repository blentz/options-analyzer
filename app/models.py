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
    position_id: Mapped[int] = mapped_column(ForeignKey("option_positions.id"), index=True)
    symbol: Mapped[str] = mapped_column(String(20), index=True)
    trade_date: Mapped[datetime] = mapped_column(DateTime, index=True)
    action: Mapped[str] = mapped_column(String(20))  # BUY, SELL
    quantity: Mapped[int] = mapped_column(Integer)  # Number of shares
    price: Mapped[Decimal] = mapped_column(Numeric(10, 4))  # Price per share
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))  # Net cash flow
    trade_type: Mapped[str] = mapped_column(String(20))  # ASSIGNMENT, EXERCISE, COVER

    # Relationships
    position: Mapped["OptionPosition"] = relationship(back_populates="underlying_trades")


class ImportLog(Base):
    """Tracks CSV import history to avoid duplicates."""
    __tablename__ = "import_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    filename: Mapped[str] = mapped_column(String(255))
    imported_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    records_imported: Mapped[int] = mapped_column(Integer, default=0)
    records_skipped: Mapped[int] = mapped_column(Integer, default=0)


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
