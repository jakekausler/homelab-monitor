"""Inbound Home Assistant event webhook ingester.

``POST /api/integrations/ha/event`` validates an :class:`HAEventPayload`, writes a single
``audit_log`` row, and returns ``202``. Pure audit sink — no alert forwarding, no dispatcher.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from homelab_monitor.kernel.api._audit_helpers import principal_label
from homelab_monitor.kernel.api.dependencies import get_repo, require_user_or_token
from homelab_monitor.kernel.auth.models import ApiToken, User
from homelab_monitor.kernel.auth.scopes import Scope
from homelab_monitor.kernel.db.audit import audit_write
from homelab_monitor.kernel.db.repository import SqliteRepository

_HA_EVENT_ACCEPTED_STATUS = 202


class HAEventPayload(BaseModel):
    """Inbound Home Assistant event payload."""

    model_config = ConfigDict(extra="forbid")

    event_type: str = Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_.:-]+$")
    data: dict[str, Any] = Field(default_factory=dict)
    severity: Literal["info", "warning", "critical"] | None = None
    title: str | None = Field(default=None, max_length=256)


class HAEventResponse(BaseModel):
    """Response envelope for an accepted HA event."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["accepted"]


def _client_ip(request: Request) -> str | None:
    """Return the request peer IP, or None when starlette omits request.client."""
    if request.client is not None:
        return request.client.host
    return None  # pragma: no cover -- defensive


router = APIRouter(prefix="/integrations/ha", tags=["integrations"])


@router.post("/event", response_model=HAEventResponse, status_code=_HA_EVENT_ACCEPTED_STATUS)
async def ingest_ha_event(
    payload: HAEventPayload,
    request: Request,
    principal: Annotated[User | ApiToken, Depends(require_user_or_token({Scope.HA_EVENT_WRITE}))],
    repo: Annotated[SqliteRepository, Depends(get_repo)],
) -> JSONResponse:
    """Record an inbound HA event as a single audit_log row and return 202."""
    await audit_write(
        repo,
        who=principal_label(principal),
        what=f"ha_event.{payload.event_type}",
        after=payload.model_dump(mode="json"),
        ip=_client_ip(request),
    )
    return JSONResponse(
        status_code=_HA_EVENT_ACCEPTED_STATUS,
        content=HAEventResponse(status="accepted").model_dump(mode="json"),
    )
