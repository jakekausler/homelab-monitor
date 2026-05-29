"""Lightweight test lifespan wiring.

Builds the same ``app.state.*`` objects the routes read, but starts NO
background machinery (no scheduler tick loops, no VL flusher, no events loops,
no render-on-boot, no immediate-run). Used by the session-scoped ``_shared_app``
fixture in conftest.py. See spec-bootonce.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Never

import httpx
import structlog
from fastapi import FastAPI
from prometheus_client import CollectorRegistry
from sqlalchemy.ext.asyncio import AsyncEngine
from structlog.stdlib import BoundLogger

from homelab_monitor.kernel.alerts.repository import AlertRepository
from homelab_monitor.kernel.api.sse import SseBroker
from homelab_monitor.kernel.auth.rate_limit import InProcessLoginRateLimiter
from homelab_monitor.kernel.auth.repository import AuthRepository
from homelab_monitor.kernel.backup.service import BackupService
from homelab_monitor.kernel.cron.repository import CronRepo
from homelab_monitor.kernel.cron.run_repository import CronRunRepository
from homelab_monitor.kernel.db.engine import get_engine
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
from homelab_monitor.kernel.logging import configure_logging
from homelab_monitor.kernel.logs.multiplex import MultiplexLogsWriter
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

# A nonexistent socket path so DockerSocketClient never actually connects.
_NONEXISTENT_SOCKET = "/tmp/hm-test-nonexistent-docker.sock"
# A dummy VL URL — VictoriaLogsWriter is constructed but its flusher never runs,
# so nothing is ever POSTed. ingest() just enqueues into a bounded in-mem queue.
_DUMMY_VL_URL = "http://victorialogs-disabled.invalid:9428"


@dataclass
class TestAppHandles:
    """Handles the per-test swap + final session cleanup need."""

    engine: AsyncEngine
    db_url: str
    log: BoundLogger
    http_client: httpx.AsyncClient
    docker_socket_client: DockerSocketClient
    vl_writer: VictoriaLogsWriter


def _register_cron_discoverer_only(loader: PluginLoader) -> None:
    """Register the minimal collector set routes/scheduler reference.

    We register NoopCollector (always succeeds) so collectors endpoints have at
    least one entry, plus CronDiscoverer so ``cron_discoverer`` can be wired and
    /api/crons routes find it. We deliberately AVOID registering the full
    production collector set (docker/probes/image-update) — those pull host
    deps and are not needed for request handling in tests; routes that reference
    those collector instances exercise them via direct app.state injection in
    the consuming tests (see test_api_docker_*).
    """
    loader.register(NoopCollector, {"name": "noop", "interval_seconds": 60})
    loader.register(
        CronDiscoverer,
        {
            "name": "cron-discoverer",
            "interval_seconds": int(CronDiscoverer.interval.total_seconds()),
            "timeout_seconds": int(CronDiscoverer.timeout.total_seconds()),
        },
    )


async def wire_test_app_state(  # noqa: PLR0915
    app: FastAPI,
    db_url: str,
    master_key: bytes,
) -> TestAppHandles:
    """Assign every ``app.state.*`` the routes read; start NO background tasks.

    Idempotent in the sense that calling it on a fresh FastAPI sets the full
    37-attr surface. The per-test fixture calls a narrower swap (see conftest)
    for DB-derived + mutable singletons each test.
    """
    # Configure logging (matches the real lifespan; required for caplog integration).
    configure_logging()
    log: BoundLogger = structlog.get_logger().bind(component="test_lifespan")

    # --- engine + migrations (explicit URL bypasses cached singleton) ---
    alembic_upgrade_head(db_url)
    engine = get_engine(url=db_url)

    # --- repos + secrets ---
    repo = SqliteRepository(engine)
    secrets_repo = AsyncSecretsRepository(repo, master_key)
    ttl_resolver = TtlCachingSecretsResolver(secrets_repo, ttl_seconds=60.0, log=log)
    await ttl_resolver.refresh_now()  # ONE synchronous refresh; NO refresh_loop task.

    auth_repo = AuthRepository(repo)
    login_rate_limiter = InProcessLoginRateLimiter()

    # --- plugin loader (register only; NO load_subprocess_plugins, NO persist bg) ---
    loader = PluginLoader(log=log)
    _register_cron_discoverer_only(loader)
    await loader.persist_to_db(repo)  # cheap; collectors endpoint reads persisted rows

    # --- resources ---
    http_client = httpx.AsyncClient(
        limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
        timeout=httpx.Timeout(10.0, connect=5.0),
    )
    in_memory_metrics_writer = MemoryRetainingMetricsWriter()
    prom_registry = CollectorRegistry()
    prom_writer = PrometheusRegistryWriter(prom_registry)
    metrics_writer = MultiplexMetricsWriter([in_memory_metrics_writer, prom_writer])
    in_memory_logs_writer = InMemoryLogsWriter()
    vl_writer = VictoriaLogsWriter(vl_url=_DUMMY_VL_URL, http_client=http_client)
    # NOTE: NO asyncio.create_task(vl_writer.run_flusher()).
    logs_writer = MultiplexLogsWriter([in_memory_logs_writer, vl_writer])
    log_stream_state: LogStreamState = {}

    broker = SseBroker(log)
    alert_repo = AlertRepository(repo)
    heartbeat_repo = HeartbeatRepo(repo)
    cron_repo = CronRepo(repo)
    cron_run_repo = CronRunRepository(repo)
    alert_dispatcher = AlertDispatcher(channels=[InprocDashboardChannel(broker)], log=log)
    failure_budget = FailureBudget(repo, log, alert_repo=alert_repo, dispatcher=alert_dispatcher)
    # NOTE: NO await failure_budget.clear_all_quarantine(...).

    # --- docker socket client (nonexistent socket; never connects) ---
    socket_client = DockerSocketClient(socket_path=_NONEXISTENT_SOCKET, log=log)

    # --- build sources loader + compose action runner (NO start_task) ---
    build_sources_loader = BuildSourcesLoader(
        config_path=Path("/tmp/hm-test-build-sources-nonexistent.yaml"),
        log=log,
    )
    # NOTE: NO build_sources_loader.start_task().
    compose_action_runner = ComposeActionRunner(
        repo=repo,
        actions_repo=ComposeActionsRepository(repo),
        build_sources_loader=build_sources_loader,
        socket_client=socket_client,
        prom_registry=prom_registry,
        log=log,
    )

    # --- scheduler (construct; NEVER start) ---
    loaded = loader.load_all()

    def ctx_factory(c: object) -> Never:  # pragma: no cover -- scheduler not started
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
    # NOTE: NO await scheduler.start().

    # --- wire cron_discoverer instance with cron_repo (mirrors lifespan 532-534) ---
    cron_discoverer_instance: CronDiscoverer | None = None
    for lc in loaded:
        c = lc.collector
        if isinstance(c, CronDiscoverer):
            c.cron_repo = cron_repo
            cron_discoverer_instance = c

    # --- backup service ---
    db_path_str = db_url.removeprefix("sqlite+aiosqlite:///")
    backup_service = BackupService(
        db_path=Path(db_path_str),
        vm_url=os.environ.get("HOMELAB_MONITOR_VM_URL", "http://victoriametrics:8428"),
        vm_data_dir=Path("/tmp/hm-test-vm-data"),
        backup_root=Path("/tmp/hm-test-backup"),
        http_client=http_client,
        db=repo,
    )

    # ------------------------------------------------------------------
    # Assign the COMPLETE 37-attr surface (matches lifespan.py WRITE list).
    # ------------------------------------------------------------------
    app.state.master_key = master_key
    app.state.auth_repo = auth_repo
    app.state.secrets_repo = secrets_repo
    app.state.ttl_resolver = ttl_resolver
    app.state.login_rate_limiter = login_rate_limiter
    app.state.repo = repo
    app.state.broker = broker
    app.state.alert_repo = alert_repo
    app.state.alert_dispatcher = alert_dispatcher
    app.state.heartbeat_repo = heartbeat_repo
    app.state.cron_repo = cron_repo
    app.state.cron_run_repo = cron_run_repo
    app.state.http_client = http_client
    app.state.metrics_writer = metrics_writer
    app.state.in_memory_metrics_writer = in_memory_metrics_writer
    app.state.prom_registry = prom_registry
    app.state.logs_writer = logs_writer
    app.state.in_memory_logs_writer = in_memory_logs_writer
    app.state.vl_writer = vl_writer
    app.state.log_stream_state = log_stream_state
    app.state.loader = loader
    app.state.scheduler = scheduler
    app.state.failure_budget = failure_budget
    app.state.backup_service = backup_service
    app.state.docker_socket_client = socket_client
    app.state.build_sources_loader = build_sources_loader
    app.state.compose_action_runner = compose_action_runner
    app.state.cron_discoverer = cron_discoverer_instance
    # Collector instances NOT registered in the minimal set: set to None so
    # the soft getattr() router reads return the 503/None path, matching the
    # behavior tests expect when they DON'T inject a mock. Tests that need them
    # present inject their own mock onto app.state (try/finally restored).
    app.state.docker_discoverer = None
    app.state.probe_supervisor = None
    app.state.image_update_collector = None
    app.state.local_build_update_collector = None
    app.state.override_loader = None
    app.state.image_events_task = None
    app.state.cron_events_token = None
    app.state.degraded_collectors = []
    app.state.started_at = utc_now_iso()

    return TestAppHandles(
        engine=engine,
        db_url=db_url,
        log=log,
        http_client=http_client,
        docker_socket_client=socket_client,
        vl_writer=vl_writer,
    )
