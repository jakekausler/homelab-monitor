"""Async DB fixtures used across DB and CLI tests.

We use a tempfile-backed SQLite DB (NOT ``:memory:``) so Alembic migrations and
test queries see the same database across multiple connections — ``aiosqlite``
gives each connection a fresh ``:memory:`` DB by default, which would defeat
the migration round-trip.
"""

from __future__ import annotations

import base64
import os
import tempfile
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine

from homelab_monitor.kernel.db.engine import dispose_engine, get_engine
from homelab_monitor.kernel.db.migrations import alembic_upgrade_head
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.secrets.repository import AsyncSecretsRepository

# Re-export the uvicorn fixture so tests can request it without importing
# from the helper module directly.
from ._uvicorn_fixture import (  # noqa: F401  -- pytest fixture re-export
    UvicornFixtureValue,  # pyright: ignore[reportUnusedImport]
    uvicorn_server,  # pyright: ignore[reportUnusedImport]
)

TEST_USERNAME = "testuser"
TEST_PASSWORD = "testpassword123"


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


@pytest_asyncio.fixture
async def authenticated_client(
    db_url: str, master_key: bytes, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[AsyncClient]:
    """Async httpx client with valid session cookie and lifespan.

    Creates a test user (constants TEST_USERNAME / TEST_PASSWORD), logs in,
    and yields an AsyncClient with the session cookie set. The CSRF cookie
    is also set and available via client.cookies.get("homelab_monitor_csrf").
    """
    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    monkeypatch.setenv("HOMELAB_MONITOR_MASTER_KEY", base64.b64encode(master_key).decode())
    monkeypatch.setenv("HOMELAB_MONITOR_HTTPS_ONLY_COOKIES", "false")
    monkeypatch.setenv("HOMELAB_MONITOR_ALERTMANAGER_URL", "disabled")

    from homelab_monitor.kernel.api.app import create_app  # noqa: PLC0415

    app = create_app(lifespan_enabled=True)
    async with app.router.lifespan_context(app):
        from homelab_monitor.kernel.auth.passwords import hash_password  # noqa: PLC0415

        await app.state.auth_repo.create_user(TEST_USERNAME, hash_password(TEST_PASSWORD, cost=4))
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/api/auth/login",
                json={"username": TEST_USERNAME, "password": TEST_PASSWORD},
            )
            assert resp.status_code == 200  # noqa: PLR2004
            yield client


@pytest_asyncio.fixture
async def api_token_client(
    db_url: str, master_key: bytes, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[AsyncClient]:
    """Async httpx client with valid API token (Bearer header).

    Creates a token with all common scopes and sets it as the default
    Authorization header for all requests.
    """
    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    monkeypatch.setenv("HOMELAB_MONITOR_MASTER_KEY", base64.b64encode(master_key).decode())
    monkeypatch.setenv("HOMELAB_MONITOR_ALERTMANAGER_URL", "disabled")

    from homelab_monitor.kernel.api.app import create_app  # noqa: PLC0415

    app = create_app(lifespan_enabled=True)
    async with app.router.lifespan_context(app):
        from homelab_monitor.kernel.auth.api_tokens import make_api_token  # noqa: PLC0415
        from homelab_monitor.kernel.auth.scopes import Scope  # noqa: PLC0415

        plaintext, _ = make_api_token(prefix="test")
        await app.state.auth_repo.create_api_token(
            name="test-token",
            scopes={Scope.HEARTBEAT_WRITE, Scope.ALERTS_INGEST_WRITE, Scope.READ_STATUS},
            plaintext_token=plaintext,
        )
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": f"Bearer {plaintext}"},
        ) as client:
            yield client
