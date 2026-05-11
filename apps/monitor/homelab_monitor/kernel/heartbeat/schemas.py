"""Pydantic query-param schemas for the heartbeat receiver endpoints.

These are NOT used as request bodies (the endpoints take no body); FastAPI
binds them to query parameters via ``Annotated[..., Query()]``.

Cap rationale:
- ``duration``: 0 .. 86400 s (24 h). Anything longer is almost certainly a
  client bug or unit confusion (ms -> s) and should 422 rather than corrupt
  the metric range.
- ``exit_code``: 0 .. 255 (POSIX ``waitpid`` truncates exit status to 8 bits).
  Anything outside is a malformed client payload.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class HeartbeatStartQuery(BaseModel):
    """Query params for ``POST /api/hb/{cron_id}/start``.

    Currently empty (``/start`` carries no run-context). Defined as a class so
    that ``model_config = ConfigDict(extra='forbid')`` rejects rogue query
    params with 422 — keeps the contract loud rather than silent.
    """

    model_config = ConfigDict(extra="forbid")


class HeartbeatOkQuery(BaseModel):
    """Query params for ``POST /api/hb/{cron_id}/ok``."""

    model_config = ConfigDict(extra="forbid")
    duration: float | None = Field(default=None, ge=0, le=86400)


class HeartbeatFailQuery(BaseModel):
    """Query params for ``POST /api/hb/{cron_id}/fail``."""

    model_config = ConfigDict(extra="forbid")
    duration: float | None = Field(default=None, ge=0, le=86400)
    exit_code: int | None = Field(default=None, ge=0, le=255)
