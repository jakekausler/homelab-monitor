"""Pydantic schemas for the /api/crons router.

All schemas use ``ConfigDict(extra='forbid')`` to reject unknown fields
with 422. ``CronListQuery`` and ``PreviewRunsQuery`` are validated via
``query_model()`` (the same helper used by the heartbeat receiver) so
extras land in the body of the 422 envelope.

Field validators on ``schedule`` use ``canonicalize_schedule`` from
``schedule.py``: invalid cron expressions raise ``InvalidCronExpression``
(a ValueError subclass) which Pydantic surfaces as a 422 with the message
attached.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from homelab_monitor.kernel.cron.schedule import (
    InvalidCronExpression,
    canonicalize_schedule,
)

if TYPE_CHECKING:
    from homelab_monitor.kernel.cron.repository import CronRecord
    from homelab_monitor.kernel.cron.run_repository import CronRunRecord

LastSeenState = Literal["unknown", "running", "ok", "failed", "late"]
WrapperHealth = Literal["ok", "stale", "unknown", "format_outdated"]
RunState = Literal["running", "ok", "fail", "unknown"]
RunLogStatus = Literal["available", "running", "expired"]


# ---------- Query params ----------


class CronListQuery(BaseModel):
    """Query params for ``GET /api/crons``.

    Filter combinatorics: every filter is ANDed. ``q`` is a case-insensitive
    substring match on either ``name`` OR ``command``.

    ``page`` is 1-based to match user expectations. ``page_size`` is capped at
    500 to bound memory; default 100 matches the spec recommendation.
    """

    model_config = ConfigDict(extra="forbid")

    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=100, ge=1, le=500)
    host: str | None = Field(default=None, max_length=200)
    enabled: bool | None = None
    state: LastSeenState | None = None
    q: str | None = Field(default=None, max_length=200)
    include_hidden: bool = False
    include_soft_deleted: bool = False
    wrapper_installed: bool | None = None


class PreviewRunsQuery(BaseModel):
    """Query params for both preview-runs endpoints.

    Used by:
    - ``GET /api/crons/{fingerprint}/preview-runs?count=N`` (saved cron)
    - ``GET /api/crons/preview-runs?expr=...&count=N`` (unsaved input from
      add-cron modal). For the unsaved form ``expr`` is required; for the
      saved form ``expr`` is omitted (router never asks for it).

    Splitting into two models would be cleaner but the router validates
    presence of ``expr`` per-endpoint to keep the URL contract obvious.
    """

    model_config = ConfigDict(extra="forbid")

    expr: str | None = Field(default=None, min_length=1, max_length=200)
    count: int = Field(default=3, ge=1, le=10)

    @field_validator("expr")
    @classmethod
    def _validate_expr(cls, v: str | None) -> str | None:
        if v is None:  # pragma: no cover
            return None
        try:
            canonicalize_schedule(v)
        except InvalidCronExpression as exc:
            raise ValueError(str(exc)) from exc
        return v


# ---------- Request bodies ----------


class CronUpdate(BaseModel):
    """Body for ``PATCH /api/crons/{fingerprint}``. Editable fields only.

    Per the derived-state model, the user can only edit policy fields. The
    cron's identity (host/source_path/schedule/command) is read-only — to
    change identity, the user edits the underlying crontab and rediscovery
    creates a new fingerprinted row.
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=200)
    expected_grace_seconds: int | None = Field(default=None, ge=0, le=86_400)
    enabled: bool | None = None
    hidden_at: str | None = Field(default=None, max_length=64)


class RegisterCronBody(BaseModel):
    """Body for ``POST /api/hb/{fingerprint}/register``.

    The wrapper script (or any heartbeat client) posts the 4-tuple of identity
    fields plus a ``wrapper`` flag. The server recomputes the fingerprint from
    these fields and rejects with 422 if the URL fingerprint does not match
    (defensive — should only happen if the wrapper has a hash bug or the URL is
    tampered with).

    The ``source_path`` field is optional (``None`` for remote-only crons whose
    disk source the monitor cannot see). JSON ``null`` is the serialized form;
    the empty string is NOT equivalent (see ``fingerprint.py`` docstring).
    """

    model_config = ConfigDict(extra="forbid")

    host: str = Field(min_length=1, max_length=255)
    source_path: str | None = Field(default=None, max_length=500)
    schedule: str = Field(min_length=1, max_length=200)
    command: str = Field(min_length=1, max_length=2000)
    wrapper: bool = False


