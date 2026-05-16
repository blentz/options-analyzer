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
    """Initialize the database: apply Alembic migrations + tune SQLite.

    Migration application is the source of truth for schema; the old
    `create_all` shortcut left databases unable to upgrade across model
    changes (the README literally told users to delete options.db). With
    Alembic we can evolve the schema without data loss.

    PRAGMA tuning runs on every startup so it stays in effect:
      - WAL: one writer + many readers without blocking
      - busy_timeout: retry locked writes for 5s before erroring
      - foreign_keys: actually enforce them (off by default in SQLite)
    """
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)

    # Apply pending migrations. We call into Alembic via its Python API
    # so we don't shell out and so it uses the same async engine.
    from alembic import command
    from alembic.config import Config as _AlembicConfig
    from pathlib import Path as _Path
    cfg = _AlembicConfig(str(_Path(__file__).resolve().parent.parent / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", DATABASE_URL)
    # Alembic's upgrade is sync — run in a thread so we don't block the loop.
    import asyncio as _asyncio
    await _asyncio.to_thread(command.upgrade, cfg, "head")

    async with engine.begin() as conn:
        await conn.execute(text("PRAGMA journal_mode=WAL"))
        await conn.execute(text("PRAGMA busy_timeout=5000"))
        await conn.execute(text("PRAGMA foreign_keys=ON"))


async def get_db() -> AsyncSession:
    """Dependency for getting database sessions."""
    async with async_session() as session:
        yield session
