"""Pydantic schemas for the /api/crons router.

All schemas use ``ConfigDict(extra='forbid')`` to reject unknown fields
with 422. ``CronListQuery`` and ``PreviewRunsQuery`` are validated via
``query_model()`` (the same helper used by the heartbeat receiver) so
extras land in the body of the 422 envelope.

Field validators on ``schedule`` use ``canonicalize_schedule`` from
``schedule.py``: invalid cron expressions raise ``InvalidCronExpression``
(a ValueError subclass) which Pydantic surfaces as a 422 with the message
attached.

The xor contract (schedule XOR cadence) is enforced via ``model_validator``
on ``CronCreate`` (where both fields are required-ish) and on ``CronUpdate``
(where both are optional but if supplied must still satisfy the rule —
relaxed: only enforce when BOTH are supplied AND would still leave the
row valid; full enforcement happens at the repo layer where current state
is known).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from homelab_monitor.kernel.cron.schedule import (
    InvalidCronExpression,
    canonicalize_schedule,
)

IntegrationMode = Literal["observe", "heartbeat", "both"]
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
    integration_mode: IntegrationMode | None = None
    enabled: bool | None = None
    state: LastSeenState | None = None
    q: str | None = Field(default=None, max_length=200)
    include_archived: bool = False


class PreviewRunsQuery(BaseModel):
    """Query params for both preview-runs endpoints.

    Used by:
    - ``GET /api/crons/{id}/preview-runs?count=N`` (saved cron)
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


class CronCreate(BaseModel):
    """Body for ``POST /api/crons``.

    Exactly one of ``schedule`` (non-empty cron expression) OR
    ``cadence_seconds`` (>0) must be set. Both-set or neither-set yields 422.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    host: str = Field(min_length=1, max_length=200)
    command: str = Field(min_length=1, max_length=2000)
    schedule: str | None = Field(default=None, min_length=1, max_length=200)
    cadence_seconds: int = Field(default=0, ge=0, le=86_400)
    expected_grace_seconds: int = Field(default=300, ge=0, le=86_400)
    integration_mode: IntegrationMode = "observe"
    enabled: bool = True

    @field_validator("schedule")
    @classmethod
    def _validate_schedule(cls, v: str | None) -> str | None:
        if v is None:
            return None
        try:
            canonicalize_schedule(v)
        except InvalidCronExpression as exc:
            raise ValueError(str(exc)) from exc
        return v

    @model_validator(mode="after")
    def _validate_xor(self) -> CronCreate:
        has_schedule = self.schedule is not None and self.schedule.strip() != ""
        has_cadence = self.cadence_seconds > 0
        if has_schedule and has_cadence:
            msg = "set exactly one of schedule or cadence_seconds (got both)"
            raise ValueError(msg)
        if not has_schedule and not has_cadence:
            msg = "set exactly one of schedule or cadence_seconds (got neither)"
            raise ValueError(msg)
        return self


class CronUpdate(BaseModel):
    """Body for ``PATCH /api/crons/{id}``.

    All fields optional. The xor invariant is rechecked at the repo layer
    against the merged (current + provided) row state — Pydantic only
    enforces local consistency when BOTH fields are supplied in the same
    request.

    ``archived_at``: pass an ISO-8601 string to soft-delete (audit verb
    becomes ``crons.delete``); pass ``null`` to restore (audit verb becomes
    ``crons.restore``).
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=200)
    host: str | None = Field(default=None, min_length=1, max_length=200)
    command: str | None = Field(default=None, min_length=1, max_length=2000)
    schedule: str | None = Field(default=None, max_length=200)
    cadence_seconds: int | None = Field(default=None, ge=0, le=86_400)
    expected_grace_seconds: int | None = Field(default=None, ge=0, le=86_400)
    integration_mode: IntegrationMode | None = None
    enabled: bool | None = None
    archived_at: str | None = Field(default=None, max_length=64)
    # archived_at sentinel: clients pass null to RESTORE; passing the literal
    # string "" is rejected. To distinguish "not provided" from "set to null"
    # we use ``model_fields_set`` at the repo layer.

    @field_validator("schedule")
    @classmethod
    def _validate_schedule(cls, v: str | None) -> str | None:
        if v is None or v == "":  # pragma: no cover
            return v
        try:
            canonicalize_schedule(v)
        except (
            InvalidCronExpression
        ) as exc:  # pragma: no cover -- defense in depth, repo validator rejects first
            raise ValueError(str(exc)) from exc
        return v

    @model_validator(mode="after")
    def _validate_xor_when_both_supplied(self) -> CronUpdate:
        """Local xor check: only fires if BOTH schedule and cadence are in payload.

        Repo layer does the full check against merged state.
        """
        provided = self.model_fields_set
        if "schedule" in provided and "cadence_seconds" in provided:
            sched = self.schedule
            cad = self.cadence_seconds or 0
            has_schedule = sched is not None and sched.strip() != ""
            has_cadence = cad > 0
            if has_schedule and has_cadence:
                msg = "set at most one of schedule or cadence_seconds (got both)"
                raise ValueError(msg)
            if not has_schedule and not has_cadence:
                msg = "set at least one of schedule or cadence_seconds (got neither)"
                raise ValueError(msg)
        return self


# ---------- Response models ----------


class HeartbeatStateOut(BaseModel):
    """Public projection of a ``heartbeats_state`` row.

    Mirrors ``HeartbeatStateRecord`` from heartbeat/repository.py but with
    Pydantic for serialization. Used inside ``CronWithStateOut``.
    """

    model_config = ConfigDict(extra="forbid")

    cron_id: str
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

    id: str
    name: str
    host: str
    command: str
    schedule: str | None
    schedule_canonical: str | None
    cadence_seconds: int
    expected_grace_seconds: int
    integration_mode: IntegrationMode
    enabled: bool
    last_seen_state: LastSeenState
    created_at: str
    updated_at: str
    archived_at: str | None


class CronListResponse(BaseModel):
    """Paginated list payload for ``GET /api/crons``."""

    model_config = ConfigDict(extra="forbid")

    items: list[CronOut]
    total: int
    page: int
    page_size: int


class CronWithStateOut(BaseModel):
    """Combined cron + heartbeat state for ``GET /api/crons/{id}``."""

    model_config = ConfigDict(extra="forbid")

    cron: CronOut
    state: HeartbeatStateOut | None


class PreviewRunsResponse(BaseModel):
    """Response for both preview-runs endpoints."""

    model_config = ConfigDict(extra="forbid")

    runs: list[str]
