"""FastAPI dependency injection helpers."""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from starlette.requests import Request

from homelab_monitor.kernel.api.errors import DependencyUnavailableProblem
from homelab_monitor.kernel.auth.csrf import verify_csrf_token
from homelab_monitor.kernel.auth.errors import (
    CsrfMismatchProblem,
    InsufficientScopeProblem,
    UnauthenticatedProblem,
)
from homelab_monitor.kernel.auth.models import ApiToken, User
from homelab_monitor.kernel.auth.rate_limit import LoginRateLimiter
from homelab_monitor.kernel.auth.repository import AuthRepository
from homelab_monitor.kernel.auth.scopes import Scope, parse_scopes

if TYPE_CHECKING:
    import httpx
    from prometheus_client import CollectorRegistry

    from homelab_monitor.kernel.alerts.repository import AlertRepository
    from homelab_monitor.kernel.api.sse import SseBroker
    from homelab_monitor.kernel.backup.service import BackupService
    from homelab_monitor.kernel.cron.repository import CronRepo
    from homelab_monitor.kernel.cron.run_repository import CronRunRepository
    from homelab_monitor.kernel.db.repository import SqliteRepository
    from homelab_monitor.kernel.dispatch.dispatcher import AlertDispatcher
    from homelab_monitor.kernel.ha.client import HomeAssistantRestClient
    from homelab_monitor.kernel.ha.websocket import HomeAssistantWebsocketClient
    from homelab_monitor.kernel.heartbeat.repository import HeartbeatRepo
    from homelab_monitor.kernel.logs.cron_run_failure_enrichments_repo import (
        CronRunFailureEnrichmentsRepository,
    )
    from homelab_monitor.kernel.logs.cycle_status import CycleStatusStore
    from homelab_monitor.kernel.logs.drain_consumer import DrainConsumer
    from homelab_monitor.kernel.logs.log_window_fetcher import LogWindowFetcher
    from homelab_monitor.kernel.logs.tail_service import TailRegistry
    from homelab_monitor.kernel.plugins.io import (
        InMemoryLogsWriter,
        LogsWriter,
        MemoryRetainingMetricsWriter,
        MetricsWriter,
    )
    from homelab_monitor.kernel.plugins.loader import PluginLoader
    from homelab_monitor.kernel.scheduler.failure_budget import FailureBudget
    from homelab_monitor.kernel.scheduler.scheduler import Scheduler
    from homelab_monitor.plugins.collectors.builtin.log_stream_budget import LogStreamState


def _require_state(request: Request, *, attr: str, code: str, message: str) -> Any:  # noqa: ANN401 -- generic state object, type varies per dependency
    """Fetch attr from app.state or raise 503 with uniform code/message."""
    val = getattr(request.app.state, attr, None)
    if val is None:
        raise DependencyUnavailableProblem(
            status_code=503,
            code=code,
            message=message,
        )
    return val


def get_scheduler(request: Request) -> Scheduler:
    """Get the scheduler from app state."""
    return _require_state(
        request,
        attr="scheduler",
        code="scheduler_unavailable",
        message="scheduler is not running (lifespan disabled)",
    )


def get_repo(request: Request) -> SqliteRepository:
    """Get the repository from app state."""
    return _require_state(
        request,
        attr="repo",
        code="database_unavailable",
        message="database is not initialized",
    )


def get_broker(request: Request) -> SseBroker:
    """Get the SSE broker from app state."""
    return _require_state(
        request,
        attr="broker",
        code="broker_unavailable",
        message="event broker is not initialized",
    )


def get_loader(request: Request) -> PluginLoader:
    """Get the plugin loader from app state."""
    return _require_state(
        request,
        attr="loader",
        code="loader_unavailable",
        message="plugin loader is not initialized",
    )


def get_metrics_writer(request: Request) -> MetricsWriter:
    """Get the metrics writer from app state."""
    return _require_state(
        request,
        attr="metrics_writer",
        code="metrics_unavailable",
        message="metrics writer is not initialized",
    )


def get_in_memory_metrics_writer(request: Request) -> MemoryRetainingMetricsWriter:
    """Get the in-memory (snapshot-capable) metrics writer from app state."""
    return _require_state(
        request,
        attr="in_memory_metrics_writer",
        code="metrics_unavailable",
        message="in-memory metrics writer is not initialized",
    )


def get_in_memory_metrics_writer_optional(
    request: Request,
) -> MemoryRetainingMetricsWriter | None:
    """Return the in-memory metrics writer, or ``None`` if lifespan disabled."""
    return getattr(request.app.state, "in_memory_metrics_writer", None)


def get_prom_registry(request: Request) -> CollectorRegistry:
    """Get the prometheus_client CollectorRegistry from app state."""
    return _require_state(
        request,
        attr="prom_registry",
        code="metrics_unavailable",
        message="prometheus registry is not initialized",
    )


def get_vm_url() -> str:
    """Return the VictoriaMetrics base URL from env, defaulting to the compose hostname."""
    return os.environ.get("HOMELAB_MONITOR_VM_URL", "http://victoriametrics:8428")


