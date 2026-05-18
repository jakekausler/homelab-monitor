"""FastAPI lifespan context manager with bootstrap sequence."""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import AsyncGenerator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path
from typing import Any, NoReturn, cast

import httpx
import structlog
from fastapi import FastAPI
from prometheus_client import CollectorRegistry
from structlog.stdlib import BoundLogger

from homelab_monitor.kernel.alerts.repository import AlertRepository
from homelab_monitor.kernel.api.sse import SseBroker
from homelab_monitor.kernel.backup.service import BackupService
from homelab_monitor.kernel.cron.repository import CronRepo
from homelab_monitor.kernel.db.engine import dispose_engine, get_engine
from homelab_monitor.kernel.db.migrations import MigrationsPendingError, run_migrations
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.db.time import utc_now_iso
from homelab_monitor.kernel.dispatch.channels.inproc_dashboard import InprocDashboardChannel
from homelab_monitor.kernel.dispatch.dispatcher import AlertDispatcher
from homelab_monitor.kernel.events import TriggerContext
from homelab_monitor.kernel.heartbeat.repository import HeartbeatRepo
from homelab_monitor.kernel.logging import configure_logging
from homelab_monitor.kernel.logs.multiplex import MultiplexLogsWriter
from homelab_monitor.kernel.logs.vl_writer import VictoriaLogsWriter
from homelab_monitor.kernel.metrics.multiplex import MultiplexMetricsWriter
from homelab_monitor.kernel.metrics.prometheus_writer import PrometheusRegistryWriter
from homelab_monitor.kernel.plugins.base import Collector
from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.io import (
    InMemoryLogsWriter,
    MemoryRetainingMetricsWriter,
)
from homelab_monitor.kernel.plugins.loader import PluginLoader
from homelab_monitor.kernel.plugins.noop import NoopCollector
from homelab_monitor.kernel.scheduler.failure_budget import FailureBudget
from homelab_monitor.kernel.scheduler.scheduler import Scheduler, SchedulerConfig
from homelab_monitor.kernel.secrets.master_key import MasterKeyError, load_master_key
from homelab_monitor.kernel.secrets.repository import AsyncSecretsRepository
from homelab_monitor.kernel.secrets.ttl_resolver import TtlCachingSecretsResolver
from homelab_monitor.plugins.collectors.builtin.log_stream_budget import (
    LogStreamBudgetCollector,
    LogStreamState,
)


class _StubSshFactory:
    """Placeholder SSH factory that raises NotImplementedError until EPIC-017."""

    def open(self, target_id: str) -> AbstractAsyncContextManager[Any]:
        del target_id  # pragma: no cover -- stub for EPIC-017; not testable in current scope
        # Stub for EPIC-017; not testable in current scope
        raise NotImplementedError("SSH support not yet implemented")  # pragma: no cover


def _critical_abort(log: BoundLogger, event: str, **fields: object) -> NoReturn:
    """Log critical error and exit. Bypasses async cleanup by design (D1).

    Tested by patching ``os._exit`` to a no-op + observing the SystemExit
    re-raise.
    """
    log.critical(event, **fields)
    os._exit(1)
    raise SystemExit(1)  # pragma: no cover -- defensive; os._exit terminates first


