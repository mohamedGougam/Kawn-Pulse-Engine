from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel

from app.config import settings

logger = logging.getLogger("kawn.db")


def _to_async_url(url: str) -> str:
    # Accept "sqlite:///./file.db" and convert to aiosqlite driver.
    if url.startswith("sqlite:///"):
        return url.replace("sqlite:///", "sqlite+aiosqlite:///", 1)
    if url.startswith("sqlite://"):
        return url.replace("sqlite://", "sqlite+aiosqlite://", 1)
    # Accept a plain "postgresql://..." (e.g. copy-pasted from Neon/Render)
    # and upgrade it to the async asyncpg driver.
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+asyncpg://", 1)
    return url


ASYNC_DATABASE_URL = _to_async_url(settings.database_url)
IS_SQLITE = ASYNC_DATABASE_URL.startswith("sqlite")

_connect_args: dict = {}
_engine_kwargs: dict = {}
if not IS_SQLITE:
    # asyncpg doesn't accept libpq-only query params that Neon / most managed
    # Postgres hosts put in their default connection strings — strip them and
    # translate the ones that matter into connect_args instead.
    # - sslmode=require -> ssl=True
    # - channel_binding=require -> asyncpg has no equivalent option and
    #   raises "invalid connection option 'channel_binding'" on connect if
    #   it's left in the DSN, so it's simply dropped (ssl=True already
    #   covers the encrypted-transport requirement it's paired with).
    if "sslmode=" in ASYNC_DATABASE_URL or "channel_binding=" in ASYNC_DATABASE_URL:
        from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

        parts = urlsplit(ASYNC_DATABASE_URL)
        query = dict(parse_qsl(parts.query))
        query.pop("sslmode", None)
        query.pop("channel_binding", None)
        ASYNC_DATABASE_URL = urlunsplit(parts._replace(query=urlencode(query)))
        _connect_args["ssl"] = True

    # Free/serverless Postgres (Neon, Render's own free tier, etc.) suspends
    # or drops idle connections after a period of inactivity — which on a
    # free-tier web dyno that itself spins down after ~15 min idle, easily
    # happens between one day's traffic and the next. Without these, the
    # pool hands out a connection object that *looks* fine but whose
    # underlying socket is already dead, the first query on it raises
    # unhandled (routes_topics._session_dep has no try/except around session
    # acquisition), and that surfaces to the caller as a plain 500 — same
    # symptom as the original blank-DATABASE_URL crash, different cause.
    # pool_pre_ping issues a cheap "SELECT 1" before handing out a pooled
    # connection and transparently reconnects if it's dead; pool_recycle
    # proactively retires connections before they get old enough to be
    # server-side-closed in the first place. Neither applies to sqlite,
    # which has no server-side idle timeout to guard against.
    _engine_kwargs["pool_pre_ping"] = True
    _engine_kwargs["pool_recycle"] = 1800

engine: AsyncEngine = create_async_engine(
    ASYNC_DATABASE_URL,
    echo=False,
    future=True,
    connect_args=_connect_args,
    **_engine_kwargs,
)

AsyncSessionLocal = sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


# Tiny migration registry: add columns to existing tables if missing.
# Format: (table_name, column_name, sqlite_column_type_sql, postgres_column_type_sql)
# SQLite is dynamically typed so "DATETIME" is accepted as a loose alias
# there, but Postgres has no DATETIME type — it needs the real TIMESTAMP
# type or the ALTER TABLE fails outright, so the two dialects get separate
# type strings rather than sharing one.
_MIGRATIONS: list[tuple[str, str, str, str]] = [
    ("pulse_cards", "language", "VARCHAR(8)", "VARCHAR(8)"),
    ("topics", "search_count", "INTEGER DEFAULT 0", "INTEGER DEFAULT 0"),
    ("topics", "last_searched_at", "DATETIME", "TIMESTAMP"),
]


def _apply_simple_migrations_sync(sync_conn) -> None:
    # create_all() above only creates tables that don't exist yet — it never
    # alters a table that's already there. That's fine for a brand-new
    # database, but any Postgres (Neon) instance stood up before these
    # columns existed already has a `topics` table without them, and
    # create_all silently leaves it that way. Previously this function
    # returned immediately on Postgres on that (incorrect) assumption, which
    # meant record_search / list_by_search_interest / the scheduler's
    # priority scoring would raise "column does not exist" the first time
    # they ran against such a database. Postgres supports
    # "ADD COLUMN IF NOT EXISTS" natively, so the same idempotent-migration
    # approach SQLite uses below works there too — just with real ALTER
    # TABLE syntax instead of PRAGMA table_info.
    if not IS_SQLITE:
        for table, column, _sqlite_type, pg_type in _MIGRATIONS:
            try:
                sync_conn.exec_driver_sql(
                    f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {pg_type}"
                )
            except Exception:
                logger.warning("postgres migration failed for %s.%s", table, column, exc_info=True)
        return

    for table, column, sqlite_type, _pg_type in _MIGRATIONS:
        try:
            res = sync_conn.exec_driver_sql(f"PRAGMA table_info({table})")
            cols = {row[1] for row in res.fetchall()}
        except Exception:
            continue

        if column in cols:
            continue

        try:
            sync_conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {column} {sqlite_type}")
        except Exception:
            logger.warning("sqlite migration failed for %s.%s", table, column, exc_info=True)


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
        await conn.run_sync(_apply_simple_migrations_sync)


@asynccontextmanager
async def get_session() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        yield session

