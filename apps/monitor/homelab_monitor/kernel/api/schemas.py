"""Pydantic response models for API endpoints."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from homelab_monitor.kernel.alerts.types import AlertOutcome, AlertStatus, Severity

# Re-export the canonical error envelope from errors.py so external callers
# can import either `from kernel.api.schemas` or `from kernel.api.errors`.
from homelab_monitor.kernel.api.errors import ErrorEnvelope, ErrorPayload
from homelab_monitor.kernel.logs.models import LogLine

__all__ = [
    "AckResponse",
    "AlertDetailResponse",
    "AlertListResponse",
    "AlertView",
    "BackupResponse",
    "CollectorStatus",
    "DismissResponse",
    "ErrorEnvelope",
    "ErrorPayload",
    "FieldDescriptor",
    "HealthzResponse",
    "HistogramBucket",
    "IngestResponse",
    "LogsFieldsResponse",
    "LogsHistogramResponse",
    "LogsQueryResponse",
    "LogsRetentionResponse",
    "LogsRetentionUpdateRequest",
    "LogsServicesResponse",
    "LogsStreamSummary",
    "LogsStreamsResponse",
    "MetricsRangeResponse",
    "MetricsSnapshotEntry",
    "MetricsSnapshotResponse",
    "OutcomeView",
    "RetryResponse",
    "SaveQueryCreateRequest",
    "SaveQueryRenameRequest",
    "SavedQueriesListResponse",
    "SavedQueryResponse",
    "SavedServiceIdentity",
    "ServiceCount",
    "VMRangeData",
    "VMRangeResult",
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


class VMRangeResult(BaseModel):
    """One series in a VictoriaMetrics ``/api/v1/query_range`` response."""

    model_config = ConfigDict(extra="forbid")
    metric: dict[str, str]
    # Each pair is [unix_timestamp_seconds_float, value_as_string]
    values: list[list[float | str]]


class VMRangeData(BaseModel):
    """``data`` field of a VictoriaMetrics range response."""

    model_config = ConfigDict(extra="forbid")
    resultType: str  # mirrors VM JSON key
    result: list[VMRangeResult]


class MetricsRangeResponse(BaseModel):
    """Response for GET /api/metrics/range — passes through VM's range shape."""

    model_config = ConfigDict(extra="forbid")
    status: Literal["success", "error"]
    data: VMRangeData


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


class BackupResponse(BaseModel):
    """Response shape for POST /api/admin/backup.

    Note: errors is a list[str] (best-effort partial-success pattern)
    rather than the standard ErrorEnvelope. Backups proceed even when
    one component (SQLite or VM) fails; the errors list captures
    component-specific failures while still returning the partial
    paths that succeeded.
    """

    model_config = ConfigDict(extra="forbid")
    snapshot_id: str
    sqlite_path: str | None = None
    vm_snapshot_path: str | None = None
    started_at: str
    ended_at: str
    size_bytes: int
    errors: list[str]


class LogsQueryResponse(BaseModel):
    """Response for GET /api/logs/query."""

    model_config = ConfigDict(extra="forbid")
    lines: list[LogLine]
    next_cursor: str | None = None
    has_more: bool = False


class LogsStreamSummary(BaseModel):
    """Per-stream byte/rate summary for the streams panel."""

    model_config = ConfigDict(extra="forbid")
    host: str
    service: str
    last_seen: str
    lines_per_sec: float
    bytes_today: int


class LogsStreamsResponse(BaseModel):
    """Response for GET /api/logs/streams."""

    model_config = ConfigDict(extra="forbid")
    streams: list[LogsStreamSummary]


class LogsRetentionResponse(BaseModel):
    """Response for GET/PATCH /api/settings/logs/retention (STAGE-004-022)."""

    model_config = ConfigDict(extra="forbid")
    retention_days: int = Field(description="EFFECTIVE retention (days) VL is running")
    pending_retention_days: int | None = Field(
        default=None,
        description="Desired retention awaiting restart, or null if none pending",
    )
    disk_used_gb: float = Field(description="VL data-dir usage in GiB")
    disk_used_pct: float = Field(description="VL usage as percent of the VL disk budget")
    disk_budget_available: bool = Field(
        description="False when budget config is missing or malformed; True otherwise"
    )
    warn_pct: int = Field(description="Warn threshold (percent of VL budget)")
    crit_pct: int = Field(description="Critical threshold (percent of VL budget)")
    retention_source: Literal["env", "runtime", "default"] = Field(
        description="Where the effective/pending value originates"
    )
    restart_required: bool = Field(
        description="True iff a pending retention differs from effective"
    )


class LogsRetentionUpdateRequest(BaseModel):
    """Request body for PATCH /api/settings/logs/retention."""

    model_config = ConfigDict(extra="forbid")
    retention_days: int = Field(ge=1, le=365, description="Desired retention in days")


class ServiceCount(BaseModel):
    """One distinct (service, source_type) identity + its line count over the window.

    STAGE-004-012A: identity is the PAIR (service, source_type). The same service
    name may appear under multiple source_types (e.g. a name that is both a docker
    container and a systemd unit) → ONE ServiceCount per (service, source_type) pair.
    """

    model_config = ConfigDict(extra="forbid")
    service: str
    source_type: str
    count: int