def get_http_client(request: Request) -> httpx.AsyncClient:
    """Get the HTTP client from app state."""
    return _require_state(
        request,
        attr="http_client",
        code="http_client_unavailable",
        message="http client is not initialized",
    )


def get_ha_ws_client(request: Request) -> HomeAssistantWebsocketClient:
    """Get the Home Assistant websocket client from app state."""
    return _require_state(
        request,
        attr="ha_ws_client",
        code="ha_ws_client_unavailable",
        message="ha websocket client is not initialized",
    )


def get_ha_client(request: Request) -> HomeAssistantRestClient:
    """Get the Home Assistant REST client from app state (STAGE-005-031)."""
    return _require_state(
        request,
        attr="ha_client",
        code="ha_client_unavailable",
        message="ha rest client is not initialized",
    )


def get_started_at(request: Request) -> str:
    """Get the startup timestamp from app state."""
    return _require_state(
        request,
        attr="started_at",
        code="state_unavailable",
        message="startup timestamp not available",
    )


def get_degraded_collectors(request: Request) -> list[str]:
    """Get the list of degraded collectors from app state."""
    return _require_state(
        request,
        attr="degraded_collectors",
        code="state_unavailable",
        message="degraded collectors list not available",
    )


def get_failure_budget(request: Request) -> FailureBudget:
    """Get the failure budget from app state."""
    return _require_state(
        request,
        attr="failure_budget",
        code="failure_budget_unavailable",
        message="failure budget is not initialized",
    )


def get_auth_repo(request: Request) -> AuthRepository:
    """Get the auth repository from app state."""
    return _require_state(
        request,
        attr="auth_repo",
        code="auth_unavailable",
        message="auth subsystem is not initialized",
    )


def get_rate_limiter(request: Request) -> LoginRateLimiter:
    """Get the login rate limiter from app state."""
    return _require_state(
        request,
        attr="login_rate_limiter",
        code="auth_unavailable",
        message="login rate limiter is not initialized",
    )


def get_master_key(request: Request) -> bytes:
    """Get the master key from app state (used by login route to mint cookies)."""
    return _require_state(
        request,
        attr="master_key",
        code="master_key_unavailable",
        message="master key is not loaded",
    )


def get_alert_repo(request: Request) -> AlertRepository:
    """Get the alert repository from app state."""
    return _require_state(
        request,
        attr="alert_repo",
        code="alert_repo_unavailable",
        message="alert repository is not initialized",
    )


def get_alert_dispatcher(request: Request) -> AlertDispatcher:
    """Get the alert dispatcher from app state."""
    return _require_state(
        request,
        attr="alert_dispatcher",
        code="alert_dispatcher_unavailable",
        message="alert dispatcher is not initialized",
    )


def get_heartbeat_repo(request: Request) -> HeartbeatRepo:
    """Get the heartbeat repository from app state."""
    return _require_state(
        request,
        attr="heartbeat_repo",
        code="heartbeat_repo_unavailable",
        message="heartbeat repository is not initialized",
    )


def get_cron_repo(request: Request) -> CronRepo:  # type: ignore[name-defined]
    """Get the cron repository from app state (STAGE-002-002)."""
    return _require_state(
        request,
        attr="cron_repo",
        code="cron_repo_unavailable",
        message="cron repository is not initialized",
    )


def get_cron_run_repo(request: Request) -> CronRunRepository:  # type: ignore[name-defined]
    """Get the cron run repository from app state (STAGE-002-011)."""
    return _require_state(
        request,
        attr="cron_run_repo",
        code="cron_run_repo_unavailable",
        message="cron run repository is not initialized",
    )


def get_cron_run_failure_repo(
    request: Request,
) -> CronRunFailureEnrichmentsRepository:  # type: ignore[name-defined]
    """Get the cron run failure-enrichment repository from app state (STAGE-004-034)."""
    return _require_state(
        request,
        attr="cron_run_failure_repo",
        code="cron_run_failure_repo_unavailable",
        message="cron run failure repository is not initialized",
    )


def get_backup_service(request: Request) -> BackupService:
    """Get the backup service from app state."""
    return _require_state(
        request,
        attr="backup_service",
        code="backup_service_unavailable",
        message="backup service is not initialized",
    )


def get_logs_writer(request: Request) -> LogsWriter:
    """Get the multiplex logs writer from app state."""
    return _require_state(
        request,
        attr="logs_writer",
        code="logs_unavailable",
        message="logs writer is not initialized",
    )


def get_in_memory_logs_writer(request: Request) -> InMemoryLogsWriter:
    """Get the in-memory logs writer (test/snapshot path) from app state."""
    return _require_state(
        request,
        attr="in_memory_logs_writer",
        code="logs_unavailable",
        message="in-memory logs writer is not initialized",
    )


def get_vl_url() -> str:
    """Return the VictoriaLogs base URL from env, defaulting to compose hostname."""
    return os.environ.get("HOMELAB_MONITOR_VL_URL", "http://victorialogs:9428")


def get_log_stream_state(request: Request) -> LogStreamState:
    """Get the per-stream summary state map (updated by the budget collector)."""
    return _require_state(
        request,
        attr="log_stream_state",
        code="logs_unavailable",
        message="log stream state not initialized",
    )


