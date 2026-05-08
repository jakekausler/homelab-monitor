"""Health check endpoints."""

from __future__ import annotations

import os
from typing import Literal

from fastapi import APIRouter, Depends
from sqlalchemy import text
from starlette.requests import Request

from homelab_monitor import __version__
from homelab_monitor.kernel.api.dependencies import get_in_memory_metrics_writer_optional
from homelab_monitor.kernel.api.schemas import HealthzResponse, VersionResponse
from homelab_monitor.kernel.db.time import utc_now_iso
from homelab_monitor.kernel.plugins.io import MemoryRetainingMetricsWriter

router = APIRouter()


@router.get("/healthz", response_model=HealthzResponse)
async def healthz(
    request: Request,
    metrics: MemoryRetainingMetricsWriter | None = Depends(get_in_memory_metrics_writer_optional),  # noqa: B008
) -> HealthzResponse:
    """Health check endpoint.

    Returns overall system health plus detailed status fields. Tolerant of
    missing dependencies (lifespan disabled / not yet started) — reports
    degraded state instead of 503-ing.
    """
    # /healthz must report degraded state when the lifespan is disabled (e.g.,
    # OpenAPI export). We tolerate missing app.state fields rather than failing
    # the dependency injection.
    state = request.app.state

    repo = getattr(state, "repo", None)
    scheduler = getattr(state, "scheduler", None)
    failure_budget = getattr(state, "failure_budget", None)
    degraded = getattr(state, "degraded_collectors", []) or []

    db_state: Literal["up", "down"] = "down"
    if repo is not None:
        try:
            await repo.fetch_one(text("SELECT 1"))
            db_state = "up"
        except Exception as exc:
            import structlog  # noqa: PLC0415

            structlog.get_logger().warning("healthz.db_check_failed", error=str(exc))
            db_state = "down"

    scheduler_running = scheduler is not None and scheduler.running

    last_tick_at: str | None = None
    failed_5m = 0
    if metrics is not None:
        last_tick_at = metrics.last_tick_at()
        failed_5m = metrics.failures_in_window(300)

    quarantined: list[str] = []
    if failure_budget is not None:
        quarantined = failure_budget.quarantined_names()

    return HealthzResponse(
        ok=(db_state == "up" and scheduler_running),
        version=__version__,
        db=db_state,
        scheduler="running" if scheduler_running else "stopped",
        last_tick_at=last_tick_at,
        failed_ticks_last_5m=failed_5m,
        quarantined_collectors=quarantined,
        degraded_collectors=list(degraded),
    )


@router.get("/version", response_model=VersionResponse)
async def version(request: Request) -> VersionResponse:
    """Version endpoint. Returns version, git SHA, build timestamp, users_configured."""
    version_str = __version__
    git_sha = os.environ.get("HOMELAB_MONITOR_GIT_SHA", "dev")
    built_at = os.environ.get("HOMELAB_MONITOR_BUILT_AT") or utc_now_iso()
    auth_repo = getattr(request.app.state, "auth_repo", None)
    users_configured = False
    if auth_repo is not None:
        try:
            users_configured = await auth_repo.users_count() > 0
        except Exception as exc:  # pragma: no cover -- defensive
            import structlog  # noqa: PLC0415

            structlog.get_logger().warning("version.users_count_failed", error=str(exc))
            users_configured = False
    return VersionResponse(
        version=version_str,
        git_sha=git_sha,
        built_at=built_at,
        users_configured=users_configured,
    )
