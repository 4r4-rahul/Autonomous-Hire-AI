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


def create_tables_sync() -> None:
    from db.models import Base
    Base.metadata.create_all(_sync_engine)
