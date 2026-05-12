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

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from homelab_monitor.kernel.cron.schedule import (
    InvalidCronExpression,
    canonicalize_schedule,
)

LastSeenState = Literal["unknown", "running", "ok", "failed", "late"]


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
    wrapper_installed_at: str | None


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


class PreviewRunsResponse(BaseModel):
    """Response for both preview-runs endpoints."""

    model_config = ConfigDict(extra="forbid")

    runs: list[str]
