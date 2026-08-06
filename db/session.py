"""
Database session factory — async (for FastAPI/Celery) and sync (for scripts).
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager, contextmanager
from typing import AsyncIterator, Iterator

from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session, sessionmaker

_DB_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://localhost/autonomoushire",
)
_DB_URL_SYNC = _DB_URL.replace("+asyncpg", "+psycopg2").replace("postgresql+psycopg2", "postgresql")

# Async engine (FastAPI, Celery async tasks)
_async_engine = create_async_engine(
    _DB_URL,
    pool_size=5,
    max_overflow=10,
    echo=False,
)
AsyncSessionLocal = async_sessionmaker(_async_engine, expire_on_commit=False)

# Sync engine (migration scripts, data loader)
_sync_engine = create_engine(_DB_URL_SYNC, pool_size=3, max_overflow=5, echo=False)
SyncSessionLocal = sessionmaker(_sync_engine, expire_on_commit=False)


@asynccontextmanager
async def get_async_session() -> AsyncIterator[AsyncSession]:
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


@contextmanager
def get_sync_session() -> Iterator[Session]:
    with SyncSessionLocal() as session:
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise


async def create_tables() -> None:
    """Create all tables (idempotent — use Alembic for migrations in production)."""
    from db.models import Base
    async with _async_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    _ensure_columns_sync()   # ALTER-in any new columns create_all can't add to existing tables


def _ensure_columns_sync() -> None:
    """Lightweight forward-migration: create_all() creates missing TABLES but never adds columns to an
    existing table, so new columns must be ALTER-ed in. Dialect-aware + idempotent. Keep this list
    append-only; each entry is (table, column, sql-type)."""
    from sqlalchemy import text
    dialect = _sync_engine.dialect.name
    default = "false" if dialect == "postgresql" else "0"
    _adds = [
        ("applications", "referral_first", f"BOOLEAN NOT NULL DEFAULT {default}"),
        ("applications", "referral_done",  f"BOOLEAN NOT NULL DEFAULT {default}"),
    ]
    with _sync_engine.begin() as conn:
        for table, col, coldef in _adds:
            try:
                if dialect == "postgresql":
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {coldef}"))
                else:  # sqlite (tests) — no ADD COLUMN IF NOT EXISTS, so pre-check PRAGMA
                    cols = {r[1] for r in conn.execute(text(f"PRAGMA table_info({table})"))}
                    if col not in cols:
                        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {coldef}"))
            except Exception:
                pass   # never let a migration hiccup break startup — sync still upserts


def create_tables_sync() -> None:
    from db.models import Base
    Base.metadata.create_all(_sync_engine)
    _ensure_columns_sync()