def get_tail_registry(request: Request) -> TailRegistry:
    """Get the live-tail connection registry from app state."""
    return _require_state(
        request,
        attr="tail_registry",
        code="tail_unavailable",
        message="tail registry is not initialized",
    )


def get_drain_consumer(request: Request) -> DrainConsumer:
    """Get the drain consumer from app state (503 when drain disabled/absent)."""
    return _require_state(
        request,
        attr="drain_consumer",
        code="drain_unavailable",
        message="drain consumer is not running (drain disabled)",
    )


def get_cycle_status_store(request: Request) -> CycleStatusStore:
    """Get the manual-cycle status store from app state."""
    return _require_state(
        request,
        attr="cycle_status_store",
        code="cycle_status_unavailable",
        message="cycle status store is not initialized",
    )


def get_log_window_fetcher(request: Request) -> LogWindowFetcher:
    """Get the LogWindowFetcher singleton from app state (STAGE-004-031A)."""
    return _require_state(
        request,
        attr="log_window_fetcher",
        code="logs_unavailable",
        message="log window fetcher is not initialized",
    )


CSRF_HEADER = "X-CSRF-Token"
STATE_CHANGING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _enforce_csrf(request: Request) -> None:
    """Compare X-CSRF-Token header to session.csrf_token. Raises on mismatch.

    Only invoked for cookie-authenticated state-changing requests. Token-authed
    requests are CSRF-immune (no ambient credential).
    """
    if request.method.upper() not in STATE_CHANGING_METHODS:
        return
    session = getattr(request.state, "session", None)
    if session is None:
        # Defensive: only called from require_session() after user was resolved
        raise CsrfMismatchProblem(  # pragma: no cover -- callable only when session set
            message="missing session for CSRF check",
        )
    provided = request.headers.get(CSRF_HEADER, "")
    if not verify_csrf_token(provided, session.csrf_token):
        raise CsrfMismatchProblem()


def require_session() -> Callable[..., User]:
    """FastAPI dependency factory: require a session-authed User; enforce CSRF.

    Use as: `Depends(require_session())`. Raises UnauthenticatedProblem if not
    session-authed; CsrfMismatchProblem on state-changing requests with a bad
    or missing X-CSRF-Token header.
    """

    def _dep(request: Request) -> User:
        if request.state.auth_kind != "session" or request.state.user is None:
            raise UnauthenticatedProblem()
        _enforce_csrf(request)
        return request.state.user  # pyright: ignore[reportReturnType]

    return _dep


def require_session_no_csrf() -> Callable[..., User]:
    """FastAPI dependency factory: require session auth WITHOUT CSRF enforcement.

    Reserved for routes that proxy to a sandboxed iframe (Karma, STAGE-001-019;
    Grafana, STAGE-001-020) where the embedded UI cannot read our session-bound
    CSRF token to attach as a header. Callers MUST implement an alternative
    anti-CSRF check (e.g., an Origin/Referer same-origin assertion) on
    state-changing methods.

    Do NOT add new callers without explicit security review; this is a
    deliberate exception, not a general convenience.

    Currently permitted callers (each substitutes Origin/Referer same-origin
    enforcement for CSRF protection):
      - `karma.py::karma_proxy` (STAGE-001-019)
      - `grafana.py::grafana_proxy` (STAGE-001-020)
    Adding new callers requires explicit security review and a
    code-comment update here listing the new callsite.
    """

    def _dep(request: Request) -> User:
        if request.state.auth_kind != "session" or request.state.user is None:
            raise UnauthenticatedProblem()
        return request.state.user  # pyright: ignore[reportReturnType]

    return _dep


def require_token_scope(scope: Scope) -> Callable[..., ApiToken]:
    """FastAPI dependency factory: require an API token with the named scope."""

    def _dep(request: Request) -> ApiToken:
        if request.state.auth_kind != "token" or request.state.token is None:
            raise UnauthenticatedProblem()
        token: ApiToken = request.state.token
        granted = parse_scopes(token.scopes)
        if scope not in granted:
            raise InsufficientScopeProblem(
                message=f"token lacks required scope: {scope.value}",
            )
        return token

    return _dep


def require_user_or_token(scopes: set[Scope]) -> Callable[..., User | ApiToken]:
    """FastAPI dependency factory: accept session OR token with ANY of the given scopes.

    For session auth: enforces CSRF on state-changing requests.
    For token auth: validates ANY (not all) scope membership.
    """

    def _dep(request: Request) -> User | ApiToken:
        kind = request.state.auth_kind
        if kind == "session" and request.state.user is not None:
            _enforce_csrf(request)
            return request.state.user  # pyright: ignore[reportReturnType]
        if kind == "token" and request.state.token is not None:
            token: ApiToken = request.state.token
            granted = parse_scopes(token.scopes)
            if scopes.isdisjoint(granted):
                raise InsufficientScopeProblem(
                    message=(
                        f"token lacks any of required scopes: {sorted(s.value for s in scopes)}"
                    ),
                )
            return token
        raise UnauthenticatedProblem()

    return _dep
