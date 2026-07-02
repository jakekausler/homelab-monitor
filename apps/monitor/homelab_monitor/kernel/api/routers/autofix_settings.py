"""Auto-fix settings routes: kill-switch toggle (STAGE-009-007).

GET returns the current kill-switch state + updated_at.

POST toggles the kill-switch after a case-insensitive confirm-phrase gate
(``disable auto-fix`` for on->off, ``enable auto-fix`` for off->on). On an
on->off transition, the endpoint push-triggers ``kill_inflight`` to SIGKILL
any in-flight REAL exec.

Auth: cookie session on all routes; CSRF enforced on POST via
``require_session()``.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import text

from homelab_monitor.kernel.api.dependencies import get_repo, require_session
from homelab_monitor.kernel.api.routers.autofix import get_orchestrator
from homelab_monitor.kernel.auth.models import User
from homelab_monitor.kernel.autofix.orchestrator import AutoFixOrchestrator
from homelab_monitor.kernel.db.audit import insert_audit
from homelab_monitor.kernel.db.repositories.app_settings_repository import (
    AppSettingsRepository,
)
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.docker.socket_client import DockerSocketError

router = APIRouter(prefix="/settings/autofix", tags=["settings"])

_DISABLE_PHRASE: Literal["disable auto-fix"] = "disable auto-fix"
_ENABLE_PHRASE: Literal["enable auto-fix"] = "enable auto-fix"
_AUTOFIX_ENABLED_KEY: Literal["autofix_enabled"] = "autofix_enabled"
_TRUTHY = frozenset({"true", "1", "yes"})

_READ_STATE_SQL = text("SELECT value, updated_at FROM app_settings WHERE key = :key")


def _is_truthy(value: str | None) -> bool:
    return value is not None and value.strip().lower() in _TRUTHY


def _get_app_settings_repo(
    repo: Annotated[SqliteRepository, Depends(get_repo)],
) -> AppSettingsRepository:
    return AppSettingsRepository(repo)


def _client_ip(request: Request) -> str | None:
    if request.client is not None:
        return request.client.host
    return None  # pragma: no cover -- defensive


class KillSwitchState(BaseModel):
    enabled: bool
    updated_at: str | None


class KillSwitchToggleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool
    confirm_phrase: str


class KillSwitchToggleResponse(BaseModel):
    enabled: bool
    updated_at: str | None
    killed_inflight_run_id: str | None = None
    unwind_warning: str | None = None


async def _read_state(repo: AppSettingsRepository, db: SqliteRepository) -> KillSwitchState:
    """Read enabled + updated_at in ONE SELECT (Minor #2, avoids 2 round-trips)."""
    # repo intentionally unused: we bypass AppSettingsRepository.get to fetch
    # value + updated_at in a single row.
    _ = repo
    row = await db.fetch_one(
        _READ_STATE_SQL,
        {"key": _AUTOFIX_ENABLED_KEY},
    )
    if row is None:
        return KillSwitchState(enabled=False, updated_at=None)
    return KillSwitchState(enabled=_is_truthy(str(row[0])), updated_at=str(row[1]))


@router.get("/kill-switch", response_model=KillSwitchState)
async def get_kill_switch(
    _user: Annotated[User, Depends(require_session())],
    repo: Annotated[AppSettingsRepository, Depends(_get_app_settings_repo)],
    db: Annotated[SqliteRepository, Depends(get_repo)],
) -> KillSwitchState:
    """Return the current kill-switch state + last-updated timestamp."""
    return await _read_state(repo, db)


@router.post("/kill-switch", response_model=KillSwitchToggleResponse)
async def toggle_kill_switch(  # noqa: PLR0913 -- FastAPI Depends parameters
    payload: KillSwitchToggleRequest,
    request: Request,
    user: Annotated[User, Depends(require_session())],
    repo: Annotated[AppSettingsRepository, Depends(_get_app_settings_repo)],
    db: Annotated[SqliteRepository, Depends(get_repo)],
    orchestrator: Annotated[AutoFixOrchestrator, Depends(get_orchestrator)],
) -> KillSwitchToggleResponse:
    current_value = await repo.get(_AUTOFIX_ENABLED_KEY)
    current_enabled = _is_truthy(current_value)
    target_enabled = payload.enabled
    submitted = payload.confirm_phrase.strip().lower()
    ip = _client_ip(request)

    # No-op: state already matches. Return current state WITHOUT auditing —
    # a matching-state POST is a legitimate idempotent "current state alias"
    # (equivalent to a GET). Auditing it would let any session flood the
    # audit table with garbage confirm_phrases (Important #2). Real state
    # transitions below still require a valid confirm phrase and DO audit.
    if current_enabled == target_enabled:
        state = await _read_state(repo, db)
        return KillSwitchToggleResponse(
            enabled=state.enabled,
            updated_at=state.updated_at,
            killed_inflight_run_id=None,
            unwind_warning=None,
        )

    # Transition. Pick the phrase for THIS direction.
    expected_phrase = _DISABLE_PHRASE if current_enabled else _ENABLE_PHRASE
    if submitted != expected_phrase.strip().lower():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"confirm_phrase must equal '{expected_phrase}'",
        )

    # Persist the flag flip.
    await repo.set(_AUTOFIX_ENABLED_KEY, "true" if target_enabled else "false")
    async with db.transaction() as conn:
        await insert_audit(
            conn,
            who=user.username,
            what="autofix.kill_switch_toggled",
            before={"enabled": current_enabled},
            after={"enabled": target_enabled},
            ip=ip,
        )

    killed_inflight_run_id: str | None = None
    unwind_warning: str | None = None

    # on -> off: push-trigger kill_inflight.
    if current_enabled and not target_enabled:
        try:
            kill_result = await orchestrator.kill_inflight(
                reason=f"user_toggle:{user.username}",
                killed_by=f"user:{user.username}",
                ip=ip,
            )
        except DockerSocketError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"failed to kill fixer-runner container: {exc}",
            ) from exc
        killed_inflight_run_id = kill_result.run_id if kill_result.killed else None
        unwind_warning = kill_result.unwind_warning

    state = await _read_state(repo, db)
    return KillSwitchToggleResponse(
        enabled=state.enabled,
        updated_at=state.updated_at,
        killed_inflight_run_id=killed_inflight_run_id,
        unwind_warning=unwind_warning,
    )
