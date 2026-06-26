"""FastAPI lifespan context manager with bootstrap sequence."""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import NoReturn, cast

import httpx
import structlog
from fastapi import FastAPI
from prometheus_client import CollectorRegistry
from structlog.stdlib import BoundLogger

from homelab_monitor.kernel.alerts.repository import AlertRepository
from homelab_monitor.kernel.api.sse import SseBroker
from homelab_monitor.kernel.backup.service import BackupService
from homelab_monitor.kernel.config import (
    load_docker_config,
    load_ha_config,
    load_ha_registry_config,
    load_pihole_config,
    load_pihole_unbound_config,
    load_synology_config,
    load_tail_config,
    load_unifi_config,
)
from homelab_monitor.kernel.cron.repository import CronRepo
from homelab_monitor.kernel.cron.run_repository import CronRunRepository
from homelab_monitor.kernel.db.engine import dispose_engine, get_engine
from homelab_monitor.kernel.db.migrations import MigrationsPendingError, run_migrations
from homelab_monitor.kernel.db.repositories.unifi_clients_repository import UnifiClientRepo
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.db.time import utc_now_iso
from homelab_monitor.kernel.dispatch.channels.ha_event import HAEventChannel
from homelab_monitor.kernel.dispatch.channels.ha_push import HAPushChannel
from homelab_monitor.kernel.dispatch.channels.inproc_dashboard import InprocDashboardChannel
from homelab_monitor.kernel.dispatch.dispatcher import AlertDispatcher
from homelab_monitor.kernel.events import TriggerContext
from homelab_monitor.kernel.ha.client import HomeAssistantRestClient
from homelab_monitor.kernel.ha.entity_registry import HaEntityRegistryCache
from homelab_monitor.kernel.ha.websocket import HomeAssistantWebsocketClient
from homelab_monitor.kernel.heartbeat.repository import HeartbeatRepo
from homelab_monitor.kernel.logging import configure_logging
from homelab_monitor.kernel.logs.cron_run_failure_enrichments_repo import (
    CronRunFailureEnrichmentsRepository,
)
from homelab_monitor.kernel.logs.multiplex import MultiplexLogsWriter
from homelab_monitor.kernel.logs.tail_service import TailRegistry
from homelab_monitor.kernel.logs.vl_writer import VictoriaLogsWriter
from homelab_monitor.kernel.metrics.multiplex import MultiplexMetricsWriter
from homelab_monitor.kernel.metrics.prometheus_writer import PrometheusRegistryWriter
from homelab_monitor.kernel.pihole.client import PiholeRestClient
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
from homelab_monitor.kernel.ssh.client import AsyncSshClientFactory
from homelab_monitor.kernel.ssh.config import load_ssh_targets
from homelab_monitor.kernel.synology.client import SynologyRestClient
from homelab_monitor.kernel.unifi.client import UnifiRestClient
from homelab_monitor.plugins.collectors.builtin.log_error_rate import (
    LogErrorRateCollector,
)
from homelab_monitor.plugins.collectors.builtin.log_stream_budget import (
    LogStreamBudgetCollector,
    LogStreamState,
)
from homelab_monitor.plugins.collectors.builtin.tail_metrics import TailMetricsCollector
from homelab_monitor.plugins.collectors.builtin.vl_health import VlHealthCollector


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
    # B1 (parallel-instance): master switch for the Docker plugin. When disabled,
    # the DockerSocketCollector + DockerDiscoverer are never registered and no
    # DockerSocketClient is constructed, so the instance does no container
    # monitoring and never touches the docker socket. Defaults True -> unset env
    # reproduces today's behavior exactly.
    docker_config = load_docker_config()
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
        from homelab_monitor.plugins.collectors.builtin.watched_dir_size import (  # noqa: PLC0415
            WatchedDirSizeCollector,
        )

        loader.register(
            WatchedDirSizeCollector,
            {
                "name": "watched_dir_size",
                "interval_seconds": int(WatchedDirSizeCollector.interval.total_seconds()),
                "timeout_seconds": int(WatchedDirSizeCollector.timeout.total_seconds()),
            },
        )
    except Exception as exc:  # pragma: no cover -- defensive
        log.warning(
            "lifespan.collector_register_failed",
            name="watched_dir_size",
            error=str(exc),
        )
        degraded.append("watched_dir_size")

    try:
        from homelab_monitor.plugins.collectors.builtin.synology_mount_health import (  # noqa: PLC0415
            SynologyMountHealthCollector,
        )

        loader.register(
            SynologyMountHealthCollector,
            {
                "name": "synology_mount_health",
                "interval_seconds": int(SynologyMountHealthCollector.interval.total_seconds()),
                "timeout_seconds": int(SynologyMountHealthCollector.timeout.total_seconds()),
            },
        )
    except Exception as exc:  # pragma: no cover -- defensive
        log.warning(
            "lifespan.collector_register_failed",
            name="synology_mount_health",
            error=str(exc),
        )
        degraded.append("synology_mount_health")

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
        loader.register(
            LogErrorRateCollector,
            {
                "name": "log_error_rate",
                "interval_seconds": int(LogErrorRateCollector.interval.total_seconds()),
                "timeout_seconds": int(LogErrorRateCollector.timeout.total_seconds()),
            },
        )
    except Exception as exc:  # pragma: no cover -- defensive
        log.warning(
            "lifespan.collector_register_failed",
            name="log_error_rate",
            error=str(exc),
        )
        degraded.append("log_error_rate")

    try:
        loader.register(
            VlHealthCollector,
            {
                "name": "vl_health",
                "interval_seconds": int(VlHealthCollector.interval.total_seconds()),
                "timeout_seconds": int(VlHealthCollector.timeout.total_seconds()),
            },
        )
    except Exception as exc:  # pragma: no cover -- defensive
        log.warning(
            "lifespan.collector_register_failed",
            name="vl_health",
            error=str(exc),
        )
        degraded.append("vl_health")

    try:
        loader.register(
            TailMetricsCollector,
            {
                "name": "tail_metrics",
                "interval_seconds": int(TailMetricsCollector.interval.total_seconds()),
                "timeout_seconds": int(TailMetricsCollector.timeout.total_seconds()),
            },
        )
    except Exception as exc:  # pragma: no cover -- defensive
        log.warning(
            "lifespan.collector_register_failed",
            name="tail_metrics",
            error=str(exc),
        )
        degraded.append("tail_metrics")

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

    try:
        from homelab_monitor.kernel.metrics.cron_run_reconciler import (  # noqa: PLC0415
            CronRunReconciler,
        )

        loader.register(
            CronRunReconciler,
            {
                "name": "cron_run_reconciler",
                "interval_seconds": int(CronRunReconciler.interval.total_seconds()),
                "timeout_seconds": int(CronRunReconciler.timeout.total_seconds()),
            },
        )
    except Exception as exc:  # pragma: no cover -- defensive
        log.warning(
            "lifespan.collector_register_failed",
            name="cron_run_reconciler",
            error=str(exc),
        )
        degraded.append("cron_run_reconciler")

    try:
        from homelab_monitor.kernel.metrics.container_crash_reconciler import (  # noqa: PLC0415
            ContainerCrashReconciler,
        )

        loader.register(
            ContainerCrashReconciler,
            {
                "name": "container_crash_reconciler",
                "interval_seconds": int(ContainerCrashReconciler.interval.total_seconds()),
                "timeout_seconds": int(ContainerCrashReconciler.timeout.total_seconds()),
            },
        )
    except Exception as exc:  # pragma: no cover -- defensive
        log.warning(
            "lifespan.collector_register_failed",
            name="container_crash_reconciler",
            error=str(exc),
        )
        degraded.append("container_crash_reconciler")

    try:
        from homelab_monitor.kernel.metrics.container_healthcheck_reconciler import (  # noqa: PLC0415
            ContainerHealthcheckReconciler,
        )

        loader.register(
            ContainerHealthcheckReconciler,
            {
                "name": "container_healthcheck_reconciler",
                "interval_seconds": int(ContainerHealthcheckReconciler.interval.total_seconds()),
                "timeout_seconds": int(ContainerHealthcheckReconciler.timeout.total_seconds()),
            },
        )
    except Exception as exc:  # pragma: no cover -- defensive
        log.warning(
            "lifespan.collector_register_failed",
            name="container_healthcheck_reconciler",
            error=str(exc),
        )
        degraded.append("container_healthcheck_reconciler")

    try:
        from homelab_monitor.kernel.metrics.redaction_audit import (  # noqa: PLC0415
            RedactionAuditCollector,
        )

        loader.register(
            RedactionAuditCollector,
            {
                "name": "redaction_audit",
                "interval_seconds": int(RedactionAuditCollector.interval.total_seconds()),
                "timeout_seconds": int(RedactionAuditCollector.timeout.total_seconds()),
            },
        )
    except Exception as exc:  # pragma: no cover -- defensive
        log.warning(
            "lifespan.collector_register_failed",
            name="redaction_audit",
            error=str(exc),
        )
        degraded.append("redaction_audit")

    # B1 (parallel-instance): skip Docker collector registration entirely when
    # the Docker plugin is disabled. The post-construction isinstance() wiring
    # blocks below become automatic no-ops when nothing is registered.
    if docker_config.enabled:
        try:
            from homelab_monitor.kernel.metrics.docker_socket_collector import (  # noqa: PLC0415
                DockerSocketCollector,
            )

            loader.register(
                DockerSocketCollector,
                {
                    "name": "docker_socket",
                    "interval_seconds": int(DockerSocketCollector.interval.total_seconds()),
                    "timeout_seconds": int(DockerSocketCollector.timeout.total_seconds()),
                },
            )
        except Exception as exc:  # pragma: no cover -- defensive
            log.warning(
                "lifespan.collector_register_failed",
                name="docker_socket",
                error=str(exc),
            )
            degraded.append("docker_socket")

        try:
            from homelab_monitor.plugins.discoverers.docker_discoverer import (  # noqa: PLC0415
                DockerDiscoverer,
            )

            # TODO: add load test for high-event-rate scenarios where events_loop +
            # periodic_task contend on the asyncio.Lock during docker compose up bursts.
            # Current tests serialize the two paths. See code review I4.
            loader.register(
                DockerDiscoverer,
                {
                    "name": "docker_discoverer",
                    "interval_seconds": int(DockerDiscoverer.interval.total_seconds()),
                    "timeout_seconds": int(DockerDiscoverer.timeout.total_seconds()),
                },
            )
        except Exception as exc:  # pragma: no cover -- defensive
            log.warning(
                "lifespan.collector_register_failed",
                name="docker_discoverer",
                error=str(exc),
            )
            degraded.append("docker_discoverer")

    try:
        from homelab_monitor.kernel.metrics.probe_supervisor import (  # noqa: PLC0415
            ProbeSupervisor,
        )

        loader.register(
            ProbeSupervisor,
            {
                "name": "docker_probes_supervisor",
                "interval_seconds": int(ProbeSupervisor.interval.total_seconds()),
                "timeout_seconds": int(ProbeSupervisor.timeout.total_seconds()),
            },
        )
    except Exception as exc:  # pragma: no cover -- defensive
        log.warning(
            "lifespan.collector_register_failed",
            name="docker_probes_supervisor",
            error=str(exc),
        )
        degraded.append("docker_probes_supervisor")

    try:
        from homelab_monitor.kernel.metrics.image_update_collector import (  # noqa: PLC0415
            ImageUpdateCollector,
        )

        loader.register(
            ImageUpdateCollector,
            {
                "name": "image_update_checker",
                "interval_seconds": int(ImageUpdateCollector.interval.total_seconds()),
                "timeout_seconds": int(ImageUpdateCollector.timeout.total_seconds()),
            },
        )
    except Exception as exc:  # pragma: no cover -- defensive
        log.warning(
            "lifespan.collector_register_failed",
            name="image_update_checker",
            error=str(exc),
        )
        degraded.append("image_update_checker")

    # ------------------------------------------------------------------
    # STAGE-003-009: LocalBuildUpdateCollector
    # ------------------------------------------------------------------
    try:
        from homelab_monitor.kernel.metrics.local_build_update_collector import (  # noqa: PLC0415
            LocalBuildUpdateCollector,
        )

        loader.register(
            LocalBuildUpdateCollector,
            {
                "name": "local_build_update_checker",
                "interval_seconds": int(LocalBuildUpdateCollector.interval.total_seconds()),
                "timeout_seconds": int(LocalBuildUpdateCollector.timeout.total_seconds()),
            },
        )
    except Exception as exc:  # pragma: no cover -- defensive
        log.warning(
            "lifespan.collector_register_failed",
            name="local_build_update_checker",
            error=str(exc),
        )
        degraded.append("local_build_update_checker")

    # ------------------------------------------------------------------
    # STAGE-004-035: NewSignatureCollector (anomaly type A — new signature)
    # ------------------------------------------------------------------
    try:
        from homelab_monitor.kernel.metrics.new_signature_collector import (  # noqa: PLC0415
            NewSignatureCollector,
        )

        loader.register(
            NewSignatureCollector,
            {
                "name": "new_signature",
                "interval_seconds": int(NewSignatureCollector.interval.total_seconds()),
                "timeout_seconds": int(NewSignatureCollector.timeout.total_seconds()),
            },
        )
    except Exception as exc:  # pragma: no cover -- defensive
        log.warning(
            "lifespan.collector_register_failed",
            name="new_signature",
            error=str(exc),
        )
        degraded.append("new_signature")

    # ------------------------------------------------------------------
    # STAGE-004-038: SilenceDetectionCollector (anomaly type D — signature silent)
    # ------------------------------------------------------------------
    try:
        from homelab_monitor.kernel.metrics.silence_detection_collector import (  # noqa: PLC0415
            SilenceDetectionCollector,
        )

        loader.register(
            SilenceDetectionCollector,
            {
                "name": "silence_detection",
                "interval_seconds": int(SilenceDetectionCollector.interval.total_seconds()),
                "timeout_seconds": int(SilenceDetectionCollector.timeout.total_seconds()),
            },
        )
    except Exception as exc:  # pragma: no cover -- defensive
        log.warning(
            "lifespan.collector_register_failed",
            name="silence_detection",
            error=str(exc),
        )
        degraded.append("silence_detection")

    # ------------------------------------------------------------------
    # STAGE-005-003: Home Assistant integration bundle (integrations/ exemplar).
    # The bundle owns per-collector failure isolation internally, so this is a
    # single import + call (no per-collector try/except here). Wave-B HA
    # collectors are added inside the bundle, not here.
    # ------------------------------------------------------------------
    from homelab_monitor.plugins.collectors.integrations.homeassistant import (  # noqa: PLC0415
        register_all as register_ha_collectors,
    )

    register_ha_collectors(loader)

    # ------------------------------------------------------------------
    # STAGE-017-006: SSH probe bundle (plugins/collectors/ssh/ exemplar).
    # The bundle owns per-probe failure isolation internally (one per ssh_target);
    # if no ssh_targets are configured it's a no-op.
    # ------------------------------------------------------------------
    from homelab_monitor.plugins.collectors.ssh import (  # noqa: PLC0415
        register_all as register_ssh_collectors,
    )

    register_ssh_collectors(loader)

    # ------------------------------------------------------------------
    # STAGE-007-002: Unifi integration bundle (integrations/unifi/).
    # The bundle owns per-collector failure isolation internally. Wave-B/C
    # Unifi collectors are added inside the bundle, not here. The placeholder
    # collector is throwaway scaffolding (removed by STAGE-007-005).
    # ------------------------------------------------------------------
    from homelab_monitor.plugins.collectors.integrations.unifi import (  # noqa: PLC0415
        register_all as register_unifi_collectors,
    )

    register_unifi_collectors(loader)

    # ------------------------------------------------------------------
    # STAGE-006-002: Pi-hole integration bundle (integrations/pihole/).
    # The bundle owns per-collector failure isolation internally. Wave-B
    # Pi-hole collectors are added inside the bundle, not here. The placeholder
    # collector is throwaway scaffolding (removed by STAGE-006-005).
    # ------------------------------------------------------------------
    from homelab_monitor.plugins.collectors.integrations.pihole import (  # noqa: PLC0415
        register_all as register_pihole_collectors,
    )

    register_pihole_collectors(loader)

    # ------------------------------------------------------------------
    # STAGE-008-002: Synology integration bundle (integrations/synology/).
    # The bundle owns per-collector failure isolation internally. Wave-B
    # Synology collectors are added inside the bundle, not here. The placeholder
    # collector is throwaway scaffolding (removed by STAGE-008-005).
    # ------------------------------------------------------------------
    from homelab_monitor.plugins.collectors.integrations.synology import (  # noqa: PLC0415
        register_all as register_synology_collectors,
    )

    register_synology_collectors(loader)

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

    # 7a. Home Assistant REST client (STAGE-005-001).
    #
    # DEGRADATION RESOLUTION (supersedes the card's literal ``ha=None`` flow):
    # The card's locked construction snippet wrote ``ha_client = ... if ha_token
    # is not None else None`` AND required "post-startup recovery without restart
    # via token_provider re-reading per tick". Those two are in tension: a client
    # constructed-once as ``None`` can never become non-None per tick. We resolve
    # by ALWAYS constructing the client with a per-request ``token_provider``; the
    # "no token configured" case is handled INSIDE the client (it returns
    # HaError(reason="auth") WITHOUT a network call). This is the only way to
    # honor the once-only construction constraint AND no-restart token recovery.
    # ``ctx.ha`` is therefore never None due to token absence; the collector-side
    # ``reason="no_token"`` no-op is a Wave-B collector concern (STAGE-005-006+),
    # NOT this stage. The token value is never stored on the client and never
    # logged.
    ha_config = load_ha_config()
    ha_client = HomeAssistantRestClient(
        base_url=ha_config.base_url,
        http=http_client,  # reuse the shared pool — do NOT create a second client
        token_provider=lambda: ttl_resolver.current().get("ha_token"),
    )
    # STAGE-005-031: expose the REST client for the HA detail enrichment layer
    # (get_ha_client dep). Mirrors app.state.ha_ws_client below.
    app.state.ha_client = ha_client

    # 7a-unifi. Unifi REST client (STAGE-007-001).
    #
    # D1 — a SECOND, DEDICATED httpx client with verify=False (the UDM uses a
    # self-signed cert CN=unifi.local). Blast radius of verify=False is exactly the
    # one UDM target; the vanilla http_client above keeps full verification for
    # everything else. Lifespan owns this client's construction + teardown (closed in
    # the finally block alongside http_client); UnifiRestClient borrows it.
    #
    # The unifi_api_key is read per-request via the TTL resolver (mirrors HA's
    # token_provider) and is never stored on the client nor logged. The client is
    # ALWAYS constructed (never None); a missing key surfaces as UnifiError(auth)
    # inside the client without a network call.
    unifi_http_client = httpx.AsyncClient(
        verify=False,  # self-signed UDM cert; blast radius is one LAN target (D1, STAGE-007-001)
        limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
        timeout=httpx.Timeout(10.0, connect=5.0),
    )
    unifi_config = load_unifi_config()
    unifi_client = UnifiRestClient(
        base_url=unifi_config.base_url,
        http=unifi_http_client,
        key_provider=lambda: ttl_resolver.current().get("unifi_api_key"),
        site_id=unifi_config.site_id,
    )
    # Eager, NON-FATAL site-id resolution from v1/sites (D3). A startup UDM/key
    # failure must NOT crash the app — log the typed error and continue. resolve is
    # single-shot here (NOT retried): on failure v1_site_id stays "default" until a
    # Wave-B/C collector re-invokes resolve_site_id() or the process restarts. The
    # classic site_name ("default") is unaffected and works regardless.
    unifi_site_err = await unifi_client.resolve_site_id()
    if unifi_site_err is not None:
        log.warning(
            "lifespan.unifi_site_id_unresolved",
            reason=unifi_site_err.reason,
            message=unifi_site_err.message,
        )
    app.state.unifi_client = unifi_client

    # STAGE-007-003: guarantee a first-class host row in the unifi_clients
    # registry. Sentinel-keyed (host:<ip>) until the active-client collector
    # discovers the host's real MAC (STAGE-007-004/007). Idempotent + safe; runs
    # after migrations (step 3) and after unifi_config is loaded. A failure here
    # must NOT crash startup (the registry seed is best-effort), so wrap + log.
    try:
        await UnifiClientRepo(repo).ensure_host_row(unifi_config.host_lan_ip)
    except Exception as exc:  # pragma: no cover -- defensive; ensure_host_row is safe
        log.warning("lifespan.unifi_host_row_seed_failed", error=str(exc))

    # 7a-pihole. Pi-hole v6 REST client (STAGE-006-001).
    #
    # Reuses the SHARED http_client (Pi-hole is plain HTTP on the LAN — no TLS, so no
    # dedicated verify=False client like Unifi needs). The app password is read
    # per-login via the TTL resolver (mirrors HA's token_provider) and is never stored
    # on the client nor logged. The client is ALWAYS constructed (never None); a
    # missing password surfaces as PiholeError(auth) inside the client without a
    # network call. Logout (DELETE /api/auth) runs best-effort in the finally block.
    pihole_config = load_pihole_config()
    pihole_client = PiholeRestClient(
        base_url=pihole_config.base_url,
        http=http_client,  # reuse the shared pool (plain HTTP, no TLS)
        password_provider=lambda: ttl_resolver.current().get("pihole_api_password_ro"),
    )
    app.state.pihole_client = pihole_client

    # 7a-pihole-rw. Pi-hole v6 RW REST client (STAGE-006-018).
    #
    # A SECOND long-lived client resolving the write-scoped app password
    # ("pihole_api_password_rw") for the Wave-E write endpoints (set blocking, gravity
    # update). It is the RW credential mirror of the RO client above: same shared
    # http_client, same per-login TTL-resolver password lookup, never stored/logged.
    # It is injected ONLY into the pihole WRITE router (via app.state) — it MUST NOT
    # leak into the RO collector path (ctx_factory wires the RO client only).
    pihole_rw_client = PiholeRestClient(
        base_url=pihole_config.base_url,
        http=http_client,  # reuse the shared pool (plain HTTP, no TLS)
        password_provider=lambda: ttl_resolver.current().get("pihole_api_password_rw"),
    )
    app.state.pihole_rw_client = pihole_rw_client

    # 7a-synology. Synology DSM v7 REST client (STAGE-008-001).
    #
    # A SECOND, DEDICATED httpx client with verify=False (the DSM serves a
    # self-signed cert CN=synology). Blast radius of verify=False is exactly the one
    # DSM target; the vanilla http_client above keeps full verification for
    # everything else. Lifespan owns this client's construction + teardown (closed in
    # the finally block alongside unifi_http_client); SynologyRestClient borrows it.
    #
    # The synology_dsm_password is read per-login via the TTL resolver (mirrors HA's
    # token_provider) and is never stored on the client nor logged. The account name
    # is not a secret and comes from config. The client is ALWAYS constructed (never
    # None); a missing password surfaces as SynologyError(auth) inside the client
    # without a network call. Logout (method=logout) runs best-effort in the finally
    # block BEFORE the dedicated client is closed.
    synology_http_client = httpx.AsyncClient(
        verify=False,  # self-signed DSM cert; blast radius is one LAN target (STAGE-008-001)
        limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
        timeout=httpx.Timeout(10.0, connect=5.0),
    )
    synology_config = load_synology_config()
    synology_client = SynologyRestClient(
        base_url=synology_config.base_url,
        http=synology_http_client,
        account=synology_config.account,
        password_provider=lambda: ttl_resolver.current().get("synology_dsm_password"),
    )
    app.state.synology_client = synology_client

    in_memory_metrics_writer = MemoryRetainingMetricsWriter()
    prom_registry = CollectorRegistry()
    prom_writer = PrometheusRegistryWriter(prom_registry)
    metrics_writer = MultiplexMetricsWriter([in_memory_metrics_writer, prom_writer])
    # 7a-ws. Home Assistant WebSocket client (STAGE-005-002).
    # Constructed AFTER metrics_writer (it emits homelab_ha_websocket_* series).
    # Reuses the same per-request token_provider as the REST client so a rotated
    # / post-startup ha_token is picked up at the next (re)connect without a
    # restart. The token value is never stored on the client and never logged.
    ha_ws_client = HomeAssistantWebsocketClient(
        base_url=ha_config.base_url,
        token_provider=lambda: ttl_resolver.current().get("ha_token"),
        metrics_writer=metrics_writer,
        log=log.bind(component="ha_websocket"),
    )
    if ha_config.base_url:
        ha_ws_client.start_task()
    app.state.ha_ws_client = ha_ws_client
    # 7a-reg. HA entity-registry cache (STAGE-005-037).
    # Constructed AFTER the WS client (it sends config/entity_registry/list over
    # it) and AFTER metrics_writer (it emits homelab_ha_entity_registry_* series).
    # Excludes disabled/hidden/category entities from availability + z-score.
    ha_registry_config = load_ha_registry_config()
    ha_entity_registry = HaEntityRegistryCache(
        ws_client=ha_ws_client,
        config=ha_registry_config,
        metrics_writer=metrics_writer,
        log=log.bind(component="ha_entity_registry"),
    )
    if ha_config.base_url and ha_registry_config.enabled:
        ha_entity_registry.start_task()
    app.state.ha_entity_registry = ha_entity_registry
    in_memory_logs_writer = InMemoryLogsWriter()
    vl_url = os.environ.get("HOMELAB_MONITOR_VL_URL", "http://victorialogs:9428")
    vl_writer = VictoriaLogsWriter(
        vl_url=vl_url,
        http_client=http_client,
    )
    flusher_task = asyncio.create_task(vl_writer.run_flusher())
    logs_writer = MultiplexLogsWriter([in_memory_logs_writer, vl_writer])
    log_stream_state: LogStreamState = {}
    ssh_targets = load_ssh_targets()
    ssh_factory = AsyncSshClientFactory(
        resolve=ssh_targets.get,
        secrets_for=lambda name: ttl_resolver.current().get(name),
    )
    broker = SseBroker(log)
    tail_registry = TailRegistry(max_connections=load_tail_config().max_connections)
    alert_repo = AlertRepository(repo)
    heartbeat_repo = HeartbeatRepo(repo)
    cron_repo = CronRepo(repo)
    cron_run_repo = CronRunRepository(repo)
    cron_run_failure_repo = CronRunFailureEnrichmentsRepository(repo)
    alert_dispatcher = AlertDispatcher(
        channels=[
            InprocDashboardChannel(broker),
            HAPushChannel(ha_client, ha_config.notify_service),
            HAEventChannel(ha_client, ha_config.event_type),
        ],
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
            ha=ha_client,
            ha_registry=getattr(app.state, "ha_entity_registry", None),
            unifi=unifi_client,
            pihole=pihole_client,
            synology=synology_client,
        )

    # 7f. BuildSourcesLoader — STAGE-003-009 generic config (scope expansion).
    # Constructed before collector iteration so LocalBuildUpdateCollector can wire it.
    from homelab_monitor.kernel.docker.build_sources_loader import (  # noqa: PLC0415
        BuildSourcesLoader,
    )

    build_sources_config_path = Path(
        os.environ.get(
            "HOMELAB_MONITOR_BUILD_SOURCES_CONFIG_PATH",
            "/config/docker/build-sources.yaml",
        )
    )
    build_sources_loader = BuildSourcesLoader(
        config_path=build_sources_config_path,
        log=log,
    )
    try:
        await build_sources_loader.refresh()
    except Exception as exc:  # pragma: no cover -- defensive
        log.warning("lifespan.build_sources_loader_initial_refresh_failed", error=str(exc))
    build_sources_loader.start_task()
    app.state.build_sources_loader = build_sources_loader

    # STAGE-003-010: ComposeActionRunner — owns per-container locks + bg tasks.
    # B1 (parallel-instance): gate the ComposeActionRunner (and the early
    # DockerSocketClient it constructs) behind the Docker master switch. With
    # docker disabled, no socket client is constructed (the instance never
    # touches the docker socket) and app.state.compose_action_runner stays unset;
    # the docker router's _get_compose_action_runner already returns 503 in that
    # case, and the post-pull/-rebuild refresher wiring below no-ops on None.
    if docker_config.enabled:
        from homelab_monitor.kernel.db.repositories.compose_actions_repository import (  # noqa: PLC0415
            ComposeActionsRepository,
        )
        from homelab_monitor.kernel.docker.compose_action_runner import (  # noqa: PLC0415
            ComposeActionRunner,
        )

        # Fetch or create the DockerSocketClient (same as in collector wiring below)
        socket_client = getattr(app.state, "docker_socket_client", None)
        if socket_client is None:
            from homelab_monitor.kernel.docker.socket_client import (  # noqa: PLC0415
                DockerSocketClient,
            )

            socket_path = os.environ.get("HOMELAB_MONITOR_DOCKER_SOCKET", "/var/run/docker.sock")
            socket_client = DockerSocketClient(socket_path=socket_path, log=log)
            app.state.docker_socket_client = socket_client

        compose_action_runner = ComposeActionRunner(
            repo=repo,
            actions_repo=ComposeActionsRepository(repo),
            build_sources_loader=build_sources_loader,
            socket_client=socket_client,
            prom_registry=prom_registry,
            log=log,
        )
        app.state.compose_action_runner = compose_action_runner

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
        if isinstance(c, VlHealthCollector):  # pragma: no cover
            c._vl_url = vl_url.rstrip("/")  # pyright: ignore[reportPrivateUsage]
            c._http_client = http_client  # pyright: ignore[reportPrivateUsage]
        if isinstance(c, TailMetricsCollector):
            c._registry = tail_registry  # pyright: ignore[reportPrivateUsage]
        # Wire cron_repo into the CronDiscoverer instance for API endpoint access
        from homelab_monitor.plugins.discoverers.cron_discoverer import (  # noqa: PLC0415
            CronDiscoverer,
        )

        if isinstance(c, CronDiscoverer):
            c.cron_repo = cron_repo
            app.state.cron_discoverer = c
        # Wire DockerSocketClient into the DockerSocketCollector instance
        from homelab_monitor.kernel.metrics.docker_socket_collector import (  # noqa: PLC0415
            DockerSocketCollector,
        )

        if isinstance(c, DockerSocketCollector):
            from homelab_monitor.kernel.docker.socket_client import (  # noqa: PLC0415
                DockerSocketClient,
            )

            socket_path = os.environ.get("HOMELAB_MONITOR_DOCKER_SOCKET", "/var/run/docker.sock")
            docker_client = DockerSocketClient(socket_path=socket_path, log=log)
            c._client = docker_client  # pyright: ignore[reportPrivateUsage]
            c._vm_url = os.environ.get(  # pyright: ignore[reportPrivateUsage]
                "HOMELAB_MONITOR_VM_URL", "http://victoriametrics:8428"
            )
            app.state.docker_socket_client = docker_client
        # Wire DockerSocketClient + SuggestionsRepository into DockerDiscoverer
        from homelab_monitor.kernel.db.repositories.suggestions_repository import (  # noqa: PLC0415
            SuggestionsRepository,
        )
        from homelab_monitor.plugins.discoverers.docker_discoverer import (  # noqa: PLC0415
            DockerDiscoverer,
        )

        if isinstance(c, DockerDiscoverer):
            # Reuse the singleton DockerSocketClient already wired into
            # DockerSocketCollector above, IF present (degraded path may have
            # skipped it). Otherwise construct a dedicated one.
            discoverer_socket_client = getattr(app.state, "docker_socket_client", None)
            if discoverer_socket_client is None:  # pragma: no cover
                # Unreachable in practice: when docker is enabled, the
                # DockerSocketCollector wiring block above always constructs and
                # stores docker_socket_client on app.state before this wiring
                # loop runs; when docker is disabled, DockerDiscoverer is never
                # registered so this isinstance branch never runs at all. Kept
                # as a defensive fallback.
                from homelab_monitor.kernel.docker.socket_client import (  # noqa: PLC0415
                    DockerSocketClient,
                )

                socket_path = os.environ.get(
                    "HOMELAB_MONITOR_DOCKER_SOCKET", "/var/run/docker.sock"
                )
                discoverer_socket_client = DockerSocketClient(socket_path=socket_path, log=log)
                app.state.docker_socket_client = discoverer_socket_client
            c._socket_client = discoverer_socket_client  # pyright: ignore[reportPrivateUsage]
            c._suggestions_repo = SuggestionsRepository(repo)  # pyright: ignore[reportPrivateUsage]
            c._db = repo  # pyright: ignore[reportPrivateUsage]
            from homelab_monitor.kernel.db.repositories.probe_targets_repository import (  # noqa: PLC0415
                ProbeTargetsRepository,
            )

            c._probe_targets_repo = ProbeTargetsRepository(repo)  # pyright: ignore[reportPrivateUsage]
            from homelab_monitor.kernel.db.repositories.override_ownership_repository import (  # noqa: PLC0415
                OverrideOwnershipRepository,
            )

            c._ownership_repo = OverrideOwnershipRepository(repo)  # pyright: ignore[reportPrivateUsage]
            app.state.docker_discoverer = c
        from homelab_monitor.kernel.metrics.probe_supervisor import (  # noqa: PLC0415
            ProbeSupervisor,
        )

        if isinstance(c, ProbeSupervisor):
            c._db = repo  # pyright: ignore[reportPrivateUsage]
            c._http_client = http_client  # pyright: ignore[reportPrivateUsage]
            c._socket_client = getattr(app.state, "docker_socket_client", None)  # pyright: ignore[reportPrivateUsage]
            c._host_ip = os.environ.get("HOMELAB_MONITOR_DOCKER_HOST_IP", "127.0.0.1")  # pyright: ignore[reportPrivateUsage]
            c._exec_enabled = (  # pyright: ignore[reportPrivateUsage]
                os.environ.get("HOMELAB_MONITOR_DOCKER_PROBES_EXEC_ENABLED", "false").lower()
                == "true"
            )
            app.state.probe_supervisor = c
        from homelab_monitor.plugins.collectors.integrations.pihole.unbound_stats import (  # noqa: PLC0415
            UnboundStatsCollector,
        )

        if isinstance(c, UnboundStatsCollector):
            c._socket_client = getattr(  # pyright: ignore[reportPrivateUsage]
                app.state, "docker_socket_client", None
            )
            c._cfg = load_pihole_unbound_config()  # pyright: ignore[reportPrivateUsage]
        from homelab_monitor.plugins.collectors.integrations.pihole.dns_health import (  # noqa: PLC0415
            PiholeDnsHealthCollector,
        )

        if isinstance(c, PiholeDnsHealthCollector):
            c._dns_host = pihole_config.dns_host  # pyright: ignore[reportPrivateUsage]
            c._dns_port = pihole_config.dns_port  # pyright: ignore[reportPrivateUsage]
        from homelab_monitor.plugins.collectors.integrations.pihole.dns_split import (  # noqa: PLC0415
            PiholeDnsSplitCollector,
        )

        if isinstance(c, PiholeDnsSplitCollector):
            c._pihole_host = pihole_config.dns_host  # pyright: ignore[reportPrivateUsage]
            c._pihole_port = pihole_config.dns_port  # pyright: ignore[reportPrivateUsage]
            c._direct_host = pihole_config.direct_dns_host  # pyright: ignore[reportPrivateUsage]
            c._direct_port = pihole_config.direct_dns_port  # pyright: ignore[reportPrivateUsage]
        from homelab_monitor.plugins.collectors.integrations.pihole.query_feed import (  # noqa: PLC0415
            PiholeQueryFeedCollector,
        )

        if isinstance(c, PiholeQueryFeedCollector):
            c._client = pihole_client  # pyright: ignore[reportPrivateUsage]
        from homelab_monitor.kernel.metrics.image_update_collector import (  # noqa: PLC0415
            ImageUpdateCollector,
        )

        if isinstance(c, ImageUpdateCollector):
            from homelab_monitor.kernel.db.repositories.image_update_state_repository import (  # noqa: PLC0415
                ImageUpdateStateRepository,
            )
            from homelab_monitor.kernel.docker.registry_digest_client import (  # noqa: PLC0415
                RegistryDigestClient,
            )

            c._db = repo  # pyright: ignore[reportPrivateUsage]
            c._socket_client = getattr(app.state, "docker_socket_client", None)  # pyright: ignore[reportPrivateUsage]
            c._http_client = http_client  # pyright: ignore[reportPrivateUsage]
            c._registry_client = RegistryDigestClient(  # pyright: ignore[reportPrivateUsage]
                http_client=http_client, log=log
            )
            c._state_repo = ImageUpdateStateRepository(repo)  # pyright: ignore[reportPrivateUsage]
            app.state.image_update_collector = c
            # Wire post-pull refresher into compose_action_runner.
            # B1: compose_action_runner is None when the Docker plugin is disabled
            # (ComposeActionRunner construction is gated), so guard before wiring.
            compose_runner = getattr(app.state, "compose_action_runner", None)
            if compose_runner is not None:
                compose_runner.set_image_update_refresher(c.refresh_container)
        from homelab_monitor.kernel.metrics.local_build_update_collector import (  # noqa: PLC0415
            LocalBuildUpdateCollector,
        )

        if isinstance(c, LocalBuildUpdateCollector):
            from homelab_monitor.kernel.db.repositories.docker_build_hashes_repository import (  # noqa: PLC0415
                DockerBuildHashesRepository,
            )
            from homelab_monitor.kernel.docker.source_hash import (  # noqa: PLC0415
                SourceHashLimits,
            )

            c._db = repo  # pyright: ignore[reportPrivateUsage]
            c._socket_client = getattr(app.state, "docker_socket_client", None)  # pyright: ignore[reportPrivateUsage]
            c._build_hashes_repo = DockerBuildHashesRepository(repo)  # pyright: ignore[reportPrivateUsage]
            compose_dir_env = os.environ.get("HOMELAB_MONITOR_COMPOSE_DIR")
            if compose_dir_env:  # pragma: no cover -- env-var-set branch validated via dev rig (3a)
                c._compose_dir = Path(compose_dir_env)  # pyright: ignore[reportPrivateUsage]
            else:
                c._compose_dir = None  # pyright: ignore[reportPrivateUsage]
                log.info(
                    "lifespan.local_build_compose_dir_unset",
                    hint="set HOMELAB_MONITOR_COMPOSE_DIR to enable",
                )
            c._limits = SourceHashLimits.from_env()  # pyright: ignore[reportPrivateUsage]
            c._build_sources_loader = build_sources_loader  # pyright: ignore[reportPrivateUsage]
            app.state.local_build_update_collector = c
            # Wire post-rebuild refresher into compose_action_runner.
            # B1: compose_action_runner is None when the Docker plugin is disabled
            # (ComposeActionRunner construction is gated), so guard before wiring.
            compose_runner = getattr(app.state, "compose_action_runner", None)
            if compose_runner is not None:
                compose_runner.set_local_build_refresher(c.refresh_container)
        from homelab_monitor.kernel.metrics.new_signature_collector import (  # noqa: PLC0415
            NewSignatureCollector,
        )

        if isinstance(c, NewSignatureCollector):
            from homelab_monitor.kernel.config import load_new_signature_config  # noqa: PLC0415

            c._config = load_new_signature_config()  # pyright: ignore[reportPrivateUsage]
        from homelab_monitor.kernel.metrics.silence_detection_collector import (  # noqa: PLC0415
            SilenceDetectionCollector,
        )

        if isinstance(c, SilenceDetectionCollector):
            from homelab_monitor.kernel.config import (  # noqa: PLC0415
                load_silence_detection_config,
            )

            c._config = load_silence_detection_config()  # pyright: ignore[reportPrivateUsage]

        from homelab_monitor.plugins.collectors.integrations.homeassistant.ha_config_entry import (  # noqa: PLC0415
            HaConfigEntryCollector,
        )

        if isinstance(c, HaConfigEntryCollector):
            c._ws = app.state.ha_ws_client  # pyright: ignore[reportPrivateUsage]

        from homelab_monitor.plugins.collectors.integrations.homeassistant.ha_repairs import (  # noqa: PLC0415
            HaRepairsCollector,
        )

        if isinstance(c, HaRepairsCollector):
            c._ws = app.state.ha_ws_client  # pyright: ignore[reportPrivateUsage]

        from homelab_monitor.plugins.collectors.integrations.homeassistant.ha_persistent_notification import (  # noqa: PLC0415, E501
            HaPersistentNotificationCollector,
        )

        if isinstance(c, HaPersistentNotificationCollector):
            c._ws = app.state.ha_ws_client  # pyright: ignore[reportPrivateUsage]

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

    # 7a. Start DockerDiscoverer events loop (runs concurrently with scheduler).
    discoverer = getattr(app.state, "docker_discoverer", None)
    if discoverer is not None and "docker_discoverer" not in degraded:
        # Build a one-off ctx for the events loop (separate from the scheduler's
        # per-tick ctx). Reuses the same factory so vm/log/db are identical.
        events_ctx = ctx_factory(discoverer)
        discoverer.start_events_loop(events_ctx)

    # 7b. Start ProbeSupervisor per-container tasks (runs concurrently with scheduler).
    supervisor = getattr(app.state, "probe_supervisor", None)
    if (  # pragma: no branch — defensive degraded-collector guard
        supervisor is not None and "docker_probes_supervisor" not in degraded
    ):
        supervisor_ctx = ctx_factory(supervisor)
        await supervisor.start_per_container_tasks(supervisor_ctx)

    # 7e. Start image-events background task (D-EVENTS-FILTERS-KWARG).
    # Separate from docker_discoverer's container-events loop; subscribes to
    # 'image' type events (filters={"type":["image"]}) and triggers an
    # out-of-band image-update check on pull.
    image_events_task: asyncio.Task[None] | None = None
    image_update_collector_handle = getattr(app.state, "image_update_collector", None)
    docker_socket_client = getattr(app.state, "docker_socket_client", None)
    if (
        # B1: explicit docker master-switch guard (docker_socket_client is also
        # None when disabled, but state the intent so this loop never starts).
        docker_config.enabled
        and image_update_collector_handle is not None
        and docker_socket_client is not None
        and "image_update_checker" not in degraded
    ):
        events_ctx = ctx_factory(image_update_collector_handle)

        async def _image_events_loop() -> None:
            """Long-lived task subscribing to docker image events.

            On 'pull' events, debounces multiple rapid pulls into a single
            scheduler.request_immediate_run call (30s window).
            Reconnects with exponential backoff on failure.
            """
            _DEBOUNCE_SECONDS = 30.0
            _pending_trigger: asyncio.Task[None] | None = None

            async def _do_trigger() -> None:  # pragma: no cover -- docker events handler
                await asyncio.sleep(_DEBOUNCE_SECONDS)
                try:
                    await scheduler.request_immediate_run(
                        "image_update_checker",
                        trigger=TriggerContext(kind="manual", request_id=None),
                    )
                except Exception as exc:  # pragma: no cover -- defensive
                    events_ctx.log.warning(
                        "image_update_collector.events_trigger_failed",
                        error=str(exc),
                    )

            _backoff = 1.0
            _MAX_BACKOFF = 60.0
            while True:
                try:
                    async for event in docker_socket_client.events(  # pragma: no cover
                        filters={"type": ["image"]}
                    ):
                        action = str(event.get("Action") or event.get("status") or "")
                        if action != "pull":
                            continue
                        _backoff = 1.0  # reset on successful event
                        # Coalesce: cancel any pending debounce and restart
                        if _pending_trigger is not None and not _pending_trigger.done():
                            _pending_trigger.cancel()
                        _pending_trigger = asyncio.create_task(_do_trigger())
                    # Stream returned without raising (rare in prod; common in tests
                    # with mocked empty body). Treat as a soft failure: back off
                    # before re-subscribing so we don't busy-loop.
                    events_ctx.log.warning(
                        "image_update_collector.events_stream_closed",
                        backoff_seconds=_backoff,
                    )
                    try:
                        await asyncio.sleep(_backoff)
                    except asyncio.CancelledError:
                        raise
                    _backoff = min(_backoff * 2, _MAX_BACKOFF)  # pragma: no cover
                except asyncio.CancelledError:
                    if (
                        _pending_trigger is not None and not _pending_trigger.done()
                    ):  # pragma: no cover -- shutdown race
                        _pending_trigger.cancel()
                    raise
                except Exception as exc:  # pragma: no cover -- docker event stream reconnect
                    events_ctx.log.warning(
                        "image_update_collector.events_loop_error",
                        error=str(exc),
                        backoff_seconds=_backoff,
                    )
                    try:
                        await asyncio.sleep(_backoff)
                    except asyncio.CancelledError:
                        raise
                    _backoff = min(_backoff * 2, _MAX_BACKOFF)

        image_events_task = asyncio.create_task(
            _image_events_loop(), name="image_update_collector.events"
        )
        app.state.image_events_task = image_events_task

    # 7c. Start OverrideLoader periodic task (D-HOTRELOAD-PERIODIC-30S).
    # B1: the override loader resolves docker-container probe overrides, so it is
    # gated behind the Docker master switch — when disabled it is never
    # constructed/started and app.state.override_loader stays unset.
    if docker_config.enabled:
        from homelab_monitor.kernel.db.repositories.override_ownership_repository import (  # noqa: PLC0415
            OverrideOwnershipRepository,
        )
        from homelab_monitor.kernel.db.repositories.probe_targets_repository import (  # noqa: PLC0415
            ProbeTargetsRepository,
        )
        from homelab_monitor.kernel.db.repositories.suggestions_repository import (  # noqa: PLC0415
            SuggestionsRepository,
        )
        from homelab_monitor.kernel.docker.override_loader import OverrideLoader  # noqa: PLC0415

        overrides_dir = Path(
            os.environ.get(
                "HOMELAB_MONITOR_DOCKER_OVERRIDES_DIR",
                "/config/plugins/docker",
            )
        )
        exec_enabled_globally = (
            os.environ.get("HOMELAB_MONITOR_DOCKER_PROBES_EXEC_ENABLED", "false").lower() == "true"
        )
        override_loader = OverrideLoader(
            db=repo,
            suggestions_repo=SuggestionsRepository(repo),
            probe_targets_repo=ProbeTargetsRepository(repo),
            ownership_repo=OverrideOwnershipRepository(repo),
            overrides_dir=overrides_dir,
            exec_enabled_globally=exec_enabled_globally,
            log=log,
            socket_client=getattr(app.state, "docker_socket_client", None),
        )
        # Run one synchronous tick at startup so the API surface sees current
        # ownership + errors before the first 30s sleep elapses.
        try:
            await override_loader.refresh_once()
        except Exception as exc:  # pragma: no cover -- defensive; tolerates dir-missing
            log.warning("lifespan.override_loader_initial_refresh_failed", error=str(exc))
        override_loader.start_task()
        app.state.override_loader = override_loader

    # 7g. Start DrainConsumer periodic task (STAGE-004-026). Env-gated; reuses
    # the existing repo + http_client + vl_url already constructed above.
    from homelab_monitor.kernel.config import (  # noqa: PLC0415
        load_drain_config,
        load_vl_query_limits,
    )
    from homelab_monitor.kernel.db.repositories.app_settings_repository import (  # noqa: PLC0415
        AppSettingsRepository,
    )
    from homelab_monitor.kernel.logs.cycle_status import CycleStatusStore  # noqa: PLC0415
    from homelab_monitor.kernel.logs.drain_consumer import DrainConsumer  # noqa: PLC0415
    from homelab_monitor.kernel.logs.drain_engine import DrainEngine  # noqa: PLC0415
    from homelab_monitor.kernel.logs.drain_persistence import SqlitePersistence  # noqa: PLC0415
    from homelab_monitor.kernel.logs.victorialogs_client import (  # noqa: PLC0415
        VictoriaLogsClient,
    )

    app.state.cycle_status_store = CycleStatusStore()
    drain_config = load_drain_config()
    if drain_config.enabled:
        drain_persistence = SqlitePersistence(repo)
        drain_engine = DrainEngine(drain_persistence)
        drain_vl_limits = load_vl_query_limits()
        drain_vl_client = VictoriaLogsClient(
            vl_url=vl_url,
            http_client=http_client,
            limits=drain_vl_limits,
        )
        from homelab_monitor.kernel.logs.signature_sync import SignatureCatalogSync  # noqa: PLC0415

        drain_sig_sync = SignatureCatalogSync(repo)
        drain_consumer = DrainConsumer(
            vl_client=drain_vl_client,
            engine=drain_engine,
            settings=AppSettingsRepository(repo),
            persistence=drain_persistence,
            config=drain_config,
            metrics_writer=metrics_writer,
            sig_sync=drain_sig_sync,
            log=log,
        )
        drain_consumer.start_task()
        app.state.drain_consumer = drain_consumer

    # 7g (continued). Construct LogWindowFetcher singleton (STAGE-004-031A).
    # This runs UNCONDITIONALLY (not gated by drain_config), reusing the shared
    # vl_url/http_client/load_vl_query_limits already in scope.
    from homelab_monitor.kernel.logs.log_window_fetcher import LogWindowFetcher  # noqa: PLC0415

    log_window_vl_client = VictoriaLogsClient(
        vl_url=vl_url,
        http_client=http_client,
        limits=load_vl_query_limits(),
    )
    app.state.log_window_fetcher = LogWindowFetcher(log_window_vl_client)

    # 7d. One-shot cron-discovery on startup. The scheduler's per-collector
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

    if "image_update_checker" not in degraded:  # pragma: no branch -- always registered
        try:
            await scheduler.await_immediate_run(
                "image_update_checker",
                trigger=TriggerContext(kind="manual", request_id=None),
                timeout=30.0,
            )
            log.info("lifespan.image_update_checker_startup_run_completed")
        except Exception as exc:  # pragma: no cover -- defensive
            log.warning(
                "lifespan.image_update_checker_startup_run_failed",
                error=str(exc),
            )

    if "local_build_update_checker" not in degraded:  # pragma: no branch -- always registered
        try:
            await scheduler.await_immediate_run(
                "local_build_update_checker",
                trigger=TriggerContext(kind="manual", request_id=None),
                timeout=30.0,
            )
            log.info("lifespan.local_build_update_checker_startup_run_completed")
        except Exception as exc:  # pragma: no cover -- defensive
            log.warning(
                "lifespan.local_build_update_checker_startup_run_failed",
                error=str(exc),
            )

    # TODO: if a 3rd collector needs startup-run, promote to a generic
    # run_on_startup: ClassVar[bool] opt-in on BaseCollector
    # (Decision 3B, STAGE-006-020 Design).
    if "pihole_version" not in degraded:  # pragma: no branch -- always registered
        try:
            await scheduler.await_immediate_run(
                "pihole_version",
                trigger=TriggerContext(kind="manual", request_id=None),
                timeout=30.0,
            )
            log.info("lifespan.pihole_version_startup_run_completed")
        except Exception as exc:  # pragma: no cover -- defensive
            log.warning(
                "lifespan.pihole_version_startup_run_failed",
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
    app.state.cron_run_repo = cron_run_repo
    app.state.cron_run_failure_repo = cron_run_failure_repo
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
    app.state.tail_registry = tail_registry
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

    # 8d. User-rules render-on-boot (STAGE-004-043). Mirrors 8c (no reload):
    # vmalert globs the rendered *.yaml within -configCheckInterval (30s). Reads
    # log_user_rules.list_enabled() and rewrites per-rule files in BOTH user-rule
    # dirs atomically (one file per rule — BUG 2: a bad rule only rejects its own
    # file on reload). Failures are logged + swallowed inside render_all.
    from homelab_monitor.kernel.logs.user_rules_render import (  # noqa: PLC0415
        render_all as render_user_rules_on_boot,
    )
    from homelab_monitor.kernel.logs.user_rules_render import (  # noqa: PLC0415
        render_dirs_from_env as user_rules_dirs_from_env,
    )
    from homelab_monitor.kernel.logs.user_rules_repo import (  # noqa: PLC0415
        LogUserRulesRepository,
    )

    user_rules_logs_dir, user_rules_metrics_dir = user_rules_dirs_from_env()
    try:
        await render_user_rules_on_boot(
            LogUserRulesRepository(repo),
            user_rules_logs_dir,
            user_rules_metrics_dir,
        )
    except Exception as exc:  # pragma: no cover -- render_all swallows OSError; defensive
        log.warning("lifespan.user_rules_render_failed", error=str(exc))

    app.state.started_at = utc_now_iso()

    try:
        yield
    finally:
        # Stop DrainConsumer before other services so its in-flight VL stream +
        # engine snapshot cannot race with http_client/engine teardown.
        drain_consumer_handle = getattr(app.state, "drain_consumer", None)
        if drain_consumer_handle is not None:  # pragma: no branch
            await drain_consumer_handle.stop_task()
        # Stop OverrideLoader before discoverer/supervisor so its in-flight
        # tx cannot race with shutdown ownership reads.
        # B1: override_loader is unset when the Docker plugin is disabled.
        override_loader_handle = getattr(app.state, "override_loader", None)
        if override_loader_handle is not None:
            await override_loader_handle.stop_task()
        build_sources_loader_handle = getattr(app.state, "build_sources_loader", None)
        if build_sources_loader_handle is not None:  # pragma: no branch
            await build_sources_loader_handle.stop_task()
        # STAGE-003-010: cancel any in-flight compose actions before scheduler shutdown
        # so subprocess children get SIGTERM via task.cancel().
        # B1: compose_action_runner is unset when the Docker plugin is disabled.
        compose_runner_handle = getattr(app.state, "compose_action_runner", None)
        if compose_runner_handle is not None:
            await compose_runner_handle.shutdown()
        # Stop ProbeSupervisor per-container tasks before scheduler shutdown
        supervisor = getattr(app.state, "probe_supervisor", None)
        if supervisor is not None:  # pragma: no branch — defensive degraded-collector guard
            await supervisor.stop_per_container_tasks()
        # Stop DockerDiscoverer events loop before scheduler shutdown
        discoverer = getattr(app.state, "docker_discoverer", None)
        if discoverer is not None:
            await discoverer.stop_events_loop()
        # Stop image-events task.
        image_events_task_handle = getattr(app.state, "image_events_task", None)
        if image_events_task_handle is not None:  # pragma: no branch -- shutdown of optional task
            image_events_task_handle.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await image_events_task_handle
        # Stop HA entity-registry cache (STAGE-005-037) — before the WS client it uses.
        ha_registry_handle = getattr(app.state, "ha_entity_registry", None)
        if ha_registry_handle is not None:  # pragma: no branch -- always set in full boot
            await ha_registry_handle.stop_task()
        # Stop HA WebSocket client (STAGE-005-002).
        ha_ws_handle = getattr(app.state, "ha_ws_client", None)
        if ha_ws_handle is not None:  # pragma: no branch -- always set in full boot
            await ha_ws_handle.stop_task()
        await scheduler.stop()
        refresh_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await refresh_task
        await vl_writer.aclose()
        with contextlib.suppress(asyncio.CancelledError):
            await flusher_task
        # Best-effort Pi-hole logout (STAGE-006-001) — uses the shared http_client, so
        # it MUST run before that client is closed. aclose() swallows all errors.
        pihole_client = getattr(app.state, "pihole_client", None)
        if pihole_client is not None:
            await pihole_client.aclose()
        # Best-effort Pi-hole RW logout (STAGE-006-018) — same ordering constraint: must
        # run before the shared http_client closes. aclose() swallows all errors.
        pihole_rw_client = getattr(app.state, "pihole_rw_client", None)
        if pihole_rw_client is not None:
            await pihole_rw_client.aclose()
        # Best-effort Synology logout (STAGE-008-001) — uses the dedicated
        # synology_http_client, so it MUST run before that client is closed. aclose()
        # swallows all errors.
        synology_client = getattr(app.state, "synology_client", None)
        if synology_client is not None:
            await synology_client.aclose()
        await http_client.aclose()
        await unifi_http_client.aclose()
        await synology_http_client.aclose()
        docker_client = getattr(app.state, "docker_socket_client", None)
        # B1: docker_socket_client is unset when the Docker plugin is disabled.
        # Otherwise this guard is defensive (degraded DockerSocketCollector path).
        if docker_client is not None:
            await docker_client.aclose()
        await dispose_engine()
