"""Database configuration and session management."""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase

from app.config import settings

DATABASE_PATH = settings.database_path_obj
DATABASE_URL = settings.database_url

engine = create_async_engine(DATABASE_URL, echo=False)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def init_db():
    """Initialize the database, creating all tables.

    Enables WAL journal mode and a small busy-timeout so concurrent CSV
    imports and StockNearCache writes don't throw `database is locked`
    when the risk page or cache janitor is also writing.
    """
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    async with engine.begin() as conn:
        # WAL allows one writer + many readers without blocking. The
        # default DELETE journal mode blocks all readers when anything
        # is writing — bad for our pattern of background cache writes.
        await conn.execute(text("PRAGMA journal_mode=WAL"))
        # If a write does have to wait, retry for up to 5s before erroring.
        await conn.execute(text("PRAGMA busy_timeout=5000"))
        # Enforce foreign key constraints (off by default in SQLite).
        await conn.execute(text("PRAGMA foreign_keys=ON"))
        await conn.run_sync(Base.metadata.create_all)


async def get_db() -> AsyncSession:
    """Dependency for getting database sessions."""
    async with async_session() as session:
        yield session
