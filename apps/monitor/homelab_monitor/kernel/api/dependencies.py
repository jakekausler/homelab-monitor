"""FastAPI dependency injection helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, TypeVar

from starlette.requests import Request

from homelab_monitor.kernel.api.errors import DependencyUnavailableProblem

if TYPE_CHECKING:
    import httpx

    from homelab_monitor.kernel.api.sse import SseBroker
    from homelab_monitor.kernel.db.repository import SqliteRepository
    from homelab_monitor.kernel.plugins.io import MetricsWriter
    from homelab_monitor.kernel.plugins.loader import PluginLoader
    from homelab_monitor.kernel.scheduler.failure_budget import FailureBudget
    from homelab_monitor.kernel.scheduler.scheduler import Scheduler


T = TypeVar("T")


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


def get_http_client(request: Request) -> httpx.AsyncClient:
    """Get the HTTP client from app state."""
    return _require_state(
        request,
        attr="http_client",
        code="http_client_unavailable",
        message="http client is not initialized",
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


def require_dev_auth(request: Request) -> str:
    """Require dev auth and return actor identity.

    Auth gating happens in DevAuthMiddleware; this dependency is a hook
    for STAGE-001-011 to inject real auth. Currently always returns "dev".
    """
    return "dev"
