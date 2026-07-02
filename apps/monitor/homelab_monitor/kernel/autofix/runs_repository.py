"""runbook_runs persistence for the auto-fix orchestrator (STAGE-009-005)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncConnection

from homelab_monitor.kernel.autofix.types import RunMode
from homelab_monitor.kernel.db.ids import uuid7
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.db.time import utc_now_iso

_INSERT_STARTED_SQL = text(
    "INSERT INTO runbook_runs "
    "(id, runbook_id, created_at, alert_id, mode, prompt, started_at, "
    " ended_at, fixer_user, host, runbook_hash) "
    "VALUES (:id, :runbook_id, :created_at, :alert_id, :mode, :prompt, "
    " :started_at, NULL, :fixer_user, :host, :runbook_hash)"
)

_COUNT_INFLIGHT_SQL = text(
    "SELECT COUNT(*) AS n FROM runbook_runs "
    "WHERE runbook_id = :runbook_id AND ended_at IS NULL "
    "AND started_at >= :stale_threshold"
)

_UPDATE_COMPLETION_SQL = text(
    "UPDATE runbook_runs "
    "SET ended_at = :ended_at, exit_code = :exit_code, "
    "    transcript_path = :transcript_path "
    "WHERE id = :id"
)

_COUNT_RECENT_SQL = text(
    "SELECT COUNT(*) AS n FROM runbook_runs "
    "WHERE runbook_id = :runbook_id AND started_at >= :threshold"
)

_LATEST_ENDED_SQL = text(
    "SELECT ended_at FROM runbook_runs "
    "WHERE runbook_id = :runbook_id AND ended_at IS NOT NULL "
    "ORDER BY ended_at DESC LIMIT 1"
)

_SELECT_RUN_BY_ID_SQL = text(
    "SELECT id, runbook_id, created_at, alert_id, mode, prompt, started_at, "
    "ended_at, fixer_user, host, runbook_hash, transcript_path, exit_code "
    "FROM runbook_runs WHERE id = :id"
)


class RunbookRunsRepository:
    """Reads/writes the ``runbook_runs`` table for the orchestrator."""

    def __init__(self, db: SqliteRepository) -> None:
        self._db = db

    async def count_inflight(
        self, conn: AsyncConnection, runbook_id: str, *, stale_threshold_iso: str
    ) -> int:
        """COUNT of open-ended (ended_at IS NULL) runs for this runbook, EXCLUDING
        stale claims (started_at older than ``stale_threshold_iso``).

        Takes an EXISTING connection so the check + insert are one transaction.
        A claim older than the stale threshold is treated as NOT inflight so a
        crashed/orphaned run (ended_at never set) self-heals after the max exec
        window (Important #1a). The full reaper that marks such rows ended is
        owned by STAGE-009-007.
        """
        result = await conn.execute(
            _COUNT_INFLIGHT_SQL,
            {"runbook_id": runbook_id, "stale_threshold": stale_threshold_iso},
        )
        row = result.first()
        return 0 if row is None else int(row[0])

    async def insert_started(  # noqa: PLR0913 -- keyword-only runbook_runs columns
        self,
        conn: AsyncConnection,
        *,
        runbook_id: str,
        alert_id: str,
        prompt: str,
        fixer_user: str,
        host: str,
        runbook_hash: str | None,
        mode: RunMode,
    ) -> str:
        """INSERT a started (ended_at NULL) row on the given connection; return run id."""
        run_id = uuid7()
        now = utc_now_iso()
        await conn.execute(
            _INSERT_STARTED_SQL,
            {
                "id": run_id,
                "runbook_id": runbook_id,
                "created_at": now,
                "alert_id": alert_id,
                "mode": mode.value,
                "prompt": prompt,
                "started_at": now,
                "fixer_user": fixer_user,
                "host": host,
                "runbook_hash": runbook_hash,
            },
        )
        return run_id

    async def mark_completed(
        self,
        *,
        run_id: str,
        exit_code: int,
        transcript_path: str | None,
    ) -> None:
        """UPDATE the run row with ended_at + exit_code + transcript_path (own txn)."""
        async with self._db.transaction() as conn:
            await conn.execute(
                _UPDATE_COMPLETION_SQL,
                {
                    "id": run_id,
                    "ended_at": utc_now_iso(),
                    "exit_code": exit_code,
                    "transcript_path": transcript_path,
                },
            )

    async def count_started_since(self, runbook_id: str, threshold_iso: str) -> int:
        """COUNT runs whose started_at >= threshold_iso (sliding rate-limit window)."""
        row = await self._db.fetch_one(
            _COUNT_RECENT_SQL,
            {"runbook_id": runbook_id, "threshold": threshold_iso},
        )
        return 0 if row is None else int(row[0])

    async def latest_ended_at(self, runbook_id: str) -> str | None:
        """ISO ended_at of the most recently completed run, or None."""
        row = await self._db.fetch_one(_LATEST_ENDED_SQL, {"runbook_id": runbook_id})
        if row is None:
            return None
        value = row[0]
        return None if value is None else str(value)

    async def get(self, run_id: str) -> Row[Any] | None:
        """Return the full runbook_runs row for ``run_id`` (or None)."""
        return await self._db.fetch_one(_SELECT_RUN_BY_ID_SQL, {"id": run_id})

    async def count_started_since_conn(
        self, conn: AsyncConnection, runbook_id: str, threshold_iso: str
    ) -> int:
        """Conn-taking COUNT runs whose started_at >= threshold_iso (rate-limit window).

        Variant of ``count_started_since`` that runs on a caller-supplied connection
        so the rate-limit re-check is atomic with the inflight check + insert.
        """
        result = await conn.execute(
            _COUNT_RECENT_SQL,
            {"runbook_id": runbook_id, "threshold": threshold_iso},
        )
        row = result.first()
        return 0 if row is None else int(row[0])

    async def latest_ended_at_conn(self, conn: AsyncConnection, runbook_id: str) -> str | None:
        """Conn-taking ISO ended_at of the most recently completed run, or None.

        Variant of ``latest_ended_at`` that runs on a caller-supplied connection
        so the cooldown re-check is atomic with the inflight check + insert.
        """
        result = await conn.execute(_LATEST_ENDED_SQL, {"runbook_id": runbook_id})
        row = result.first()
        if row is None:
            return None
        value = row[0]
        return None if value is None else str(value)

    async def mark_completed_conn(
        self,
        conn: AsyncConnection,
        *,
        run_id: str,
        exit_code: int,
        transcript_path: str | None,
    ) -> None:
        """Conn-taking UPDATE of ended_at + exit_code + transcript_path.

        Variant of ``mark_completed`` that runs on a caller-supplied connection so
        the completion UPDATE + audit (+ outcome insert) are one atomic txn
        (Important #2).
        """
        await conn.execute(
            _UPDATE_COMPLETION_SQL,
            {
                "id": run_id,
                "ended_at": utc_now_iso(),
                "exit_code": exit_code,
                "transcript_path": transcript_path,
            },
        )
