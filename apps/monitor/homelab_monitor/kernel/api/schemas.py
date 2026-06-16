"""Pydantic response models for API endpoints."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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
    "AnnotationCreateRequest",
    "AnnotationListResponse",
    "AnnotationResponse",
    "BackupResponse",
    "CollectorStatus",
    "DismissResponse",
    "DrainCycleResultResponse",
    "ErrorEnvelope",
    "ErrorPayload",
    "FieldDescriptor",
    "HealthzResponse",
    "HistogramBucket",
    "IngestResponse",
    "LastCycleResponse",
    "LogUserRuleCreateRequest",
    "LogUserRuleHealth",
    "LogUserRuleListResponse",
    "LogUserRulePatchRequest",
    "LogUserRuleResponse",
    "LogUserRulesHealthResponse",
    "LogWindowResponse",
    "LogsFieldsResponse",
    "LogsHistogramResponse",
    "LogsQueryResponse",
    "LogsRetentionResponse",
    "LogsRetentionUpdateRequest",
    "LogsServicesResponse",
    "LogsStreamSummary",
    "LogsStreamsResponse",
    "MetricNamesResponse",
    "MetricsRangeResponse",
    "MetricsSnapshotEntry",
    "MetricsSnapshotResponse",
    "ModelDetailResponse",
    "ModelListResponse",
    "ModelSummary",
    "ModelTemplateEntry",
    "OutcomeView",
    "RefreshCycleResponse",
    "RefreshStatusResponse",
    "RetryResponse",
    "SaveQueryCreateRequest",
    "SaveQueryRenameRequest",
    "SavedQueriesListResponse",
    "SavedQueryResponse",
    "SavedServiceIdentity",
    "ServiceCount",
    "SignatureListResponse",
    "SignaturePatchRequest",
    "SignatureResponse",
    "SignatureSamplesResponse",
    "SilenceAllowlistCreateRequest",
    "SilenceAllowlistListResponse",
    "SilenceAllowlistResponse",
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


class MetricNamesResponse(BaseModel):
    """Response for GET /api/metrics/metric-names — VM ``__name__`` label values.

    Discovery aid for the MetricsQL Simple-mode authoring autocomplete. ``names``
    is the distinct list of metric names VictoriaMetrics currently knows about
    (VM's ``/api/v1/label/__name__/values`` response ``data``).
    """

    model_config = ConfigDict(extra="forbid")
    names: list[str]


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


class LogWindowResponse(BaseModel):
    """Response for GET /api/logs/window (STAGE-004-031A).

    The anchor-centered surrounding-logs window: N lines before + N after the
    selected line's timestamp, merged + deduped + sorted ascending. `lines`
    reuses the converged LogLine shape (same as /logs/query). All fields except
    `anchor_index` are ALWAYS populated → required (non-optional FE types).
    `anchor_index` is genuinely nullable (anchor may not be locatable).
    """

    model_config = ConfigDict(extra="forbid")
    lines: list[LogLine]
    truncated_before: bool
    truncated_after: bool
    degraded: bool
    anchor_index: int | None
    window_start: datetime
    window_end: datetime
    queried_at: datetime


class SignatureResponse(BaseModel):
    """One signature catalog row."""

    model_config = ConfigDict(extra="forbid")
    template_hash: str
    service_key: str
    template_str: str
    label: str | None = None
    status: Literal["active", "suppressed", "expected"]
    first_seen_at: int
    last_seen_at: int
    total_count: int


class SignatureListResponse(BaseModel):
    """Response for GET /api/logs/signatures."""

    model_config = ConfigDict(extra="forbid")
    signatures: list[SignatureResponse]
    total: int


class SignaturePatchRequest(BaseModel):
    """Request body for PATCH /api/logs/signatures/{template_hash}/{service_key}.

    Both fields optional: a request may set label only, status only, or both.
    `label=None` in the JSON body is INDISTINGUISHABLE from omitted with this shape;
    use a sentinel-free contract: label is set whenever the key is present. The
    endpoint uses `model_fields_set` to distinguish 'set label to null' from 'omit'.
    """

    model_config = ConfigDict(extra="forbid")
    label: str | None = None
    status: Literal["active", "suppressed", "expected"] | None = None


class SignatureSamplesResponse(BaseModel):
    """Response for GET /api/logs/signatures/{template_hash}/{service_key}/samples.

    `lines` is the converged LogLine shape (reused from /logs/query). `reason` is
    null on success, or one of 'template_too_generic' / 'vl_unavailable' when
    `lines` is empty for a known best-effort reason.
    """

    model_config = ConfigDict(extra="forbid")
    lines: list[LogLine]
    reason: str | None = None


class AnnotationResponse(BaseModel):
    """One signature annotation row."""

    model_config = ConfigDict(extra="forbid")
    id: int
    template_hash: str
    service_key: str
    note: str
    author: str
    created_at: str  # ISO-8601 UTC


class AnnotationListResponse(BaseModel):
    """Response for GET /api/logs/signatures/{h}/{s}/annotations."""

    model_config = ConfigDict(extra="forbid")
    annotations: list[AnnotationResponse]


class AnnotationCreateRequest(BaseModel):
    """Body for POST /api/logs/signatures/{h}/{s}/annotations.

    `note` is trimmed; whitespace-only is rejected (422). The STRIPPED value is
    what gets stored. min_length=1 guards the empty string; the validator guards
    the whitespace-only string.
    """

    model_config = ConfigDict(extra="forbid")
    note: str = Field(min_length=1, max_length=2000)

    @field_validator("note")
    @classmethod
    def _strip_note(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            msg = "note must not be empty or whitespace-only"
            raise ValueError(msg)
        return stripped


class SilenceAllowlistResponse(BaseModel):
    """One expected-silence allowlist entry."""

    model_config = ConfigDict(extra="forbid")
    id: int
    template_hash: str | None
    service_key: str
    schedule_kind: Literal["always", "cron", "window"]
    schedule_value: str
    reason: str
    created_at: str  # ISO-8601 UTC
    expires_at: str | None


class SilenceAllowlistListResponse(BaseModel):
    """Response for GET /api/logs/signatures/silence-allowlist."""

    model_config = ConfigDict(extra="forbid")
    entries: list[SilenceAllowlistResponse]


class SilenceAllowlistCreateRequest(BaseModel):
    """Body for POST /api/logs/signatures/silence-allowlist.

    schedule_value semantics by kind:
      - always: MUST be empty ('') — any value is rejected (422).
      - cron:   a cron expression; canonicalized via canonicalize_schedule (422 if invalid).
      - window: '<start-iso>/<end-iso>'; both ends valid ISO, start <= end (422 otherwise).
    expires_at, when present, MUST be a valid ISO-8601 datetime (422 otherwise).
    template_hash omitted/None => per-service entry (covers all signatures of service_key).
    """

    model_config = ConfigDict(extra="forbid")
    template_hash: str | None = None
    service_key: str = Field(min_length=1, max_length=200)
    schedule_kind: Literal["always", "cron", "window"]
    schedule_value: str = Field(default="", max_length=200)
    reason: str = Field(min_length=1, max_length=2000)
    expires_at: str | None = None

    @field_validator("expires_at")
    @classmethod
    def _validate_expires_at(cls, v: str | None) -> str | None:
        # A naive (timezone-less) value is interpreted as UTC by the
        # silence-detection collector (exp.replace(tzinfo=UTC)).
        if v is None:
            return None
        try:
            datetime.fromisoformat(v)
        except ValueError as exc:
            msg = f"expires_at must be a valid ISO-8601 datetime: {v!r}"
            raise ValueError(msg) from exc
        return v

    @model_validator(mode="after")
    def _validate_schedule(self) -> SilenceAllowlistCreateRequest:
        from homelab_monitor.kernel.cron.schedule import (  # noqa: PLC0415
            InvalidCronExpression,
            canonicalize_schedule,
        )

        kind = self.schedule_kind
        val = self.schedule_value
        if kind == "always":
            if val != "":
                msg = "schedule_value must be empty for schedule_kind='always'"
                raise ValueError(msg)
        elif kind == "cron":
            if not val.strip():
                msg = "schedule_value (cron expression) is required for schedule_kind='cron'"
                raise ValueError(msg)
            try:
                canonical = canonicalize_schedule(val)
            except InvalidCronExpression as exc:
                raise ValueError(str(exc)) from exc
            if canonical == "@reboot":
                msg = (
                    "schedule_value '@reboot' is not supported for silence cron "
                    "entries; use a recurring cron expression"
                )
                raise ValueError(msg)
            object.__setattr__(self, "schedule_value", canonical)
        else:
            # schedule_kind is Literal[always|cron|window]; not-always + not-cron => window.
            self._validate_window(val)
        return self

    @staticmethod
    def _validate_window(val: str) -> None:
        parts = val.split("/")
        if len(parts) != 2:  # noqa: PLR2004
            msg = "window schedule_value must be '<start-iso>/<end-iso>'"
            raise ValueError(msg)
        try:
            start = datetime.fromisoformat(parts[0])
            end = datetime.fromisoformat(parts[1])
        except ValueError as exc:
            msg = f"window schedule_value has invalid ISO datetime: {val!r}"
            raise ValueError(msg) from exc
        if end < start:
            msg = "window end must be >= start"
            raise ValueError(msg)


class LogUserRuleResponse(BaseModel):
    """One user-authored vmalert rule."""

    model_config = ConfigDict(extra="forbid")
    id: int
    rule_name: str
    expr: str
    expr_kind: Literal["logsql", "metricsql"]
    severity: Literal["info", "warning", "critical"]
    summary: str
    description: str
    for_duration: str
    source_kind: str
    source_ref: str | None
    enabled: bool
    created_at: str  # ISO-8601 UTC
    updated_at: str  # ISO-8601 UTC


class LogUserRuleListResponse(BaseModel):
    """Response for GET /api/logs/user-rules."""

    model_config = ConfigDict(extra="forbid")
    rules: list[LogUserRuleResponse]


class LogUserRuleCreateRequest(BaseModel):
    """Body for POST /api/logs/user-rules.

    rule_name MUST be a Prometheus alertname identifier (^[A-Za-z_][A-Za-z0-9_]*$).
    expr is the LogsQL (logsql) or MetricsQL (metricsql) expression. for_duration
    is a vmalert duration ('5m'/'30s'/...) or '0s'. Deeper validation (incl. a
    render dry-run) happens in the repo; bad input there -> 400.
    """

    model_config = ConfigDict(extra="forbid")
    rule_name: str = Field(min_length=1, max_length=200)
    expr: str = Field(min_length=1, max_length=8192)
    expr_kind: Literal["logsql", "metricsql"]
    severity: Literal["info", "warning", "critical"]
    summary: str = Field(min_length=1, max_length=1000)
    description: str = Field(default="", max_length=4000)
    for_duration: str = Field(default="0s", max_length=16)


class LogUserRulePatchRequest(BaseModel):
    """Body for PATCH /api/logs/user-rules/{id}. All fields optional (partial update).

    rule_name and expr_kind are immutable and NOT accepted here.
    """

    model_config = ConfigDict(extra="forbid")
    expr: str | None = Field(default=None, min_length=1, max_length=8192)
    severity: Literal["info", "warning", "critical"] | None = None
    summary: str | None = Field(default=None, min_length=1, max_length=1000)
    description: str | None = Field(default=None, max_length=4000)
    for_duration: str | None = Field(default=None, max_length=16)
    enabled: bool | None = None


class LogUserRuleHealth(BaseModel):
    """vmalert-reported health for one user rule."""

    model_config = ConfigDict(extra="forbid")
    health: Literal["ok", "err", "unknown"]
    last_error: str = ""


class LogUserRulesHealthResponse(BaseModel):
    """Response for GET /api/logs/user-rules-health.

    Maps rule_name -> health. Rules present in the DB but absent here (e.g. a
    vmalert instance was unreachable, or vmalert has not yet loaded the rule)
    are NOT included; the UI defaults missing entries to "unknown".
    """

    model_config = ConfigDict(extra="forbid")
    rules: dict[str, LogUserRuleHealth]


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


class RefreshCycleResponse(BaseModel):
    """202 response for POST /logs/signatures/refresh."""

    model_config = ConfigDict(extra="forbid")
    cycle_id: str


class DrainCycleResultResponse(BaseModel):
    """One drain cycle's outcome (mirrors DrainCycleResult)."""

    model_config = ConfigDict(extra="forbid")
    started_at: int
    finished_at: int
    lines_processed: int
    new_templates: int
    models_touched: int
    cycle_status: Literal["ok", "partial", "failed"]
    error: str | None = None


class RefreshStatusResponse(BaseModel):
    """Status of a manually-triggered drain cycle."""

    model_config = ConfigDict(extra="forbid")
    status: Literal["running", "done", "failed"]
    result: DrainCycleResultResponse | None = None
    error: str | None = None


class ModelSummary(BaseModel):
    """Column-level summary of one drain_models row (no blob deserialize)."""

    model_config = ConfigDict(extra="forbid")
    model_key: str
    template_count: int
    line_count: int
    last_processed_ts: int | None = None
    updated_at: int


class ModelListResponse(BaseModel):
    """Response for GET /api/logs/signatures/models."""

    model_config = ConfigDict(extra="forbid")
    models: list[ModelSummary]


class ModelTemplateEntry(BaseModel):
    """One mined template within a model (from engine.templates())."""

    model_config = ConfigDict(extra="forbid")
    template_id: int
    template_hash: str
    template_str: str
    size: int
    first_seen_ts: int
    last_seen_ts: int


class ModelDetailResponse(BaseModel):
    """Response for GET /api/logs/signatures/models/{model_key}."""

    model_config = ConfigDict(extra="forbid")
    model_key: str
    summary: ModelSummary
    templates: list[ModelTemplateEntry]


class LastCycleResponse(BaseModel):
    """Response for GET /api/logs/signatures/cycle/last.

    `has_run` is False (and all numeric fields are null/zero) when no cycle has run
    yet — this is NOT an error.
    """

    model_config = ConfigDict(extra="forbid")
    has_run: bool
    started_at: int | None = None
    finished_at: int | None = None
    lines_processed: int = 0
    new_templates: int = 0
    models_touched: int = 0
    cycle_status: Literal["ok", "partial", "failed"] | None = None
    error: str | None = None