def _extract_sqlite_path(db_url: str) -> str:
    """Strip the SQLAlchemy prefix from a sqlite+aiosqlite DSN."""
    if ":memory:" in db_url:
        msg = "BackupService cannot operate on in-memory SQLite (:memory: detected)"
        raise ValueError(msg)
    prefix = "sqlite+aiosqlite:///"
    if db_url.startswith(prefix):
        return db_url[len(prefix) :]
    return db_url


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:  # noqa: PLR0912, PLR0915  -- spec-mandated bootstrap sequence
    """Bootstrap sequence with hybrid abort/degrade per D1."""
    configure_logging()
    log = cast(BoundLogger, structlog.get_logger().bind(component="lifespan"))

    # 1. Master key
    try:
        master_key = load_master_key()
    except MasterKeyError as exc:
        _critical_abort(log, "lifespan.master_key_missing", error=str(exc))

    # 1b. HTTPS_ONLY_COOKIES check
    https_only_value = os.environ.get("HOMELAB_MONITOR_HTTPS_ONLY_COOKIES", "true")
    if https_only_value.strip().lower() in ("false", "0", "no"):
        log.warning(
            "HTTPS_ONLY_COOKIES is disabled; session cookies will be sent over plain HTTP. "
            "This is intended for local development only. Do NOT enable in production.",
            env_var="HOMELAB_MONITOR_HTTPS_ONLY_COOKIES",
        )

    # 2. Engine
    engine = get_engine(url=os.environ.get("HOMELAB_MONITOR_DB_URL"))

    # 3. Migrations
    try:
        await run_migrations(engine)
    except MigrationsPendingError as exc:
        _critical_abort(log, "lifespan.migrations_pending", error=str(exc))
    except Exception as exc:  # pragma: no cover -- rare migrations error
        _critical_abort(log, "lifespan.migrations_failed", error=str(exc))

    # 4. Repo + secrets
    repo = SqliteRepository(engine)
    secrets_repo = AsyncSecretsRepository(repo, master_key)
    ttl_resolver = TtlCachingSecretsResolver(secrets_repo, ttl_seconds=60.0, log=log)
    await ttl_resolver.refresh_now()
    refresh_task = asyncio.create_task(ttl_resolver.refresh_loop())

    # 4b. Auth subsystem
    from homelab_monitor.kernel.auth.rate_limit import InProcessLoginRateLimiter  # noqa: PLC0415
    from homelab_monitor.kernel.auth.repository import AuthRepository  # noqa: PLC0415

    auth_repo = AuthRepository(repo)
    login_rate_limiter = InProcessLoginRateLimiter()
    # The "no users configured" warning fires once at lifespan startup. If the
    # operator deletes the last user mid-process, /api/version's
    # `users_configured` flag flips back to false (per-request lookup), but
    # this warning won't re-fire until restart — acceptable for v1.
    user_count = await auth_repo.users_count()
    if user_count == 0:
        log.warning(
            "lifespan.bootstrap_required",
            reason="no_users_configured",
            hint="run `hm user create <USERNAME>` to create the first operator account",
        )

    # 5. Loader
    loader = PluginLoader(log=log)
    degraded: list[str] = []
    try:
        loader.register(NoopCollector, {"name": "noop", "interval_seconds": 60})
    except Exception as exc:  # pragma: no cover -- NoopCollector always succeeds
        log.warning("lifespan.collector_register_failed", name="noop", error=str(exc))
        degraded.append("noop")
    # TODO(STAGE-014): load from /config/plugins/collectors/host.yaml when YAML
    # config-loading lands; until then, defaults are baked into HostCollectorConfig.
    # TODO(STAGE-014): unify interval/timeout — ClassVar timedelta vs config
    # int seconds. Sub-second collectors (e.g., timedelta(milliseconds=500))
    # currently violate CollectorConfig.interval_seconds: int(ge=1) at
    # registration time. Either widen to float seconds or enforce a 1s
    # floor in BaseCollector.__init_subclass__.
    try:
        from homelab_monitor.plugins.collectors.builtin.host import HostCollector  # noqa: PLC0415

        loader.register(
            HostCollector,
            {
                "name": "host",
                "interval_seconds": int(HostCollector.interval.total_seconds()),
                "timeout_seconds": int(HostCollector.timeout.total_seconds()),
            },
        )
    except Exception as exc:  # pragma: no cover -- defensive, host always succeeds
        log.warning("lifespan.collector_register_failed", name="host", error=str(exc))
        degraded.append("host")

    try:
        from homelab_monitor.plugins.collectors.builtin.self_disk import (  # noqa: PLC0415
            SelfDiskCollector,
        )

        loader.register(
            SelfDiskCollector,
            {
                "name": "self_disk",
                "interval_seconds": int(SelfDiskCollector.interval.total_seconds()),
                "timeout_seconds": int(SelfDiskCollector.timeout.total_seconds()),
            },
        )
    except Exception as exc:  # pragma: no cover -- defensive, self_disk always succeeds
        log.warning("lifespan.collector_register_failed", name="self_disk", error=str(exc))
        degraded.append("self_disk")

    try:
        loader.register(
            LogStreamBudgetCollector,
            {
                "name": "log_stream_budget",
                "interval_seconds": int(LogStreamBudgetCollector.interval.total_seconds()),
                "timeout_seconds": int(LogStreamBudgetCollector.timeout.total_seconds()),
            },
        )
    except Exception as exc:  # pragma: no cover -- defensive
        log.warning(
            "lifespan.collector_register_failed",
            name="log_stream_budget",
            error=str(exc),
        )
        degraded.append("log_stream_budget")

    try:
        from homelab_monitor.plugins.discoverers.cron_discoverer import (  # noqa: PLC0415
            CronDiscoverer,
        )

        loader.register(
            CronDiscoverer,
            {
                "name": "cron-discoverer",
                "interval_seconds": int(CronDiscoverer.interval.total_seconds()),
                "timeout_seconds": int(CronDiscoverer.timeout.total_seconds()),
            },
        )
    except Exception as exc:  # pragma: no cover -- defensive
        log.warning(
            "lifespan.collector_register_failed",
            name="cron-discoverer",
            error=str(exc),
        )
        degraded.append("cron-discoverer")

    try:
        from homelab_monitor.kernel.metrics.heartbeat_collector import (  # noqa: PLC0415
            HeartbeatStateCollector,
        )

        loader.register(
            HeartbeatStateCollector,
            {
                "name": "heartbeat_state",
                "interval_seconds": int(HeartbeatStateCollector.interval.total_seconds()),
                "timeout_seconds": int(HeartbeatStateCollector.timeout.total_seconds()),
            },
        )
    except Exception as exc:  # pragma: no cover -- defensive
        log.warning(
            "lifespan.collector_register_failed",
            name="heartbeat_state",
            error=str(exc),
        )
        degraded.append("heartbeat_state")

    plugins_env = os.environ.get("HOMELAB_MONITOR_PLUGINS_DIR")
    if plugins_env is not None:
        plugins_dir: Path | None = Path(plugins_env)
    else:
        # Best-effort dev default: walk up from kernel/api/lifespan.py until a
        # `runbooks/_examples` directory is found. If absent, skip subprocess
        # plugin loading (only the built-in noop collector will register).
        # WHY parents[5]: 5 levels up from kernel/api/lifespan.py = repo root
        candidate = Path(__file__).resolve().parents[5] / "runbooks" / "_examples"
        plugins_dir = candidate if candidate.is_dir() else None

    if plugins_dir is not None:
        loader.load_subprocess_plugins(plugins_dir)
    else:  # pragma: no cover -- no plugins_dir when env unset
        log.info("lifespan.subprocess_plugins_skipped", reason="no_plugins_dir")

    # 6. Persist
    try:
        await loader.persist_to_db(repo)
    except Exception as exc:  # pragma: no cover -- persist always succeeds in tests
        _critical_abort(log, "lifespan.persist_failed", error=str(exc))

    # 7. Resources for ctx_factory
    http_client = httpx.AsyncClient(
        limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
        timeout=httpx.Timeout(10.0, connect=5.0),
    )
    in_memory_metrics_writer = MemoryRetainingMetricsWriter()
    prom_registry = CollectorRegistry()
    prom_writer = PrometheusRegistryWriter(prom_registry)
    metrics_writer = MultiplexMetricsWriter([in_memory_metrics_writer, prom_writer])
    in_memory_logs_writer = InMemoryLogsWriter()
    vl_url = os.environ.get("HOMELAB_MONITOR_VL_URL", "http://victorialogs:9428")
    vl_writer = VictoriaLogsWriter(
        vl_url=vl_url,
        http_client=http_client,
    )
    flusher_task = asyncio.create_task(vl_writer.run_flusher())
    logs_writer = MultiplexLogsWriter([in_memory_logs_writer, vl_writer])
    log_stream_state: LogStreamState = {}
    ssh_factory = _StubSshFactory()
    broker = SseBroker(log)
    alert_repo = AlertRepository(repo)
    heartbeat_repo = HeartbeatRepo(repo)
    cron_repo = CronRepo(repo)
    alert_dispatcher = AlertDispatcher(
        channels=[InprocDashboardChannel(broker)],
        log=log,
    )
    failure_budget = FailureBudget(
        repo,
        log,
        alert_repo=alert_repo,
        dispatcher=alert_dispatcher,
    )

    # 6b. Clear all quarantine on startup (prevents stale quarantine from blocking redeploys)
    quarantine_cleared = await failure_budget.clear_all_quarantine(by="system_startup")
    if quarantine_cleared > 0:
        log.info(
            "lifespan.quarantine_cleared_on_startup",
            count=quarantine_cleared,
        )

    def ctx_factory(c: Collector) -> CollectorContext:
        """Build a CollectorContext for a collector."""
        secrets_view = ttl_resolver.current().filtered(loader.declared_secrets(c.name))
        bound_log = log.bind(collector=c.name)
        return CollectorContext(
            config=loader.config_for(c.name),
            db=repo,
            vm=metrics_writer,
            vl=logs_writer,
            http=http_client,
            ssh=ssh_factory,
            secrets=secrets_view,
            log=bound_log,  # pyright: ignore[reportArgumentType]
            ha=None,
        )

    collectors = loader.load_all()
    # TODO(refactor): inject log_stream_state via CollectorContext rather
    # than mutating private attrs post-construction. Currently this is the
    # cleanest path because the loader doesn't support DI on registration.
    # Revisit when adding the next stateful collector.
    for lc in collectors:
        c = lc.collector  # unwrap LoadedCollector to get the actual collector instance
        # only fires when LogStreamBudgetCollector is loaded (integration-only)
        if isinstance(c, LogStreamBudgetCollector):  # pragma: no cover
            c._state = log_stream_state  # pyright: ignore[reportPrivateUsage]
            c._vl_url = vl_url.rstrip("/")  # pyright: ignore[reportPrivateUsage]
            c._http_client = http_client  # pyright: ignore[reportPrivateUsage]
        # Wire cron_repo into the CronDiscoverer instance for API endpoint access
        from homelab_monitor.plugins.discoverers.cron_discoverer import (  # noqa: PLC0415
            CronDiscoverer,
        )

        if isinstance(c, CronDiscoverer):
            c.cron_repo = cron_repo
            app.state.cron_discoverer = c
    scheduler = Scheduler(
        collectors,
        ctx_factory,
        metrics_writer,
        SchedulerConfig(event_sink=broker),
        failure_budget=failure_budget,
        alert_repo=alert_repo,
        alert_dispatcher=alert_dispatcher,
    )
    await scheduler.start()

    # 7b. One-shot cron-discovery on startup. The scheduler's per-collector
    # tick loop applies an initial offset, so the first SCHEDULED cron tick
    # can be up to ~one interval (300s) away. Requesting an immediate run
    # here makes the `wrapper_installed` column converge within seconds of a
    # restart/upgrade. Fire-and-forget: request_immediate_run only enqueues
    # the run and returns; app startup is not blocked on the discovery scan.
    # Skipped when cron-discoverer failed to register (degraded). Best-effort:
    # any failure is logged and swallowed so startup always completes.
    # The startup discovery pass is disabled when
    # HOMELAB_MONITOR_DISABLE_STARTUP_CRON_DISCOVERY is truthy. The test
    # fixtures set this to keep the fire-and-forget discovery task from
    # racing test-seeded cron rows (UNIQUE crons.fingerprint). Production
    # leaves it unset, so the pass runs normally.
    _disable_startup_discovery = os.environ.get(
        "HOMELAB_MONITOR_DISABLE_STARTUP_CRON_DISCOVERY", ""
    ).strip().lower() in ("1", "true", "yes")
    if "cron-discoverer" not in degraded and not _disable_startup_discovery:
        try:
            await scheduler.request_immediate_run(
                "cron-discoverer",
                trigger=TriggerContext(kind="manual", request_id=None),
            )
            log.info("lifespan.cron_discovery_startup_run_requested")
        except Exception as exc:  # pragma: no cover -- defensive; guarded above
            log.warning(
                "lifespan.cron_discovery_startup_run_failed",
                error=str(exc),
            )

    app.state.master_key = master_key
    app.state.auth_repo = auth_repo
    app.state.secrets_repo = secrets_repo
    app.state.login_rate_limiter = login_rate_limiter
    app.state.scheduler = scheduler
    app.state.repo = repo
    app.state.broker = broker
    app.state.alert_repo = alert_repo
    app.state.heartbeat_repo = heartbeat_repo
    app.state.cron_repo = cron_repo
    app.state.alert_dispatcher = alert_dispatcher
    app.state.ttl_resolver = ttl_resolver
    app.state.http_client = http_client
    app.state.metrics_writer = metrics_writer
    app.state.in_memory_metrics_writer = in_memory_metrics_writer
    app.state.prom_registry = prom_registry
    app.state.logs_writer = logs_writer
    app.state.in_memory_logs_writer = in_memory_logs_writer
    app.state.vl_writer = vl_writer
    app.state.log_stream_state = log_stream_state
    app.state.loader = loader
    app.state.failure_budget = failure_budget

    # 8. Backup service (admin endpoint + CLI share this instance).
    db_path_str = _extract_sqlite_path(
        os.environ.get("HOMELAB_MONITOR_DB_URL", "sqlite+aiosqlite:////data/homelab-monitor.db")
    )
    backup_service = BackupService(
        db_path=Path(db_path_str),
        vm_url=os.environ.get("HOMELAB_MONITOR_VM_URL", "http://victoriametrics:8428"),
        vm_data_dir=Path(os.environ.get("HOMELAB_MONITOR_VM_DATA_DIR", "/var/vm-data")),
        backup_root=Path(
            os.environ.get("HOMELAB_MONITOR_BACKUP_ROOT", "/storage/backup/homelab-monitor")
        ),
        http_client=http_client,
        db=repo,
    )
    app.state.backup_service = backup_service

    app.state.degraded_collectors = degraded

    # 8b. Alertmanager render-on-boot (idempotent: reuses token if present).
    # Failures here are logged and swallowed; lifespan continues.
    am_template_path = Path(
        os.environ.get(
            "HOMELAB_MONITOR_ALERTMANAGER_TEMPLATE",
            "/etc/alertmanager-template/alertmanager.yml.template",
        )
    )
    am_output_path = Path(
        os.environ.get(
            "HOMELAB_MONITOR_ALERTMANAGER_OUTPUT",
            "/var/alertmanager-config/alertmanager.yml",
        )
    )
    am_url = os.environ.get("HOMELAB_MONITOR_ALERTMANAGER_URL", "http://alertmanager:9093")
    # When the env var is one of these sentinels, skip the reload entirely (used in tests + ops).
    am_reload_url: str | None = (
        None if am_url.strip().lower() in {"disabled", "", "false", "0", "none"} else am_url
    )

    from homelab_monitor.kernel.alertmanager.render import render_on_boot  # noqa: PLC0415

    await render_on_boot(
        auth_repo=auth_repo,
        secrets_repo=secrets_repo,
        template_path=am_template_path,
        output_path=am_output_path,
        am_url=am_reload_url,
        http_client=http_client,
        log=log,
    )

    # 8c. Cron-events Vector config render-on-boot (idempotent: reuses token
    # if present). Mirrors the # 8b Alertmanager block MINUS the reload step —
    # Vector reads the rendered config at container start. Failures here are
    # logged and swallowed inside render_on_boot; lifespan continues.
    vector_template_path = Path(
        os.environ.get(
            "HOMELAB_MONITOR_VECTOR_TEMPLATE",
            "/etc/vector-template/vector.toml.template",
        )
    )
    vector_output_path = Path(
        os.environ.get(
            "HOMELAB_MONITOR_VECTOR_OUTPUT",
            "/var/vector-config/vector.toml",
        )
    )

    from homelab_monitor.kernel.cron.render import (  # noqa: PLC0415
        render_on_boot as render_vector_on_boot,
    )

    cron_events_token = await render_vector_on_boot(
        auth_repo=auth_repo,
        secrets_repo=secrets_repo,
        template_path=vector_template_path,
        output_path=vector_output_path,
        log=log,
    )
    if cron_events_token is not None:
        app.state.cron_events_token = cron_events_token

    app.state.started_at = utc_now_iso()

    try:
        yield
    finally:
        await scheduler.stop()
        refresh_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await refresh_task
        await vl_writer.aclose()
        with contextlib.suppress(asyncio.CancelledError):
            await flusher_task
        await http_client.aclose()
        await dispose_engine()
