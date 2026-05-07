"""In-process uvicorn server fixture for streaming-response tests.

WHY: ``httpx.ASGITransport`` buffers streaming responses (it accumulates
``http.response.body`` chunks and only returns control once the ASGI app
finishes). SSE tests must verify that the server flushes chunks to the
socket as soon as the broker publishes — this requires a real HTTP server
on a real socket.

Approach:
- Spawn uvicorn on an ephemeral port in a background thread (its own
  ``asyncio`` event loop).
- Each call to the fixture gets a fresh temp DB; we run migrations and
  create a test user out-of-band on the test loop, then start uvicorn
  pointing at the same DB URL. uvicorn's lifespan re-runs migrations
  (idempotent) and bootstraps the scheduler/broker on uvicorn's loop —
  so SSE delivery and tick publishing happen on the same loop, which is
  what the tests verify.
- The fixture yields a ``(base_url, username, password)`` tuple so each
  test can log in via ``POST /api/auth/login`` over the real socket.

Why not use ``app_bootstrapped``: that fixture already runs the lifespan
on the test loop. Passing the same app to uvicorn (with ``lifespan="on"``)
would double-bootstrap (overwrite ``app.state``, start a second scheduler).
Passing it with ``lifespan="off"`` would leave the scheduler on the test
loop while uvicorn served requests on its own loop — the SSE broker
wouldn't see scheduler ticks. Building a fresh app inside uvicorn and
running its lifespan on uvicorn's loop is the only configuration that
keeps the scheduler/broker/SSE-handler chain on one loop.
"""

from __future__ import annotations

import asyncio
import base64
import os
import socket
import tempfile
import threading
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import pytest
import pytest_asyncio
import uvicorn
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from homelab_monitor.kernel.api.app import create_app
from homelab_monitor.kernel.auth.passwords import hash_password
from homelab_monitor.kernel.auth.repository import AuthRepository
from homelab_monitor.kernel.db.migrations import alembic_upgrade_head
from homelab_monitor.kernel.db.repository import SqliteRepository

UVICORN_TEST_USERNAME = "ssetestuser"
UVICORN_TEST_PASSWORD = "ssetestpassword123"


@dataclass(frozen=True)
class UvicornFixtureValue:
    """Tuple-like result of the ``uvicorn_server`` fixture."""

    base_url: str
    username: str
    password: str