# ---------- Response models ----------


class HeartbeatStateOut(BaseModel):
    """Public projection of a ``heartbeats_state`` row.

    Mirrors ``HeartbeatStateRecord`` from cron/repository.py but with
    Pydantic for serialization. Used inside ``CronWithStateOut``.
    """

    model_config = ConfigDict(extra="forbid")

    cron_fingerprint: str
    current_state: LastSeenState
    last_start_at: str | None
    last_ok_at: str | None
    last_fail_at: str | None
    current_streak: int
    expected_next_at: str | None
    last_duration_seconds: float | None
    last_exit_code: int | None
    updated_at: str


class CronOut(BaseModel):
    """Public projection of a ``crons`` row."""

    model_config = ConfigDict(extra="forbid")

    fingerprint: str
    name: str
    host: str
    command: str
    schedule: str | None
    schedule_canonical: str | None
    cadence_seconds: int
    expected_grace_seconds: int
    enabled: bool
    last_seen_state: LastSeenState
    created_at: str
    updated_at: str
    hidden_at: str | None
    source_path: str | None
    wrapper_last_seen_at: str | None
    last_discovered_at: str | None
    soft_deleted_at: str | None
    is_local: bool
    wrapper_installed: bool


class CronListResponse(BaseModel):
    """Paginated list payload for ``GET /api/crons``."""

    model_config = ConfigDict(extra="forbid")

    items: list[CronOut]
    total: int
    page: int
    page_size: int


class CronWithStateOut(BaseModel):
    """Combined cron + heartbeat state for ``GET /api/crons/{fingerprint}``."""

    model_config = ConfigDict(extra="forbid")

    cron: CronOut
    state: HeartbeatStateOut | None
    wrapper_health: WrapperHealth


class PreviewRunsResponse(BaseModel):
    """Response for both preview-runs endpoints."""

    model_config = ConfigDict(extra="forbid")

    runs: list[str]


# ---------- Install wrapper request/response ----------


class InstallWrapperRequest(BaseModel):
    """Body for POST /api/crons/{fingerprint}/install-wrapper."""

    model_config = ConfigDict(extra="forbid")
    confirm: bool = False


class CrontabDiffOut(BaseModel):
    """Crontab line diff for install preview."""

    model_config = ConfigDict(extra="forbid")
    source_path: str
    old_line: str
    new_line: str


class InstallWrapperPreview(BaseModel):
    """Dry-run response (confirm=false)."""

    model_config = ConfigDict(extra="forbid")
    fingerprint: str
    wrapper_path: str
    wrapper_content: str
    token_file_path: str
    crontab_diff: CrontabDiffOut


class InstallWrapperResult(BaseModel):
    """confirm=true response."""

    model_config = ConfigDict(extra="forbid")
    cron: CronOut


# ---------- Uninstall wrapper request/response ----------


class UninstallWrapperRequest(BaseModel):
    """Body for POST /api/crons/{fingerprint}/uninstall-wrapper."""

    model_config = ConfigDict(extra="forbid")
    confirm: bool = False


class UninstallWrapperPreview(BaseModel):
    """Dry-run response (confirm=false). Uninstall is a pure crontab-line edit,
    so the preview carries ONLY the crontab diff (no wrapper_content / token)."""

    model_config = ConfigDict(extra="forbid")
    fingerprint: str
    crontab_diff: CrontabDiffOut


class UninstallWrapperResult(BaseModel):
    """confirm=true response."""

    model_config = ConfigDict(extra="forbid")
    cron: CronOut


# ---------- Run history (STAGE-002-014) ----------


class CronRunsListQuery(BaseModel):
    """Query params for ``GET /api/crons/{fingerprint}/runs``.

    ``limit`` is the page size (default 50, cap 200). ``cursor`` is the
    opaque pagination cursor returned by the previous page (None for the
    first page). ``state`` (optional) filters by run state.
    """

    model_config = ConfigDict(extra="forbid")

    limit: int = Field(default=50, ge=1, le=200)
    cursor: str | None = Field(default=None, min_length=1, max_length=256)
    state: RunState | None = None


