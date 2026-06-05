"""Global logs settings endpoints (STAGE-004-022).

GET surfaces effective-vs-pending VL retention, VL disk usage, and warn/crit
thresholds. PATCH persists the DESIRED retention (applied at next restart;
VL's -retentionPeriod is startup-only). Auth: cookie session (CSRF enforced
on PATCH by require_session())."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from homelab_monitor.kernel.api.dependencies import get_repo, require_session
from homelab_monitor.kernel.api.schemas import (
    LogsRetentionResponse,
    LogsRetentionUpdateRequest,
)
from homelab_monitor.kernel.auth.models import User
from homelab_monitor.kernel.config import load_vl_disk_warning_config
from homelab_monitor.kernel.db.repositories.app_settings_repository import (
    AppSettingsRepository,
)
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.logs.vl_retention import (
    RetentionState,
    compute_vl_disk_usage,
    persist_retention,
    resolve_retention,
)

router = APIRouter()


def _get_app_settings_repo(
    repo: Annotated[SqliteRepository, Depends(get_repo)],
) -> AppSettingsRepository:
    return AppSettingsRepository(repo)


def _build_response(state: RetentionState) -> LogsRetentionResponse:
    usage = compute_vl_disk_usage()
    warn = load_vl_disk_warning_config()
    return LogsRetentionResponse(
        retention_days=state.retention_days,
        pending_retention_days=state.pending_retention_days,
        disk_used_gb=usage.disk_used_gb,
        disk_used_pct=usage.disk_used_pct,
        disk_budget_available=usage.budget_available,
        warn_pct=warn.warn_pct,
        crit_pct=warn.crit_pct,
        retention_source=state.retention_source,
        restart_required=state.restart_required,
    )


@router.get("/settings/logs/retention", response_model=LogsRetentionResponse)
async def get_logs_retention(
    _user: Annotated[User, Depends(require_session())],
    repo: Annotated[AppSettingsRepository, Depends(_get_app_settings_repo)],
) -> LogsRetentionResponse:
    """Return effective + pending VL retention, disk usage, and thresholds.

    Auth: cookie session required. CSRF NOT enforced on GET."""
    state = await resolve_retention(repo)
    return _build_response(state)


@router.patch("/settings/logs/retention", response_model=LogsRetentionResponse)
async def patch_logs_retention(
    body: LogsRetentionUpdateRequest,
    _user: Annotated[User, Depends(require_session())],
    repo: Annotated[AppSettingsRepository, Depends(_get_app_settings_repo)],
) -> LogsRetentionResponse:
    """Persist the desired VL retention. 422 on out-of-range (Field ge/le).

    Returns the reconciled state (pending set + restart_required true when the
    new value differs from effective; cleared/no-op when it equals effective).
    Auth: cookie session required; CSRF enforced (PATCH)."""
    state = await persist_retention(repo, body.retention_days)
    return _build_response(state)
