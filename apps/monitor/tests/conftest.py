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
from typing import Never

import pytest
import pytest_asyncio
import structlog
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from prometheus_client import CollectorRegistry
from sqlalchemy.ext.asyncio import AsyncEngine

from homelab_monitor.kernel.alerts.repository import AlertRepository
from homelab_monitor.kernel.api.routers import logs as logs_router
from homelab_monitor.kernel.api.sse import SseBroker
from homelab_monitor.kernel.auth.rate_limit import InProcessLoginRateLimiter
from homelab_monitor.kernel.auth.repository import AuthRepository
from homelab_monitor.kernel.backup.service import BackupService
from homelab_monitor.kernel.cron.repository import CronRepo
from homelab_monitor.kernel.cron.run_repository import CronRunRepository
from homelab_monitor.kernel.db.engine import dispose_engine, get_engine
from homelab_monitor.kernel.db.migrations import alembic_upgrade_head
from homelab_monitor.kernel.db.repositories.compose_actions_repository import (
    ComposeActionsRepository,
)
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.db.time import utc_now_iso
from homelab_monitor.kernel.dispatch.channels.inproc_dashboard import (
    InprocDashboardChannel,
)
from homelab_monitor.kernel.dispatch.dispatcher import AlertDispatcher
from homelab_monitor.kernel.docker.build_sources_loader import BuildSourcesLoader
from homelab_monitor.kernel.docker.compose_action_runner import ComposeActionRunner
from homelab_monitor.kernel.docker.socket_client import DockerSocketClient
from homelab_monitor.kernel.heartbeat.repository import HeartbeatRepo
from homelab_monitor.kernel.logs.multiplex import MultiplexLogsWriter
from homelab_monitor.kernel.logs.services import ServicesCache
from homelab_monitor.kernel.logs.vl_writer import VictoriaLogsWriter
from homelab_monitor.kernel.metrics.multiplex import MultiplexMetricsWriter
from homelab_monitor.kernel.metrics.prometheus_writer import PrometheusRegistryWriter
from homelab_monitor.kernel.plugins.io import (
    InMemoryLogsWriter,
    MemoryRetainingMetricsWriter,
)
from homelab_monitor.kernel.plugins.loader import PluginLoader
from homelab_monitor.kernel.plugins.noop import NoopCollector
from homelab_monitor.kernel.scheduler.failure_budget import FailureBudget
from homelab_monitor.kernel.scheduler.scheduler import Scheduler, SchedulerConfig
from homelab_monitor.kernel.secrets.repository import AsyncSecretsRepository
from homelab_monitor.kernel.secrets.ttl_resolver import TtlCachingSecretsResolver
from homelab_monitor.plugins.collectors.builtin.log_stream_budget import LogStreamState
from homelab_monitor.plugins.discoverers.cron_discoverer import CronDiscoverer

from ._test_lifespan import wire_test_app_state

# Re-export the uvicorn fixture so tests can request it without importing
# from the helper module directly.
from ._uvicorn_fixture import (  # noqa: F401  -- pytest fixture re-export
    UvicornFixtureValue,  # pyright: ignore[reportUnusedImport]
    uvicorn_server,  # pyright: ignore[reportUnusedImport]
)

# Test HTTP isolation is provided by pytest-httpx (see _mock_vm_lifespan_tick); no stub needed.