class CronRunOut(BaseModel):
    """Public projection of one ``cron_runs`` row.

    EXCLUDES the internal-only fields ``content_digest``, ``vl_window_start``,
    ``vl_window_end``, and ``enriched_at`` (the timestamp). The boolean
    ``enriched`` derives from ``enriched_at IS NOT NULL`` — clients only
    need to know if enrichment ran, not when. ``content_digest`` is reserved
    for EPIC-004 (not surfaced in v1).
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str
    state: RunState
    started_at: str
    ended_at: str | None
    duration_seconds: float | None
    exit_code: int | None
    source: str
    overlapping: bool
    line_count: int | None
    byte_count: int | None
    anomaly_flags: str
    enriched: bool


class CronRunListResponse(BaseModel):
    """Cursor-paginated list payload for ``GET /api/crons/{fp}/runs``."""

    model_config = ConfigDict(extra="forbid")

    items: list[CronRunOut]
    next_cursor: str | None


# ---------- Run log (STAGE-002-014) ----------


class RunLogLine(BaseModel):
    """One log line returned by the narrow run-log endpoint."""

    model_config = ConfigDict(extra="forbid")

    timestamp: str
    message: str
    stream: str
    fields: dict[str, str]


class RunLogResponse(BaseModel):
    """Response body for ``GET /api/crons/{fp}/runs/{run_id}/log``.

    Single model with a ``log_status`` discriminator field. The 503
    ``vl_unavailable`` error case is NOT this shape — it uses the standard
    HttpProblem error envelope (the endpoint raises before constructing
    this body).

    log_status values:
      - "available": run is closed AND VL returned data. entries populated.
      - "running": run is still in flight; VL queried over [vl_window_start, now].
        entries reflect output so far.
      - "expired": closed run whose vl_window_end is older than VL retention;
        OR defensively for a malformed closed-row state. entries=[].
    """

    model_config = ConfigDict(extra="forbid")

    log_status: RunLogStatus
    state: RunState
    duration_seconds: float | None
    line_count: int | None
    byte_count: int | None
    anomaly_flags: str
    entries: list[RunLogLine]
    truncated: bool


# ---------- Helpers ----------


def cron_record_to_out(rec: CronRecord, *, local_hostname: str | None = None) -> CronOut:
    """Convert a CronRecord (database row) to CronOut (public projection).

    Shared across heartbeat and crons routers to avoid duplication.

    Args:
        rec: The cron record to convert.
        local_hostname: The local hostname for computing `is_local`. If None, `is_local` is False.
    """
    return CronOut(
        fingerprint=rec.fingerprint,
        name=rec.name,
        host=rec.host,
        command=rec.command,
        schedule=None if rec.schedule == "" else rec.schedule,
        schedule_canonical=rec.schedule_canonical,
        cadence_seconds=rec.cadence_seconds,
        expected_grace_seconds=rec.expected_grace_seconds,
        enabled=rec.enabled,
        last_seen_state=rec.last_seen_state,  # type: ignore[arg-type]
        created_at=rec.created_at,
        updated_at=rec.updated_at,
        hidden_at=rec.hidden_at,
        source_path=rec.source_path,
        wrapper_last_seen_at=rec.wrapper_last_seen_at,
        last_discovered_at=rec.last_discovered_at,
        soft_deleted_at=rec.soft_deleted_at,
        is_local=(local_hostname is not None and rec.host == local_hostname),
        wrapper_installed=rec.wrapper_installed,
    )


def cron_run_record_to_out(rec: CronRunRecord) -> CronRunOut:
    """Convert CronRunRecord → CronRunOut.

    Drops internal-only columns and computes ``enriched`` from
    ``enriched_at IS NOT NULL``.
    """
    return CronRunOut(
        run_id=rec.run_id,
        state=rec.state,  # type: ignore[arg-type]
        started_at=rec.started_at,
        ended_at=rec.ended_at,
        duration_seconds=rec.duration_seconds,
        exit_code=rec.exit_code,
        source=rec.source,
        overlapping=rec.overlapping,
        line_count=rec.line_count,
        byte_count=rec.byte_count,
        anomaly_flags=rec.anomaly_flags,
        enriched=rec.enriched_at is not None,
    )