def _free_port() -> int:
    """Bind to port 0 to let the kernel pick a free TCP port, then close."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _bootstrap_test_user(db_url: str) -> None:
    """Run migrations and create a single test user out-of-band.

    Runs on the test event loop, before uvicorn starts. Once committed,
    uvicorn's lifespan (which re-runs migrations idempotently) will see
    the user via its own engine.
    """
    alembic_upgrade_head(db_url)
    engine: AsyncEngine = create_async_engine(db_url, future=True)
    try:
        repo = SqliteRepository(engine)
        auth_repo = AuthRepository(repo)
        await auth_repo.create_user(
            UVICORN_TEST_USERNAME,
            hash_password(UVICORN_TEST_PASSWORD, cost=4),
        )
    finally:
        await engine.dispose()


class UvicornTestServer:
    """Run uvicorn on an ephemeral port in a background thread.

    The ``serve()`` coroutine runs on a fresh event loop owned by this
    server's thread; the test's event loop is unaffected. ``start()`` and
    ``stop()`` are awaitable from the test loop and poll for status flags
    set by uvicorn on its own loop.
    """

    _STARTUP_TIMEOUT_S = 10.0
    _SHUTDOWN_TIMEOUT_S = 10.0
    _POLL_INTERVAL_S = 0.05

    def __init__(self, port: int, db_url: str, master_key: bytes) -> None:
        # We build the FastAPI app inside __init__ so the test loop never
        # touches the lifespan; uvicorn's loop will. Env vars are read by
        # the lifespan when uvicorn starts the app.
        self._db_url = db_url
        self._master_key = master_key
        self._app = create_app(lifespan_enabled=True)
        self._config = uvicorn.Config(
            self._app,
            host="127.0.0.1",
            port=port,
            log_level="error",
            lifespan="on",
            access_log=False,
        )
        self._server = uvicorn.Server(self._config)
        self._thread: threading.Thread | None = None
        self.url = f"http://127.0.0.1:{port}"

    def _run(self) -> None:  # pragma: no cover -- runs in worker thread
        # Each thread needs its own event loop. ``Server.serve()`` is the
        # documented entry point that, when invoked off the main thread,
        # skips installing signal handlers (which would crash here).
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._server.serve())
        finally:
            loop.close()

    async def start(self) -> None:
        """Start uvicorn in a daemon thread; await ``server.started`` flag."""
        # Env vars must be set BEFORE the lifespan reads them. The fixture
        # uses monkeypatch.setenv before constructing UvicornTestServer; we
        # do not re-set them here.
        self._thread = threading.Thread(
            target=self._run,
            name="uvicorn-test-server",
            daemon=True,
        )
        self._thread.start()

        deadline = self._STARTUP_TIMEOUT_S / self._POLL_INTERVAL_S
        for _ in range(int(deadline)):
            if self._server.started:
                return
            if not self._thread.is_alive():
                msg = "uvicorn thread died during startup"
                raise RuntimeError(msg)
            await asyncio.sleep(self._POLL_INTERVAL_S)
        msg = f"uvicorn did not start within {self._STARTUP_TIMEOUT_S}s"
        raise RuntimeError(msg)

    async def stop(self) -> None:
        """Signal uvicorn to exit and join the worker thread."""
        self._server.should_exit = True
        thread = self._thread
        if thread is None:
            return
        deadline = self._SHUTDOWN_TIMEOUT_S / self._POLL_INTERVAL_S
        for _ in range(int(deadline)):
            if not thread.is_alive():
                return
            await asyncio.sleep(self._POLL_INTERVAL_S)
        msg = f"uvicorn did not stop within {self._SHUTDOWN_TIMEOUT_S}s"
        raise RuntimeError(msg)


@pytest_asyncio.fixture
async def uvicorn_server(
    master_key: bytes,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[UvicornFixtureValue]:
    """Run a real uvicorn server on a free port for streaming-response tests.

    Yields ``UvicornFixtureValue(base_url, username, password)``. The test
    must POST to ``{base_url}/api/auth/login`` with the username/password
    to obtain a session cookie before subscribing to ``/api/events``.

    Each invocation:
    - Creates a private temp DB (NOT shared with the ``db_url`` fixture)
      to keep uvicorn's lifespan teardown isolated from the autouse
      ``_reset_engine_singleton``.
    - Runs migrations and creates a single test user on the test loop.
    - Starts uvicorn (which runs ITS OWN lifespan, hence its own engine,
      scheduler, broker — all on uvicorn's loop). Because migrations are
      idempotent, re-running them under uvicorn's lifespan is a no-op.
    - On teardown, sets ``should_exit`` and joins the thread.

    Set ``HOMELAB_MONITOR_PLUGINS_DIR=/dev/null`` so uvicorn's lifespan
    doesn't try to load subprocess plugins (which spawn forkserver
    children — unnecessary cost and flakiness for SSE tests).

    NOTE: monkeypatch.setenv mutates os.environ which is process-global.
    Running this fixture under pytest-xdist (parallel workers) is safe ONLY
    because each worker is a separate process. Tests within the same worker
    that overlap with this fixture must not depend on the affected env vars
    mid-test.
    """
    # Private temp DB — do NOT use the shared db_url fixture.
    fd, raw = tempfile.mkstemp(prefix="hm-uvicorn-", suffix=".db")
    os.close(fd)
    db_path = Path(raw)
    db_path.unlink(missing_ok=True)
    db_url = f"sqlite+aiosqlite:///{db_path}"

    # Set env vars BEFORE constructing the server (the lifespan reads them
    # from the environment when uvicorn starts the app on its loop).
    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    monkeypatch.setenv(
        "HOMELAB_MONITOR_MASTER_KEY",
        base64.b64encode(master_key).decode(),
    )
    monkeypatch.setenv("HOMELAB_MONITOR_HTTPS_ONLY_COOKIES", "false")
    # bcrypt cost 4 is intentional for test speed (~50x faster than the
    # production default of 12). The test DB lives in a tempdir and is
    # unlinked post-test; the trivially-weak hash is never persisted.
    monkeypatch.setenv("HOMELAB_MONITOR_BCRYPT_COST", "4")
    monkeypatch.setenv("HOMELAB_MONITOR_AUTO_MIGRATE", "1")
    # Skip subprocess plugin loading: pointing at a non-directory makes the
    # lifespan log ``subprocess_plugins_skipped`` instead of forking. The
    # built-in ``noop`` collector is registered unconditionally, which is
    # all the SSE tests need to see scheduler.tick events.
    monkeypatch.setenv("HOMELAB_MONITOR_PLUGINS_DIR", "/dev/null")

    # Bootstrap the test user out-of-band before uvicorn starts.
    await _bootstrap_test_user(db_url)

    port = _free_port()
    server = UvicornTestServer(port=port, db_url=db_url, master_key=master_key)
    await server.start()
    try:
        yield UvicornFixtureValue(
            base_url=server.url,
            username=UVICORN_TEST_USERNAME,
            password=UVICORN_TEST_PASSWORD,
        )
    finally:
        await server.stop()
        # Brief grace for aiosqlite's WAL flush after server.stop() returns.
        # SQLite WAL files may still be written briefly after the lifespan
        # teardown; missing_ok=True handles already-cleaned files but a
        # short sleep prevents flaky cleanup races on slower filesystems.
        await asyncio.sleep(0.05)
        for suffix in ("", "-wal", "-shm"):
            (db_path.parent / (db_path.name + suffix)).unlink(missing_ok=True)
