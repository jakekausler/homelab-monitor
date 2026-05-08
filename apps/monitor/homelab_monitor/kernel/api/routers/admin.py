"""POST /api/admin/backup — operator-initiated SQLite + VM backup.

Auth: cookie session OR API token with `admin:backup:write` scope. CSRF
enforced for cookie-authed POSTs (built into `require_user_or_token`).

Audit: every successful run writes one row to `audit_log` with who="operator"
or who="api-token:<id>" and what="admin.backup_run". Errors collected during
the run are reported in the response body and in the audit row's `after` field.
"""

from __future__ import annotations

from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, Request

from homelab_monitor.kernel.api.dependencies import (
    get_backup_service,
    get_repo,
    require_user_or_token,
)
from homelab_monitor.kernel.api.schemas import BackupResponse
from homelab_monitor.kernel.auth.models import ApiToken, User
from homelab_monitor.kernel.auth.scopes import Scope
from homelab_monitor.kernel.backup.service import BackupService
from homelab_monitor.kernel.db.audit import insert_audit
from homelab_monitor.kernel.db.repository import SqliteRepository

router = APIRouter(prefix="/admin", tags=["admin"])


def _principal_label(principal: User | ApiToken) -> str:
    """Format the audit `who` column for a user or token principal."""
    if isinstance(principal, ApiToken):
        return f"api-token:{principal.id}"
    return principal.username


@router.post("/backup", response_model=BackupResponse, status_code=200)
async def run_backup(
    request: Request,
    principal: Annotated[
        User | ApiToken,
        Depends(require_user_or_token({Scope.ADMIN_BACKUP_WRITE})),
    ],
    backup_service: Annotated[BackupService, Depends(get_backup_service)],
    repo: Annotated[SqliteRepository, Depends(get_repo)],
) -> BackupResponse:
    """Run a full backup (SQLite + VM snapshot). Returns the result."""
    log = structlog.get_logger().bind(component="admin.backup")
    log.info("admin.backup.start", who=_principal_label(principal))

    result = await backup_service.run_backup()

    async with repo.transaction() as conn:
        await insert_audit(
            conn,
            who=_principal_label(principal),
            what="admin.backup_run",
            before=None,
            after={
                "snapshot_id": result.snapshot_id,
                "sqlite_path": result.sqlite_path,
                "vm_snapshot_path": result.vm_snapshot_path,
                "size_bytes": result.size_bytes,
                "errors": list(result.errors),
            },
            ip=request.client.host if request.client is not None else None,
        )

    log.info(
        "admin.backup.done",
        snapshot_id=result.snapshot_id,
        size_bytes=result.size_bytes,
        error_count=len(result.errors),
    )

    return BackupResponse(
        snapshot_id=result.snapshot_id,
        sqlite_path=result.sqlite_path,
        vm_snapshot_path=result.vm_snapshot_path,
        started_at=result.started_at,
        ended_at=result.ended_at,
        size_bytes=result.size_bytes,
        errors=list(result.errors),
    )
