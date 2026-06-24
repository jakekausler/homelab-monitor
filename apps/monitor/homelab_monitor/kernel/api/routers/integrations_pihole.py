"""POST /api/integrations/pihole/* — Pi-hole WRITE endpoints (STAGE-006-018).

Two state-changing actions guarded by Scope.PIHOLE_WRITE + a per-action
confirm_phrase (mirrors the docker pull-and-restart precedent):

- POST /api/integrations/pihole/blocking        -> set DNS blocking on/off
- POST /api/integrations/pihole/gravity/update  -> rebuild gravity (streaming)

Both use the long-lived RW Pi-hole client (app.state.pihole_rw_client) and write
an audit row (who/what/before/after/ip) within a local transaction. Pi-hole state
lives remotely (not in the local DB), so the audit row is the sole local record of
the action, mirroring how docker probe-toggle audits the action. A downstream
PiholeError surfaces as HTTP 502 Bad Gateway; the attempt is NOT audited (audit on
success only, matching the probe-toggle precedent which audits the completed write).
"""

from __future__ import annotations

from typing import Annotated, Literal, cast

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict

from homelab_monitor.kernel.api.dependencies import get_repo, require_user_or_token
from homelab_monitor.kernel.auth.models import ApiToken, User
from homelab_monitor.kernel.auth.scopes import Scope
from homelab_monitor.kernel.db.audit import insert_audit
from homelab_monitor.kernel.db.ids import uuid7
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.pihole.client import PiholeRestClient
from homelab_monitor.kernel.pihole.errors import PiholeError

router = APIRouter(prefix="/integrations/pihole", tags=["integrations-pihole"])

_CONFIRM_ENABLE: Literal["enable"] = "enable"
_CONFIRM_DISABLE: Literal["disable"] = "disable"
_CONFIRM_GRAVITY: Literal["update"] = "update"

# Max log_tail lines persisted in the audit "after" payload (the client already
# truncates to 20; this is a defensive second cap on the audit row size).
_AUDIT_LOG_TAIL_MAX = 20


def _get_pihole_rw_client(request: Request) -> PiholeRestClient:
    client = getattr(request.app.state, "pihole_rw_client", None)
    if client is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="pihole rw client is not initialized",
        )
    return client


def _who(principal: User | ApiToken) -> str:
    """Mirror docker.py: User -> username, ApiToken -> 'token:<name>'."""
    return principal.username if isinstance(principal, User) else f"token:{principal.name}"


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client is not None else None


class BlockingRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    action: Literal["enable", "disable"]
    timer: int | None = None
    confirm_phrase: str


class BlockingResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    blocking: str
    timer: float | None
    audit_id: str


class GravityUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    confirm_phrase: str


class GravityUpdateResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    success: bool
    log_tail: list[str]
    audit_id: str


def _blocking_state_str(payload: object) -> str:
    """Extract the blocking-state string from a /api/dns/blocking payload (or 'unknown')."""
    if isinstance(payload, dict):
        val = cast("dict[str, object]", payload).get("blocking")
        if isinstance(val, str):
            return val
    return "unknown"


def _blocking_timer_val(payload: object) -> float | None:
    """Extract the timer (float|None) from a /api/dns/blocking payload."""
    if isinstance(payload, dict):
        val = cast("dict[str, object]", payload).get("timer")
        if isinstance(val, bool):
            return None
        if isinstance(val, (int, float)):
            return float(val)
    return None


def _validate_blocking_confirm(body: BlockingRequest) -> BlockingRequest:
    required = _CONFIRM_ENABLE if body.action == "enable" else _CONFIRM_DISABLE
    if body.confirm_phrase.strip().lower() != required:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"confirm_phrase must equal '{required}'",
        )
    return body


@router.post("/blocking", response_model=BlockingResponse)
async def set_blocking(
    body: Annotated[BlockingRequest, Depends(_validate_blocking_confirm)],
    request: Request,
    principal: Annotated[User | ApiToken, Depends(require_user_or_token({Scope.PIHOLE_WRITE}))],
    client: Annotated[PiholeRestClient, Depends(_get_pihole_rw_client)],
    repo: Annotated[SqliteRepository, Depends(get_repo)],
) -> BlockingResponse:
    """Enable/disable Pi-hole DNS blocking. confirm_phrase must equal the action."""

    # Read the CURRENT state first (for the audit `before`). A read failure is NOT
    # fatal — record before-state as "unknown" and proceed with the write.
    before_result = await client.dns_blocking()
    before_state = (
        _blocking_state_str(before_result.payload)
        if not isinstance(before_result, PiholeError)
        else "unknown"
    )

    blocking = body.action == "enable"
    result = await client.set_blocking(blocking=blocking, timer=body.timer)
    if isinstance(result, PiholeError):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"pihole set_blocking failed: {result.message}",
        )

    new_state = _blocking_state_str(result.payload)
    new_timer = _blocking_timer_val(result.payload)
    audit_id = uuid7()
    async with repo.transaction() as conn:
        await insert_audit(
            conn,
            audit_id=audit_id,
            who=_who(principal),
            what=f"pihole.blocking.{body.action}",
            before={"blocking": before_state},
            after={"blocking": new_state, "timer": new_timer},
            ip=_client_ip(request),
        )
    return BlockingResponse(blocking=new_state, timer=new_timer, audit_id=audit_id)


def _validate_gravity_confirm(body: GravityUpdateRequest) -> GravityUpdateRequest:
    if body.confirm_phrase.strip().lower() != _CONFIRM_GRAVITY:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"confirm_phrase must equal '{_CONFIRM_GRAVITY}'",
        )
    return body


@router.post("/gravity/update", response_model=GravityUpdateResponse)
async def gravity_update(
    body: Annotated[GravityUpdateRequest, Depends(_validate_gravity_confirm)],
    request: Request,
    principal: Annotated[User | ApiToken, Depends(require_user_or_token({Scope.PIHOLE_WRITE}))],
    client: Annotated[PiholeRestClient, Depends(_get_pihole_rw_client)],
    repo: Annotated[SqliteRepository, Depends(get_repo)],
) -> GravityUpdateResponse:
    """Trigger a Pi-hole gravity rebuild. confirm_phrase must equal 'update'."""

    result = await client.gravity_update()
    if isinstance(result, PiholeError):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"pihole gravity update failed: {result.message}",
        )

    payload = result.payload
    success = False
    log_tail: list[str] = []
    if isinstance(payload, dict):
        success_obj = cast("dict[str, object]", payload).get("success")
        success = bool(success_obj) if isinstance(success_obj, bool) else False
        tail_obj = cast("dict[str, object]", payload).get("log_tail")
        if isinstance(tail_obj, list):
            log_tail = [str(x) for x in cast("list[object]", tail_obj)][:_AUDIT_LOG_TAIL_MAX]

    audit_id = uuid7()
    async with repo.transaction() as conn:
        await insert_audit(
            conn,
            audit_id=audit_id,
            who=_who(principal),
            what="pihole.gravity.update",
            before=None,
            after={"success": success, "log_tail": log_tail},
            ip=_client_ip(request),
        )
    return GravityUpdateResponse(success=success, log_tail=log_tail, audit_id=audit_id)
