"""Pydantic response models for API endpoints."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

# Re-export the canonical error envelope from errors.py so external callers
# can import either `from kernel.api.schemas` or `from kernel.api.errors`.
from homelab_monitor.kernel.api.errors import ErrorEnvelope, ErrorPayload

__all__ = [
    "CollectorStatus",
    "ErrorEnvelope",
    "ErrorPayload",
    "HealthzResponse",
    "MetricsSnapshotEntry",
    "MetricsSnapshotResponse",
    "RetryResponse",
    "VersionResponse",
]


class HealthzResponse(BaseModel):
    """Response for GET /api/healthz."""

    model_config = ConfigDict(extra="forbid")
    ok: bool
    version: str
    db: Literal["up", "down"]
    scheduler: Literal["running", "stopped"]
    last_tick_at: str | None
    failed_ticks_last_5m: int
    quarantined_collectors: list[str]
    degraded_collectors: list[str]


class VersionResponse(BaseModel):
    """Response for GET /api/version."""

    model_config = ConfigDict(extra="forbid")
    version: str
    git_sha: str
    built_at: str
    users_configured: bool


class CollectorStatus(BaseModel):
    """Status of a single collector in GET /api/collectors response."""

    model_config = ConfigDict(extra="forbid")
    name: str
    status: Literal["healthy", "quarantined", "degraded"]
    last_run: str | None
    last_error: str | None
    quarantined: bool
    quarantined_at: str | None
    quarantine_reason: str | None
    next_run: str | None
    run_kind: str
    interval_seconds: float
    consecutive_failures: int


class RetryResponse(BaseModel):
    """Response for POST /api/collectors/{name}/retry."""

    model_config = ConfigDict(extra="forbid")
    name: str
    tick_id: str
    requested_at: str


class MetricsSnapshotEntry(BaseModel):
    """Single metric entry in a snapshot."""

    model_config = ConfigDict(extra="forbid")
    name: str
    value: float
    labels: dict[str, str]
    kind: Literal["gauge", "counter", "summary"]
    ts: str  # ISO-8601 UTC of the most recent write for this (name, labels)


class MetricsSnapshotResponse(BaseModel):
    """Response for GET /api/metrics/snapshot."""

    model_config = ConfigDict(extra="forbid")
    ts: str  # snapshot capture time (ISO-8601 UTC)
    entries: list[MetricsSnapshotEntry]
