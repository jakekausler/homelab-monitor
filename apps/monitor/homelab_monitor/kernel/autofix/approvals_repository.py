"""runbook_run_approvals persistence for the dry-run approval flow (STAGE-009-006).

Mirrors RunbookRunsRepository style: conn-taking (``_conn``) variants share a
caller's transaction; own-txn variants open their own transaction (and, for
reject, write the audit row in the same txn).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncConnection

from homelab_monitor.kernel.db.audit import insert_audit
from homelab_monitor.kernel.db.ids import uuid7
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.db.time import utc_now_iso


@dataclass(frozen=True, slots=True)
class ApprovalRecord:
    """A hydrated row from ``runbook_run_approvals``."""

    id: str
    dry_run_id: str
    runbook_id: str
    alert_id: str | None
    pinned_runbook_hash: str | None
    status: str
    approved_by: str | None
    decided_at: str | None
    real_run_id: str | None
    created_at: str


_COLS = (
    "id, dry_run_id, runbook_id, alert_id, pinned_runbook_hash, status, "
    "approved_by, decided_at, real_run_id, created_at"
)

_INSERT_SQL = text(
    "INSERT INTO runbook_run_approvals "
    "(id, dry_run_id, runbook_id, alert_id, pinned_runbook_hash, status, "
    " approved_by, decided_at, real_run_id, created_at) "
    "VALUES (:id, :dry_run_id, :runbook_id, :alert_id, :pinned_runbook_hash, "
    " 'pending', NULL, NULL, NULL, :created_at)"
)

_SELECT_BY_ID_SQL = text(f"SELECT {_COLS} FROM runbook_run_approvals WHERE id = :id")

_SELECT_BY_STATUS_SQL = text(
    f"SELECT {_COLS} FROM runbook_run_approvals WHERE status = :status ORDER BY created_at DESC"
)

_MARK_APPROVED_SQL = text(
    "UPDATE runbook_run_approvals "
    "SET status = 'approved', approved_by = :approved_by, decided_at = :decided_at "
    "WHERE id = :id AND status = 'pending'"
)

_MARK_REJECTED_SQL = text(
    "UPDATE runbook_run_approvals "
    "SET status = 'rejected', approved_by = :approved_by, decided_at = :decided_at "
    "WHERE id = :id AND status = 'pending'"
)

_REVERT_TO_PENDING_SQL = text(
    "UPDATE runbook_run_approvals "
    "SET status = 'pending', approved_by = NULL, decided_at = NULL "
    "WHERE id = :id AND status = 'approved' AND real_run_id IS NULL"
)

_SET_REAL_RUN_ID_SQL = text(
    "UPDATE runbook_run_approvals SET real_run_id = :real_run_id WHERE id = :id"
)


def _row_to_approval(row: Row[Any]) -> ApprovalRecord:
    return ApprovalRecord(
        id=str(row.id),
        dry_run_id=str(row.dry_run_id),
        runbook_id=str(row.runbook_id),
        alert_id=None if row.alert_id is None else str(row.alert_id),
        pinned_runbook_hash=(
            None if row.pinned_runbook_hash is None else str(row.pinned_runbook_hash)
        ),
        status=str(row.status),
        approved_by=None if row.approved_by is None else str(row.approved_by),
        decided_at=None if row.decided_at is None else str(row.decided_at),
        real_run_id=None if row.real_run_id is None else str(row.real_run_id),
        created_at=str(row.created_at),
    )


class RunbookRunApprovalsRepository:
    """Reads/writes ``runbook_run_approvals``."""

    def __init__(self, db: SqliteRepository) -> None:
        self._db = db

    async def insert_pending(
        self,
        conn: AsyncConnection,
        *,
        dry_run_id: str,
        runbook_id: str,
        alert_id: str | None,
        pinned_runbook_hash: str | None,
    ) -> str:
        """INSERT a status='pending' approval on the given connection; return id."""
        approval_id = uuid7()
        await conn.execute(
            _INSERT_SQL,
            {
                "id": approval_id,
                "dry_run_id": dry_run_id,
                "runbook_id": runbook_id,
                "alert_id": alert_id,
                "pinned_runbook_hash": pinned_runbook_hash,
                "created_at": utc_now_iso(),
            },
        )
        return approval_id

    async def get(self, approval_id: str) -> ApprovalRecord | None:
        row = await self._db.fetch_one(_SELECT_BY_ID_SQL, {"id": approval_id})
        return None if row is None else _row_to_approval(row)

    async def list_by_status(self, status: str) -> list[ApprovalRecord]:
        rows = await self._db.fetch_all(_SELECT_BY_STATUS_SQL, {"status": status})
        return [_row_to_approval(r) for r in rows]

    async def mark_approved_conn(
        self, conn: AsyncConnection, *, approval_id: str, approved_by: str, when: str
    ) -> int:
        """Guard-approve the given approval; return SQL rowcount.

        The UPDATE has an ``AND status = 'pending'`` guard, so:
          - rowcount == 1 : this call landed the approval.
          - rowcount == 0 : someone else already approved/rejected it (race).
        """
        result = await conn.execute(
            _MARK_APPROVED_SQL,
            {"id": approval_id, "approved_by": approved_by, "decided_at": when},
        )
        return result.rowcount

    async def mark_rejected_conn(
        self, conn: AsyncConnection, *, approval_id: str, approved_by: str, when: str
    ) -> int:
        """Guard-reject the given approval; return SQL rowcount.

        The UPDATE has an ``AND status = 'pending'`` guard, so:
          - rowcount == 1 : this call landed the rejection.
          - rowcount == 0 : someone else already approved/rejected it (race).
        """
        result = await conn.execute(
            _MARK_REJECTED_SQL,
            {"id": approval_id, "approved_by": approved_by, "decided_at": when},
        )
        return result.rowcount

    async def revert_to_pending_conn(
        self,
        conn: AsyncConnection,
        *,
        approval_id: str,
    ) -> int:
        """Revert approved -> pending. Used when the real claim denies post-approval,
        so the approval can be retried without an orphaned 'approved' record.
        Returns rowcount (1 = reverted, 0 = state changed underneath us)."""
        result = await conn.execute(_REVERT_TO_PENDING_SQL, {"id": approval_id})
        return result.rowcount

    async def set_real_run_id_conn(
        self, conn: AsyncConnection, *, approval_id: str, real_run_id: str
    ) -> None:
        await conn.execute(_SET_REAL_RUN_ID_SQL, {"id": approval_id, "real_run_id": real_run_id})

    async def mark_rejected(
        self, *, approval_id: str, approved_by: str, when: str | None = None, ip: str | None = None
    ) -> int:
        """Own-txn reject + ``autofix.rejected`` audit in ONE transaction (router use).

        Returns the ``mark_rejected_conn`` rowcount (1 = rejection landed,
        0 = the approval was already decided by someone else). On rowcount==0
        the audit is still written (records the attempt) — callers that need
        to skip the audit on the race path should call ``mark_rejected_conn``
        directly.
        """
        decided_at = when if when is not None else utc_now_iso()
        async with self._db.transaction() as conn:
            rowcount = await self.mark_rejected_conn(
                conn,
                approval_id=approval_id,
                approved_by=approved_by,
                when=decided_at,
            )
            await insert_audit(
                conn,
                who=approved_by,
                what="autofix.rejected",
                after={"approval_id": approval_id, "decided_by": approved_by},
                ip=ip,
            )
        return rowcount


__all__ = ["ApprovalRecord", "RunbookRunApprovalsRepository"]
