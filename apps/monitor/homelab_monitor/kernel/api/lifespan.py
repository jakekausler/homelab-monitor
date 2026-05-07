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
from structlog.stdlib import BoundLogger

from homelab_monitor.kernel.api.sse import SseBroker
from homelab_monitor.kernel.db.engine import dispose_engine, get_engine
from homelab_monitor.kernel.db.migrations import MigrationsPendingError, run_migrations
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.db.time import utc_now_iso
from homelab_monitor.kernel.logging import configure_logging
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


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:  # noqa: PLR0915  -- spec-mandated bootstrap sequence
    """Bootstrap sequence with hybrid abort/degrade per D1."""
    configure_logging()
    log = cast(BoundLogger, structlog.get_logger().bind(component="lifespan"))

    # 1. Master key
    try:
        master_key = load_master_key()
    except MasterKeyError as exc:
        _critical_abort(log, "lifespan.master_key_missing", error=str(exc))

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
    metrics_writer = MemoryRetainingMetricsWriter()
    logs_writer = InMemoryLogsWriter()
    ssh_factory = _StubSshFactory()
    broker = SseBroker(log)
    failure_budget = FailureBudget(repo, log)

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

    scheduler = Scheduler(
        loader.load_all(),
        ctx_factory,
        metrics_writer,
        SchedulerConfig(event_sink=broker),
        failure_budget=failure_budget,
    )
    await scheduler.start()

    app.state.master_key = master_key
    app.state.auth_repo = auth_repo
    app.state.login_rate_limiter = login_rate_limiter
    app.state.scheduler = scheduler
    app.state.repo = repo
    app.state.broker = broker
    app.state.ttl_resolver = ttl_resolver
    app.state.http_client = http_client
    app.state.metrics_writer = metrics_writer
    app.state.logs_writer = logs_writer
    app.state.loader = loader
    app.state.failure_budget = failure_budget
    app.state.degraded_collectors = degraded

    app.state.started_at = utc_now_iso()

    try:
        yield
    finally:
        await scheduler.stop()
        refresh_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await refresh_task
        await http_client.aclose()
        await dispose_engine()
