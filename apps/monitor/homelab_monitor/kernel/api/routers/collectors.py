"""Collector management endpoints."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends
from starlette.requests import Request

from homelab_monitor.kernel.api.dependencies import (
    get_loader,
    get_metrics_writer,
    get_scheduler,
    require_session,
)
from homelab_monitor.kernel.api.errors import NotFoundProblem
from homelab_monitor.kernel.api.schemas import CollectorStatus, RetryResponse
from homelab_monitor.kernel.auth.models import User
from homelab_monitor.kernel.db.time import utc_now_iso
from homelab_monitor.kernel.events import TriggerContext
from homelab_monitor.kernel.plugins.io import InMemoryMetricsWriter
from homelab_monitor.kernel.plugins.loader import PluginLoader
from homelab_monitor.kernel.plugins.types import PLUGIN_NAME_PATTERN

PLUGIN_NAME_PATTERN_RE = re.compile(PLUGIN_NAME_PATTERN)

if TYPE_CHECKING:
    from homelab_monitor.kernel.scheduler.scheduler import Scheduler

router = APIRouter()


@router.get("/collectors", response_model=list[CollectorStatus])
async def list_collectors(
    _user: User = Depends(require_session()),  # noqa: B008
    loader: PluginLoader = Depends(get_loader),  # noqa: B008
    metrics: InMemoryMetricsWriter = Depends(get_metrics_writer),  # noqa: B008
    scheduler: Scheduler = Depends(get_scheduler),  # noqa: B008
) -> list[CollectorStatus]:
    """List all loaded collectors with their status."""
    fb = scheduler.failure_budget
    results: list[CollectorStatus] = []
    for lc in loader.load_all():
        config = lc.config
        collector = lc.collector
        name = config.name

        # Determine status
        is_quarantined = fb is not None and fb.is_quarantined(name)
        degraded_set = fb.degraded_names() if fb is not None else []
        status = (
            "quarantined" if is_quarantined else ("degraded" if name in degraded_set else "healthy")
        )

        # Get last run timestamp
        last_run = metrics.last_tick_at_for(name)

        # Get last error
        last_error = metrics.last_error_for(name)

        # Get quarantine details
        quarantined_at = None
        quarantine_reason = None
        if fb is not None and is_quarantined:  # pragma: no cover -- requires collected failures
            q_info = fb.quarantine_state(name)
            if q_info is not None:  # pragma: no cover -- defensive fallback
                quarantined_at = q_info.quarantined_at
                quarantine_reason = q_info.quarantine_reason

        # Calculate next_run
        next_run = None
        if last_run is not None:
            interval_s = config.interval_seconds
            last_run_dt = datetime.fromisoformat(last_run.replace("Z", "+00:00"))

            now_utc = datetime.now(UTC)
            next_run_dt = max(last_run_dt + timedelta(seconds=interval_s), now_utc)
            next_run = next_run_dt.isoformat().replace("+00:00", "Z")

        cf_count = fb.consecutive_failures(name) if fb is not None else 0

        results.append(
            CollectorStatus(
                name=name,
                status=status,
                last_run=last_run,
                last_error=last_error,
                quarantined=is_quarantined,
                quarantined_at=quarantined_at,
                quarantine_reason=quarantine_reason,
                next_run=next_run,
                run_kind=collector.run_kind.value,
                interval_seconds=config.interval_seconds,
                consecutive_failures=cf_count,
            )
        )

    return results


@router.post("/collectors/{name}/retry", response_model=RetryResponse)
async def retry_collector(
    name: str,
    request: Request,
    user: User = Depends(require_session()),  # noqa: B008
    scheduler: Scheduler = Depends(get_scheduler),  # noqa: B008
    loader: PluginLoader = Depends(get_loader),  # noqa: B008
) -> RetryResponse:
    """Request an immediate retry of a collector.

    Clears any quarantine, enqueues an immediate run, and returns the tick_id.
    """
    # Validate name format
    if not PLUGIN_NAME_PATTERN_RE.match(name):
        raise NotFoundProblem(
            message=f"collector not found: {name}",
        )

    # Check collector exists
    loaded_names = {lc.config.name for lc in loader.load_all()}
    if name not in loaded_names:
        raise NotFoundProblem(
            message=f"collector not found: {name}",
        )

    # Clear quarantine
    await scheduler.clear_quarantine(name, by=str(user.id))

    # Request immediate run
    tick_id = await scheduler.request_immediate_run(
        name,
        trigger=TriggerContext(kind="retry", request_id=request.state.request_id),
    )

    return RetryResponse(
        name=name,
        tick_id=tick_id,
        requested_at=utc_now_iso(),
    )
