"""runbook_run_feedback persistence (STAGE-009-009).

Mirrors ``RunbookRunApprovalsRepository`` style: ``_conn`` variants share the
caller's transaction; standalone reads open their own via ``self._db``.
"""

from __future__ import annotations

import json
from typing import Any, cast

from sqlalchemy import text
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncConnection

from homelab_monitor.kernel.autofix.feedback_parser import ParsedFeedbackItem
from homelab_monitor.kernel.autofix.types import (
    FeedbackKind,
    RunbookRunFeedback,
)
from homelab_monitor.kernel.db.ids import uuid7
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.db.time import utc_now_iso

_COLS = "id, runbook_run_id, kind, suggestion_text, structured_hint, created_at"

_INSERT_SQL = text(
    "INSERT INTO runbook_run_feedback "
    "(id, runbook_run_id, kind, suggestion_text, structured_hint, created_at) "
    "VALUES (:id, :runbook_run_id, :kind, :suggestion_text, :structured_hint, "
    " :created_at)"
)

_SELECT_BY_RUN_SQL = text(
    f"SELECT {_COLS} FROM runbook_run_feedback "
    "WHERE runbook_run_id = :runbook_run_id "
    "ORDER BY created_at ASC, id ASC"
)


def _row_to_feedback(row: Row[Any]) -> RunbookRunFeedback:
    hint_raw = row.structured_hint
    hint: dict[str, object] | None = None
    if hint_raw is not None:
        parsed: object = json.loads(str(hint_raw))
        if isinstance(parsed, dict):
            hint = cast(dict[str, object], parsed)
    return RunbookRunFeedback(
        id=str(row.id),
        runbook_run_id=str(row.runbook_run_id),
        kind=FeedbackKind(str(row.kind)),
        suggestion_text=str(row.suggestion_text),
        structured_hint=hint,
        created_at=str(row.created_at),
    )


class RunbookRunFeedbackRepository:
    """Reads/writes ``runbook_run_feedback``."""

    def __init__(self, db: SqliteRepository) -> None:
        self._db = db

    async def insert_conn(
        self,
        conn: AsyncConnection,
        *,
        runbook_run_id: str,
        item: ParsedFeedbackItem,
        feedback_id: str | None = None,
        created_at: str | None = None,
    ) -> RunbookRunFeedback:
        """INSERT one feedback row on the caller's connection. Returns the
        hydrated record. ``feedback_id`` and ``created_at`` default to a
        fresh uuid7 + utc_now_iso; callers may pass explicit values for
        deterministic tests.
        """
        fid = feedback_id if feedback_id is not None else uuid7()
        now = created_at if created_at is not None else utc_now_iso()
        hint_json = json.dumps(item.structured_hint) if item.structured_hint is not None else None
        await conn.execute(
            _INSERT_SQL,
            {
                "id": fid,
                "runbook_run_id": runbook_run_id,
                "kind": item.kind.value,
                "suggestion_text": item.suggestion_text,
                "structured_hint": hint_json,
                "created_at": now,
            },
        )
        return RunbookRunFeedback(
            id=fid,
            runbook_run_id=runbook_run_id,
            kind=item.kind,
            suggestion_text=item.suggestion_text,
            structured_hint=item.structured_hint,
            created_at=now,
        )

    async def list_by_run(self, runbook_run_id: str) -> list[RunbookRunFeedback]:
        """Read all feedback for a run, oldest-first (deterministic tie-break by id)."""
        rows = await self._db.fetch_all(_SELECT_BY_RUN_SQL, {"runbook_run_id": runbook_run_id})
        return [_row_to_feedback(r) for r in rows]


__all__ = ["RunbookRunFeedbackRepository"]
