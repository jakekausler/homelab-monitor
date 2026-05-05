"""Async DB fixtures used across DB and CLI tests.

We use a tempfile-backed SQLite DB (NOT ``:memory:``) so Alembic migrations and
test queries see the same database across multiple connections — ``aiosqlite``
gives each connection a fresh ``:memory:`` DB by default, which would defeat
the migration round-trip.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine

from homelab_monitor.kernel.db.engine import dispose_engine, get_engine
from homelab_monitor.kernel.db.migrations import alembic_upgrade_head
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.secrets.repository import AsyncSecretsRepository


@pytest.fixture
def db_path() -> Iterator[Path]:
    """Yield a fresh temp DB file path; remove the file (and -wal/-shm) afterwards."""
    fd, raw = tempfile.mkstemp(prefix="hm-test-", suffix=".db")
    os.close(fd)
    path = Path(raw)
    path.unlink(missing_ok=True)  # let SQLite create it fresh
    try:
        yield path
    finally:
        for suffix in ("", "-wal", "-shm"):
            (path.parent / (path.name + suffix)).unlink(missing_ok=True)


@pytest.fixture
def db_url(db_path: Path) -> str:
    """Return a ``sqlite+aiosqlite`` URL pointing at ``db_path``."""
    return f"sqlite+aiosqlite:///{db_path}"


@pytest.fixture
def db_url_env(db_url: str, monkeypatch: pytest.MonkeyPatch) -> str:
    """Set ``HOMELAB_MONITOR_DB_URL`` for the duration of the test and return the URL."""
    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    return db_url


@pytest_asyncio.fixture
async def db_engine(db_url: str) -> AsyncIterator[AsyncEngine]:
    """Async engine pointed at a freshly migrated temp DB.

    Note: bypasses ``run_migrations`` / the ``HOMELAB_MONITOR_AUTO_MIGRATE``
    gate by calling ``alembic_upgrade_head`` directly — that gate is tested
    separately in ``test_db_migrations.py``.
    """
    alembic_upgrade_head(db_url)
    engine = get_engine(url=db_url)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def repo(db_engine: AsyncEngine) -> SqliteRepository:
    """Repository facade bound to the migrated test engine."""
    return SqliteRepository(engine=db_engine)


@pytest_asyncio.fixture(autouse=True)
async def _reset_engine_singleton() -> AsyncIterator[None]:  # pyright: ignore[reportUnusedFunction]
    """Ensure tests do not leak module-level engine state across each other.

    Invoked by pytest's autouse collector, not by direct call — hence the
    leading underscore and the pyright suppression.
    """
    yield
    await dispose_engine()


@pytest.fixture
def master_key() -> bytes:
    """Fixed 32-byte test key — deterministic, easy to reason about in failures."""
    return bytes(range(32))


@pytest_asyncio.fixture
async def secrets_repo(db_engine: AsyncEngine, master_key: bytes) -> AsyncSecretsRepository:
    """``AsyncSecretsRepository`` bound to the migrated test DB + the fixture key."""
    return AsyncSecretsRepository(SqliteRepository(engine=db_engine), master_key)
