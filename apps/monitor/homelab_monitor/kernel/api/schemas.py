"""Pydantic response models for API endpoints."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from homelab_monitor.kernel.alerts.types import AlertOutcome, AlertStatus, Severity

# Re-export the canonical error envelope from errors.py so external callers
# can import either `from kernel.api.schemas` or `from kernel.api.errors`.
from homelab_monitor.kernel.api.errors import ErrorEnvelope, ErrorPayload

__all__ = [
    "AckResponse",
    "AlertDetailResponse",
    "AlertListResponse",
    "AlertView",
    "CollectorStatus",
    "DismissResponse",
    "ErrorEnvelope",
    "ErrorPayload",
    "HealthzResponse",
    "IngestResponse",
    "MetricsSnapshotEntry",
    "MetricsSnapshotResponse",
    "OutcomeView",
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


class AlertView(BaseModel):
    """Public projection of an Alert row for API responses."""

    model_config = ConfigDict(extra="forbid")
    id: str
    fingerprint: str
    source_tool: str
    severity: Severity
    status: AlertStatus
    opened_at: str
    last_seen_at: str
    resolved_at: str | None = None
    ack_at: str | None = None
    ack_by: int | None = None
    runbook_id: str | None = None
    labels: dict[str, str]
    annotations: dict[str, str]


class OutcomeView(BaseModel):
    """One alert_outcomes row in API responses."""

    model_config = ConfigDict(extra="forbid")
    outcome: AlertOutcome
    decided_at: str
    decided_by: int | None = None


class AlertListResponse(BaseModel):
    """Response for GET /api/alerts.

    NOTE: ``total`` is intentionally omitted; cursor pagination would require a
    separate COUNT query. The frontend can infer "has more" from
    ``next_cursor is not None``.
    """

    model_config = ConfigDict(extra="forbid")
    items: list[AlertView]
    next_cursor: str | None = None


class AlertDetailResponse(BaseModel):
    """Response for GET /api/alerts/{id}."""

    model_config = ConfigDict(extra="forbid")
    alert: AlertView
    outcomes: list[OutcomeView]
    payload: dict[str, Any]


class IngestResponse(BaseModel):
    """Response for POST /api/alerts/ingest."""

    model_config = ConfigDict(extra="forbid")
    received: int
    ingested: int


class AckResponse(BaseModel):
    """Response for POST /api/alerts/{id}/ack."""

    model_config = ConfigDict(extra="forbid")
    alert_id: str
    ack_at: str


class DismissResponse(BaseModel):
    """Response for POST /api/alerts/{id}/dismiss."""

    model_config = ConfigDict(extra="forbid")
    alert_id: str
    dismissed_at: str