class LogsServicesResponse(BaseModel):
    """Response for GET /api/logs/services.

    `services` is sorted DESC by count over (service, source_type) identities.
    `truncated` is True when the number of distinct identities exceeded the
    requested `limit` (only the top `limit` are returned).
    """

    model_config = ConfigDict(extra="forbid")
    services: list[ServiceCount]
    truncated: bool


class FieldDescriptor(BaseModel):
    """One discovered field in the current query scope (STAGE-004-018).

    `name` is the dotted field path (e.g. ``json.context.user_id``). `coverage`
    is the EXACT fraction of matching lines that carry this field, derived from
    VictoriaLogs ``field_names`` hit counts (field hits / total ``_msg`` hits).
    `sample_values` are up to K distinct stringified values seen in the bounded
    most-recent sample (first-seen order); a rare field with accurate coverage
    but no value in the sample yields an empty list + ``type_hint="unknown"``.
    """

    model_config = ConfigDict(extra="forbid")
    name: str = Field(description="Dotted field path, e.g. json.context.user_id")
    sample_values: list[str] = Field(
        description="Up to K distinct example values seen in the sample (first-seen order)"
    )
    coverage: float = Field(
        ge=0.0,
        le=1.0,
        description="Exact fraction (0..1) of matching lines carrying this field",
    )
    type_hint: str = Field(description="numeric | bool | string | object | array | mixed | unknown")


class LogsFieldsResponse(BaseModel):
    """Response for GET /api/logs/fields (STAGE-004-018).

    `fields` is sorted DESC by coverage, tie-broken by name ASC. `sampled_lines`
    is the actual number of lines the value/type sample was drawn from.
    `truncated` is True when the sample hit the requested cap (more lines existed
    than were sampled).
    """

    model_config = ConfigDict(extra="forbid")
    fields: list[FieldDescriptor]
    sampled_lines: int
    truncated: bool


class HistogramBucket(BaseModel):
    """One time-bucket in the logs density histogram (STAGE-004-019).

    `start_ts` is the bucket-start ISO-8601 UTC timestamp — the click target the
    frontend uses to narrow the range to [start_ts, start_ts + bucket_duration_ms).
    `counts_by_severity` always carries all three coarse keys (error/warn/info),
    zeros included, so the stacked chart has no gaps. `total` is their sum.
    """

    model_config = ConfigDict(extra="forbid")
    start_ts: str = Field(description="Bucket-start ISO-8601 UTC timestamp (the click target)")
    counts_by_severity: dict[str, int] = Field(
        description="Coarse severity counts; always keys error/warn/info (zeros included)"
    )
    total: int = Field(description="Sum of counts_by_severity for this bucket")


class LogsHistogramResponse(BaseModel):
    """Response for GET /api/logs/histogram (STAGE-004-019).

    `buckets` is time-ascending, the START-aligned buckets covering [start, end]
    (may be buckets + 1 due to VL's inclusive end). `bucket_duration_ms` is the
    bucket width; the frontend derives each bar's click window as
    [start_ts, start_ts + bucket_duration_ms).
    """

    model_config = ConfigDict(extra="forbid")
    buckets: list[HistogramBucket]
    bucket_duration_ms: int


class SavedServiceIdentity(BaseModel):
    """One saved (service, source_type) identity. Mirrors UI ServiceIdentity."""

    model_config = ConfigDict(extra="forbid")
    service: str
    source_type: str


class SavedQueryResponse(BaseModel):
    """One saved query row (response for GET list items / POST / PATCH)."""

    model_config = ConfigDict(extra="forbid")
    id: int
    name: str
    logs_ql: str
    selected_services: list[SavedServiceIdentity]
    since_preset: str | None = None
    range_start_iso: str | None = None
    range_end_iso: str | None = None
    advanced_mode: bool
    created_at: str
    updated_at: str


class SavedQueriesListResponse(BaseModel):
    """Response for GET /api/logs/saved-queries (sorted by name)."""

    model_config = ConfigDict(extra="forbid")
    saved_queries: list[SavedQueryResponse]


class SaveQueryCreateRequest(BaseModel):
    """Body for POST /api/logs/saved-queries.

    Invariant: either since_preset is set, OR both range_start_iso and
    range_end_iso are set. Validated by a model validator.
    """

    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    logs_ql: str = Field(max_length=4096)
    selected_services: list[SavedServiceIdentity] = Field(default_factory=lambda: [])
    since_preset: str | None = None
    range_start_iso: str | None = None
    range_end_iso: str | None = None
    advanced_mode: bool = False

    @model_validator(mode="after")
    def _check_range_invariant(self) -> SaveQueryCreateRequest:
        has_partial_custom = (self.range_start_iso is None) != (self.range_end_iso is None)
        if has_partial_custom:
            msg = "range_start_iso and range_end_iso must both be set or both be null"
            raise ValueError(msg)
        has_preset = self.since_preset is not None
        has_custom = self.range_start_iso is not None and self.range_end_iso is not None
        if has_preset == has_custom:  # both set OR neither set → invalid
            msg = (
                "exactly one of since_preset OR (range_start_iso AND range_end_iso) "
                "must be provided"
            )
            raise ValueError(msg)
        return self


class SaveQueryRenameRequest(BaseModel):
    """Body for PATCH /api/logs/saved-queries/{id}."""

    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)