def make_engine() -> AsyncEngine:
    """Return a fresh temp-file-backed async engine for migration tests."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)  # noqa: SIM115
    tmp.close()
    return get_engine(url=f"sqlite+aiosqlite:///{tmp.name}")


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


@pytest_asyncio.fixture(scope="session")
async def _shared_app() -> AsyncIterator[FastAPI]:  # pyright: ignore[reportUnusedFunction]
    """Construct + wire the app ONCE per xdist worker.

    Uses a lightweight test lifespan (no background machinery). Per-test DB and
    mutable-singleton isolation is provided by ``_per_test_db`` which swaps the
    DB-derived + accumulating-state objects each test.
    """
    # Persistent for the whole session: read at REQUEST time by the shared app.
    # HTTPS_ONLY_COOKIES is read per-request in _set_auth_cookies (auth.py:92);
    # if unset, login cookies are issued Secure=True and the http:// test client
    # discards them -> every subsequent request 401s. MASTER_KEY is needed for
    # the shared app's whole lifetime. Real-lifespan tests override BOTH via
    # monkeypatch.setenv, so leaving them set never leaks into those tests.
    _persistent_env = {
        "HOMELAB_MONITOR_MASTER_KEY": base64.b64encode(bytes(range(32))).decode(),
        "HOMELAB_MONITOR_HTTPS_ONLY_COOKIES": "false",
    }
    os.environ.update(_persistent_env)
    # Transient: consumed ONLY during create_app + wire_test_app_state (the shared
    # app's lifespan is disabled, so it never re-reads these). RESTORED afterward
    # so they do not leak into real-lifespan tests that DON'T set them themselves
    # (e.g. test_lifespan_requests_cron_discovery_on_startup reads
    # DISABLE_STARTUP_CRON_DISCOVERY via the real lifespan).
    _transient_env = {
        "HOMELAB_MONITOR_DOCKER_SOCKET": "/tmp/hm-test-nonexistent-docker.sock",
        "HOMELAB_MONITOR_DISABLE_STARTUP_CRON_DISCOVERY": "1",
        "HOMELAB_MONITOR_ALERTMANAGER_URL": "disabled",
    }
    _saved_env = {k: os.environ.get(k) for k in _transient_env}
    os.environ.update(_transient_env)
    os.environ.setdefault("HOMELAB_MONITOR_BCRYPT_COST", "4")

    from homelab_monitor.kernel.api.app import create_app  # noqa: PLC0415

    # A throwaway initial DB just to satisfy wire_test_app_state's engine build;
    # _per_test_db immediately swaps in a fresh per-test DB before any request.
    fd, raw = tempfile.mkstemp(prefix="hm-shared-app-", suffix=".db")
    os.close(fd)
    initial_db_path = Path(raw)
    initial_db_path.unlink(missing_ok=True)
    initial_db_url = f"sqlite+aiosqlite:///{initial_db_path}"

    app = create_app(lifespan_enabled=False)
    handles = await wire_test_app_state(app, initial_db_url, bytes(range(32)))
    # Restore the prior environ now that all eager env reads are done.
    for _k, _v in _saved_env.items():
        if _v is None:
            os.environ.pop(_k, None)
        else:
            os.environ[_k] = _v
    try:
        yield app
    finally:
        await handles.http_client.aclose()
        await handles.docker_socket_client.aclose()
        await handles.engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            (initial_db_path.parent / (initial_db_path.name + suffix)).unlink(missing_ok=True)


@pytest_asyncio.fixture
async def _per_test_db(  # noqa: PLR0915  # pyright: ignore[reportUnusedFunction]
    _shared_app: FastAPI, db_path: Path, master_key: bytes
) -> AsyncIterator[FastAPI]:
    """Swap a fresh DB + fresh mutable singletons onto the shared app per test.

    Re-establishes the FULL DB-derived + accumulating-state surface every test,
    so a prior test's delattr / set-None / mock injection cannot leak (some
    tests, e.g. test_api_crons_discover_now, delattr without restore).
    """
    # Clear structlog context to avoid cross-test logger state pollution.
    structlog.contextvars.clear_contextvars()

    db_url = f"sqlite+aiosqlite:///{db_path}"
    alembic_upgrade_head(db_url)
    engine = get_engine(url=db_url)  # explicit URL → fresh engine, not cached singleton

    state = _shared_app.state

    # ---- DB-derived objects (rebuild from the fresh engine) ----
    repo = SqliteRepository(engine)
    secrets_repo = AsyncSecretsRepository(repo, master_key)
    ttl_resolver = TtlCachingSecretsResolver(secrets_repo, ttl_seconds=60.0)
    await ttl_resolver.refresh_now()
    auth_repo = AuthRepository(repo)
    alert_repo = AlertRepository(repo)
    heartbeat_repo = HeartbeatRepo(repo)
    cron_repo = CronRepo(repo)
    cron_run_repo = CronRunRepository(repo)

    log = structlog.get_logger().bind(component="test_lifespan")
    broker = SseBroker(log)
    alert_dispatcher = AlertDispatcher(channels=[InprocDashboardChannel(broker)], log=log)
    failure_budget = FailureBudget(repo, log, alert_repo=alert_repo, dispatcher=alert_dispatcher)

    # ---- mutable non-DB singletons (fresh per test) ----
    login_rate_limiter = InProcessLoginRateLimiter()
    in_memory_metrics_writer = MemoryRetainingMetricsWriter()
    prom_registry = CollectorRegistry()
    prom_writer = PrometheusRegistryWriter(prom_registry)
    metrics_writer = MultiplexMetricsWriter([in_memory_metrics_writer, prom_writer])
    in_memory_logs_writer = InMemoryLogsWriter()
    vl_writer = VictoriaLogsWriter(
        vl_url="http://victorialogs-disabled.invalid:9428",
        http_client=state.http_client,  # reuse session http client; never flushed
    )
    logs_writer = MultiplexLogsWriter([in_memory_logs_writer, vl_writer])
    log_stream_state: LogStreamState = {}

    # Reset module-level services cache (fresh per test).
    logs_router._services_cache = ServicesCache()  # pyright: ignore[reportPrivateUsage]

    # ---- plugin loader (fresh; re-register minimal set + persist to fresh DB) ----
    loader = PluginLoader(log=log)
    loader.register(NoopCollector, {"name": "noop", "interval_seconds": 60})
    loader.register(
        CronDiscoverer,
        {
            "name": "cron-discoverer",
            "interval_seconds": int(CronDiscoverer.interval.total_seconds()),
            "timeout_seconds": int(CronDiscoverer.timeout.total_seconds()),
        },
    )
    await loader.persist_to_db(repo)
    loaded = loader.load_all()

    def ctx_factory(c: object) -> Never:  # pragma: no cover -- scheduler never started
        raise RuntimeError("ctx_factory must not run in test lifespan")

    scheduler = Scheduler(
        loaded,
        ctx_factory,
        metrics_writer,
        SchedulerConfig(event_sink=broker),
        failure_budget=failure_budget,
        alert_repo=alert_repo,
        alert_dispatcher=alert_dispatcher,
    )
    cron_discoverer_instance = None
    for lc in loaded:
        c = lc.collector
        if isinstance(c, CronDiscoverer):
            c.cron_repo = cron_repo
            cron_discoverer_instance = c

    # ---- docker socket client + compose runner (fresh; nonexistent socket) ----
    socket_client = DockerSocketClient(socket_path="/tmp/hm-test-nonexistent-docker.sock", log=log)
    build_sources_loader = BuildSourcesLoader(
        config_path=Path("/tmp/hm-test-build-sources-nonexistent.yaml"), log=log
    )
    compose_action_runner = ComposeActionRunner(
        repo=repo,
        actions_repo=ComposeActionsRepository(repo),
        build_sources_loader=build_sources_loader,
        socket_client=socket_client,
        prom_registry=prom_registry,
        log=log,
    )

    # ---- backup service (fresh; bound to per-test DB path) ----
    backup_service = BackupService(
        db_path=db_path,
        vm_url=os.environ.get("HOMELAB_MONITOR_VM_URL", "http://victoriametrics:8428"),
        vm_data_dir=Path("/tmp/hm-test-vm-data"),
        backup_root=Path("/tmp/hm-test-backup"),
        http_client=state.http_client,
        db=repo,
    )

    # ================= SWAP / RESET the COMPLETE surface =================
    # DB-derived:
    state.repo = repo
    state.secrets_repo = secrets_repo
    state.ttl_resolver = ttl_resolver
    state.auth_repo = auth_repo
    state.alert_repo = alert_repo
    state.alert_dispatcher = alert_dispatcher
    state.heartbeat_repo = heartbeat_repo
    state.cron_repo = cron_repo
    state.cron_run_repo = cron_run_repo
    state.failure_budget = failure_budget
    state.backup_service = backup_service
    state.loader = loader
    state.scheduler = scheduler
    state.cron_discoverer = cron_discoverer_instance
    state.compose_action_runner = compose_action_runner
    state.build_sources_loader = build_sources_loader
    state.docker_socket_client = socket_client
    # Mutable accumulating singletons (reset to fresh instances):
    state.login_rate_limiter = login_rate_limiter
    state.broker = broker
    state.in_memory_metrics_writer = in_memory_metrics_writer
    state.prom_registry = prom_registry
    state.metrics_writer = metrics_writer
    state.in_memory_logs_writer = in_memory_logs_writer
    state.vl_writer = vl_writer
    state.logs_writer = logs_writer
    state.log_stream_state = log_stream_state
    # Re-establish collector-instance attrs that tests may delattr/set-None:
    state.docker_discoverer = None
    state.probe_supervisor = None
    state.image_update_collector = None
    state.local_build_update_collector = None
    state.override_loader = None
    state.image_events_task = None
    state.cron_events_token = None
    state.degraded_collectors = []
    # started_at / master_key are stable across tests; re-set for full parity:
    state.started_at = utc_now_iso()
    state.master_key = master_key
    # http_client stays the SESSION client (state.http_client unchanged).
    # ====================================================================

    try:
        yield _shared_app
    finally:
        await engine.dispose()
        # NOTE: do NOT aclose state.http_client here — it's session-scoped.
        # vl_writer/socket_client created here never opened connections
        # (no flusher, nonexistent socket), so no aclose needed; GC reclaims.


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
    _shared_app: FastAPI, _per_test_db: FastAPI
) -> AsyncIterator[AsyncClient]:
    """Async httpx client with valid session cookie (shared app, per-test DB)."""
    from homelab_monitor.kernel.auth.passwords import hash_password  # noqa: PLC0415

    await _shared_app.state.auth_repo.create_user(
        TEST_USERNAME, hash_password(TEST_PASSWORD, cost=4)
    )
    async with AsyncClient(
        transport=ASGITransport(app=_shared_app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/auth/login",
            json={"username": TEST_USERNAME, "password": TEST_PASSWORD},
        )
        assert resp.status_code == 200  # noqa: PLR2004
        client.app = _shared_app  # type: ignore[attr-defined]
        yield client


@pytest_asyncio.fixture
async def api_token_client(
    _shared_app: FastAPI, _per_test_db: FastAPI
) -> AsyncIterator[AsyncClient]:
    """Async httpx client with valid API token (shared app, per-test DB)."""
    from homelab_monitor.kernel.auth.api_tokens import make_api_token  # noqa: PLC0415
    from homelab_monitor.kernel.auth.scopes import Scope  # noqa: PLC0415

    plaintext, _ = make_api_token(prefix="test")
    await _shared_app.state.auth_repo.create_api_token(
        name="test-token",
        scopes={Scope.HEARTBEAT_WRITE, Scope.ALERTS_INGEST_WRITE, Scope.READ_STATUS},
        plaintext_token=plaintext,
    )
    async with AsyncClient(
        transport=ASGITransport(app=_shared_app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {plaintext}"},
    ) as client:
        client.app = _shared_app  # type: ignore[attr-defined]
        yield client


@pytest_asyncio.fixture
async def unauthenticated_client(
    _shared_app: FastAPI, _per_test_db: FastAPI
) -> AsyncIterator[AsyncClient]:
    """Async httpx client with no authentication (shared app, per-test DB)."""
    async with AsyncClient(
        transport=ASGITransport(app=_shared_app), base_url="http://test"
    ) as client:
        client.app = _shared_app  # type: ignore[attr-defined]
        yield client
