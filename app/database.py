from __future__ import annotations

from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel

from app.config import settings


def _to_async_sqlite_url(url: str) -> str:
    # Accept "sqlite:///./file.db" and convert to aiosqlite driver.
    if url.startswith("sqlite:///"):
        return url.replace("sqlite:///", "sqlite+aiosqlite:///", 1)
    if url.startswith("sqlite://"):
        return url.replace("sqlite://", "sqlite+aiosqlite://", 1)
    return url


ASYNC_DATABASE_URL = _to_async_sqlite_url(settings.database_url)

engine: AsyncEngine = create_async_engine(
    ASYNC_DATABASE_URL,
    echo=False,
    future=True,
)

AsyncSessionLocal = sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


# Tiny migration registry: add columns to existing tables if missing.
# Format: (table_name, column_name, column_type_sql)
_MIGRATIONS: list[tuple[str, str, str]] = [
    ("pulse_cards", "language", "VARCHAR(8)"),
]


async def _apply_simple_migrations(conn) -> None:
    for table, column, coltype in _MIGRATIONS:
        try:
            res = await conn.execute(text(f"PRAGMA table_info({table})"))
            cols = {row[1] for row in res.fetchall()}
            if column not in cols:
                await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}"))
        except Exception:
            # Best-effort; if the table doesn't exist yet create_all will handle it.
            pass


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
        await _apply_simple_migrations(conn)


@asynccontextmanager
async def get_session() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        yield session

