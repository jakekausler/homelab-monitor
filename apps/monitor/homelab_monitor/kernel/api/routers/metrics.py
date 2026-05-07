"""Metrics snapshot endpoint — returns the in-memory writer's latest values."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from homelab_monitor.kernel.api.dependencies import get_metrics_writer, require_session
from homelab_monitor.kernel.api.schemas import (
    MetricsSnapshotEntry,
    MetricsSnapshotResponse,
)
from homelab_monitor.kernel.auth.models import User
from homelab_monitor.kernel.db.time import utc_now_iso
from homelab_monitor.kernel.plugins.io import MemoryRetainingMetricsWriter

router = APIRouter()


@router.get("/metrics/snapshot", response_model=MetricsSnapshotResponse)
async def metrics_snapshot(
    _user: User = Depends(require_session()),  # noqa: B008
    writer: MemoryRetainingMetricsWriter = Depends(get_metrics_writer),  # noqa: B008
) -> MetricsSnapshotResponse:
    """Return the in-memory writer's latest-value snapshot.

    Auth: cookie session required. CSRF NOT enforced on GET.

    SCAFFOLDING: when a real VictoriaMetrics-backed writer ships in STAGE-001-015,
    this endpoint will be retired in favor of direct VM queries. STAGE-014's
    Overview tile is the only consumer.
    """
    entries = [
        MetricsSnapshotEntry(
            name=e.name,
            value=e.value,
            labels=e.labels,
            kind=e.kind,
            ts=e.ts,
        )
        for e in writer.snapshot()
    ]
    return MetricsSnapshotResponse(ts=utc_now_iso(), entries=entries)
