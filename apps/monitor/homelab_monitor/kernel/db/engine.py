"""Async SQLite engine factory with WAL/foreign-keys/busy-timeout pragmas."""

from __future__ import annotations

import os
from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

DEFAULT_DATABASE_URL = "sqlite+aiosqlite:///./data/homelab-monitor.db"
"""Default URL when ``HOMELAB_MONITOR_DB_URL`` is not set."""

_engine: AsyncEngine | None = None


def get_database_url() -> str:
    """Resolve the database URL from the env var, falling back to the default."""
    return os.environ.get("HOMELAB_MONITOR_DB_URL", DEFAULT_DATABASE_URL)


def _apply_sqlite_pragmas(dbapi_connection: Any, _connection_record: Any) -> None:  # noqa: ANN401
    """SQLAlchemy ``connect`` listener: enforce WAL + foreign keys + busy timeout.

    Runs against every new DBAPI connection (sync underlying of the async engine).
    Pragmas are connection-level in SQLite, so they MUST be applied here, not at
    engine-creation time.
    """
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")  # pragma: no cover
        cursor.execute("PRAGMA foreign_keys=ON")  # pragma: no cover
        cursor.execute("PRAGMA busy_timeout=5000")  # pragma: no cover
    finally:
        cursor.close()  # pragma: no cover


def get_engine(url: str | None = None) -> AsyncEngine:
    """Return the cached :class:`AsyncEngine`, creating it on first call.

    Pragma listener is wired against ``engine.sync_engine`` (the underlying sync
    engine of an async engine) — SQLAlchemy events fire on the sync layer.

    Pass an explicit ``url`` to bypass the cache (used by tests and by Alembic
    when running migrations against a temporary DB).
    """
    global _engine  # noqa: PLW0603
    if url is not None:
        engine = create_async_engine(url, future=True)
        event.listen(engine.sync_engine, "connect", _apply_sqlite_pragmas)
        return engine
    if _engine is None:
        _engine = create_async_engine(get_database_url(), future=True)
        event.listen(_engine.sync_engine, "connect", _apply_sqlite_pragmas)
    return _engine


async def dispose_engine() -> None:
    """Dispose of the cached engine (used at shutdown and between tests)."""
    global _engine  # noqa: PLW0603
    if _engine is not None:
        await _engine.dispose()
        _engine = None
