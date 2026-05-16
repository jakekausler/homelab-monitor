"""Async repository for the heartbeat receiver.

State-changing methods route through a single private
``_record_state_transition`` helper that, in ONE transaction:

1. Reads the previous ``heartbeats_state`` row (for streak math).
2. UPSERTs the new ``heartbeats_state`` row.
3. UPDATEs ``crons.last_seen_state`` (Decision 4 mirror).
4. Inserts an ``audit_log`` row.

Streak rules:
- New state == previous state -> ``current_streak + 1``.
- Different state OR no previous row -> ``current_streak = 1``.

``expected_next_at`` is computed only on the OK transition AND only when the
cron's ``cadence_seconds > 0``. Cadence derivation from the cron expression
(``croniter``) is OUT OF SCOPE for this stage; cadence stays 0 until the
crons-CRUD stage (002-002) populates it.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncConnection

from homelab_monitor.kernel.cron.repository import (
    CronRecord,
    HeartbeatStateRecord,
)
from homelab_monitor.kernel.db.audit import insert_audit
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.db.time import utc_now_iso


def _row_to_cron(row: Row[Any]) -> CronRecord:
    return CronRecord(
        fingerprint=str(row.fingerprint),
        name=str(row.name),
        host=str(row.host),
        command=str(row.command),
        schedule="" if row.schedule is None else str(row.schedule),
        schedule_canonical=(
            None if row.schedule_canonical is None else str(row.schedule_canonical)
        ),
        cadence_seconds=int(row.cadence_seconds),
        expected_grace_seconds=int(row.expected_grace_seconds),
        enabled=bool(row.enabled),
        last_seen_state=str(row.last_seen_state),
        created_at=str(row.created_at),
        updated_at=str(row.updated_at),
        hidden_at=None if row.hidden_at is None else str(row.hidden_at),
        source_path=None if row.source_path is None else str(row.source_path),
        wrapper_last_seen_at=(
            None if row.wrapper_last_seen_at is None else str(row.wrapper_last_seen_at)
        ),
        last_discovered_at=(
            None if row.last_discovered_at is None else str(row.last_discovered_at)
        ),
        soft_deleted_at=(None if row.soft_deleted_at is None else str(row.soft_deleted_at)),
        log_match_key=(None if row.log_match_key is None else str(row.log_match_key)),
    )


def _row_to_state(row: Row[Any]) -> HeartbeatStateRecord:
    return HeartbeatStateRecord(
        cron_fingerprint=str(row.cron_fingerprint),
        current_state=str(row.current_state),
        last_start_at=None if row.last_start_at is None else str(row.last_start_at),
        last_ok_at=None if row.last_ok_at is None else str(row.last_ok_at),
        last_fail_at=None if row.last_fail_at is None else str(row.last_fail_at),
        current_streak=int(row.current_streak),
        expected_next_at=(None if row.expected_next_at is None else str(row.expected_next_at)),
        last_duration_seconds=(
            None if row.last_duration_seconds is None else float(row.last_duration_seconds)
        ),
        last_exit_code=None if row.last_exit_code is None else int(row.last_exit_code),
        updated_at=str(row.updated_at),
        observed_runs_total=int(row.observed_runs_total),
        last_observed_run_at=(
            None if row.last_observed_run_at is None else str(row.last_observed_run_at)
        ),
    )


_SELECT_CRON_SQL = text(
    "SELECT fingerprint, name, host, command, schedule, schedule_canonical, "
    "cadence_seconds, expected_grace_seconds, enabled, last_seen_state, "
    "created_at, updated_at, hidden_at, source_path, wrapper_last_seen_at, "
    "last_discovered_at, soft_deleted_at, log_match_key "
    "FROM crons WHERE fingerprint = :fingerprint"
)

_SELECT_STATE_SQL = text(
    "SELECT cron_fingerprint, current_state, last_start_at, last_ok_at, "
    "last_fail_at, current_streak, expected_next_at, last_duration_seconds, "
    "last_exit_code, updated_at, observed_runs_total, last_observed_run_at FROM heartbeats_state "
    "WHERE cron_fingerprint = :cron_fingerprint"
)

_UPSERT_STATE_SQL = text(
    "INSERT INTO heartbeats_state ("
    "  cron_fingerprint, current_state, last_start_at, last_ok_at, last_fail_at, "
    "  current_streak, expected_next_at, last_duration_seconds, "
    "  last_exit_code, updated_at, observed_runs_total, last_observed_run_at"
    ") VALUES ("
    "  :cron_fingerprint, :current_state, :last_start_at, :last_ok_at, "
    "  :last_fail_at, :current_streak, :expected_next_at, "
    "  :last_duration_seconds, :last_exit_code, :updated_at, "
    "  :observed_runs_total, :last_observed_run_at"
    ") "
    "ON CONFLICT(cron_fingerprint) DO UPDATE SET "
    "  current_state = excluded.current_state, "
    "  last_start_at = excluded.last_start_at, "
    "  last_ok_at = excluded.last_ok_at, "
    "  last_fail_at = excluded.last_fail_at, "
    "  current_streak = excluded.current_streak, "
    "  expected_next_at = excluded.expected_next_at, "
    "  last_duration_seconds = excluded.last_duration_seconds, "
    "  last_exit_code = excluded.last_exit_code, "
    "  updated_at = excluded.updated_at"
)

_UPDATE_CRONS_LAST_SEEN_SQL = text(
    "UPDATE crons SET last_seen_state = :state, updated_at = :updated_at "
    "WHERE fingerprint = :fingerprint"
)

_UPSERT_OBSERVED_RUN_SQL = text(
    "INSERT INTO heartbeats_state ("
    "  cron_fingerprint, current_state, last_start_at, last_ok_at, last_fail_at, "
    "  current_streak, expected_next_at, last_duration_seconds, last_exit_code, "
    "  updated_at, observed_runs_total, last_observed_run_at"
    ") VALUES ("
    "  :cron_fingerprint, 'unknown', NULL, NULL, NULL, 0, NULL, NULL, NULL, "
    "  :now, 1, :observed_at"
    ") "
    "ON CONFLICT(cron_fingerprint) DO UPDATE SET "
    "  observed_runs_total = heartbeats_state.observed_runs_total + 1, "
    "  last_observed_run_at = excluded.last_observed_run_at, "
    "  updated_at = excluded.updated_at"
)


def compute_expected_next_at(
    *,
    last_ok_at_iso: str,
    cadence_seconds: int,
    grace_seconds: int,
) -> str | None:
    """Return ISO-8601 UTC for ``last_ok_at + cadence + grace``.

    Returns ``None`` when ``cadence_seconds <= 0`` (cadence not yet derived).
    """
    if cadence_seconds <= 0:
        return None
    base = datetime.fromisoformat(last_ok_at_iso)
    if base.tzinfo is None:
        msg = f"last_ok_at must be tz-aware ISO; got: {last_ok_at_iso!r}"
        raise ValueError(msg)
    delta = timedelta(seconds=cadence_seconds + grace_seconds)
    return (base + delta).isoformat()


class HeartbeatRepo:
    """Async CRUD for the heartbeat subsystem.

    Public mutators (``record_start``, ``record_ok``, ``record_fail``) all
    funnel through ``_record_state_transition`` so the dual-write to
    ``heartbeats_state`` + ``crons.last_seen_state`` + ``audit_log`` is
    expressed in exactly one place (Decision 4: mirror, dual-write).
    """

    def __init__(self, db: SqliteRepository) -> None:
        self._db = db

    # ----- reads -----

    async def get_cron(self, fingerprint: str) -> CronRecord | None:
        """Return the cron with ``fingerprint == fingerprint``, or ``None``."""
        row = await self._db.fetch_one(_SELECT_CRON_SQL, {"fingerprint": fingerprint})
        if row is None:
            return None
        return _row_to_cron(row)

    async def get_heartbeat_state(self, fingerprint: str) -> HeartbeatStateRecord | None:
        """Return the heartbeat state for ``fingerprint``, or ``None`` if no pings yet."""
        row = await self._db.fetch_one(_SELECT_STATE_SQL, {"cron_fingerprint": fingerprint})
        if row is None:
            return None
        return _row_to_state(row)

    async def list_crons(self) -> list[CronRecord]:
        """Return all crons ordered by ``name`` (helper for tests / future Inventory).

        Includes hidden crons by design — this is the legacy debugging helper
        that pre-dates the soft-delete model. Production list queries go
        through ``CronRepo.list_crons``.
        """
        rows = await self._db.fetch_all(
            text(
                "SELECT fingerprint, name, host, command, schedule, schedule_canonical, "
                "cadence_seconds, expected_grace_seconds, enabled, last_seen_state, "
                "created_at, updated_at, hidden_at, source_path, wrapper_last_seen_at, "
                "last_discovered_at, soft_deleted_at, log_match_key "
                "FROM crons ORDER BY name, fingerprint"
            )
        )
        return [_row_to_cron(r) for r in rows]

    # ----- mutators (public) -----

    async def record_start(
        self,
        fingerprint: str,
        *,
        who: str,
        ip: str | None,
    ) -> HeartbeatStateRecord:
        """Record a ``/start`` ping. Returns the updated state row.

        Sets ``current_state='running'``, ``last_start_at=now``. Streak math
        per the class docstring.
        """
        now = utc_now_iso()
        return await self._record_state_transition(
            fingerprint=fingerprint,
            new_state="running",
            now=now,
            last_start_at=now,
            who=who,
            ip=ip,
            audit_what="heartbeat.start",
        )

    async def record_ok(
        self,
        fingerprint: str,
        *,
        duration_seconds: float | None,
        who: str,
        ip: str | None,
    ) -> HeartbeatStateRecord:
        """Record an ``/ok`` ping. Returns the updated state row.

        Sets ``current_state='ok'``, ``last_ok_at=now``, optional
        ``last_duration_seconds``. Computes ``expected_next_at`` if the cron's
        ``cadence_seconds > 0``.
        """
        now = utc_now_iso()
        return await self._record_state_transition(
            fingerprint=fingerprint,
            new_state="ok",
            now=now,
            last_ok_at=now,
            duration_seconds=duration_seconds,
            who=who,
            ip=ip,
            audit_what="heartbeat.ok",
        )

    async def record_fail(
        self,
        fingerprint: str,
        *,
        duration_seconds: float | None,
        exit_code: int | None,
        who: str,
        ip: str | None,
    ) -> HeartbeatStateRecord:
        """Record a ``/fail`` ping. Returns the updated state row."""
        now = utc_now_iso()
        return await self._record_state_transition(
            fingerprint=fingerprint,
            new_state="failed",
            now=now,
            last_fail_at=now,
            duration_seconds=duration_seconds,
            exit_code=exit_code,
            who=who,
            ip=ip,
            audit_what="heartbeat.fail",
        )

    async def record_observed_run(
        self,
        fingerprint: str,
        *,
        observed_at: str,
        who: str,
        ip: str | None,
    ) -> HeartbeatStateRecord:
        """Record a NEUTRAL observed run from B-mode log evidence (D1).

        A vanilla cron dispatch line proves "cron fired the job", not "the job
        succeeded". So this increments ``observed_runs_total``, sets
        ``last_observed_run_at`` + ``updated_at``, and DOES NOT touch
        ``current_state``, ``last_ok_at``, ``last_fail_at``, ``current_streak``,
        ``last_exit_code``, or ``expected_next_at``. It does NOT mirror to
        ``crons.last_seen_state``.

        Writes an ``audit_log`` row with ``what="cron.observed_run"`` in the SAME
        transaction. Caller MUST have verified ``fingerprint`` exists.
        """
        async with self._db.engine.begin() as conn:
            cron = await self._fetch_cron_in_conn(conn, fingerprint)
            if cron is None:  # pragma: no cover -- caller already verified
                msg = f"cron not found: {fingerprint}"
                raise LookupError(msg)
            previous = await self._fetch_state_in_conn(conn, fingerprint)
            await conn.execute(
                _UPSERT_OBSERVED_RUN_SQL,
                {
                    "cron_fingerprint": fingerprint,
                    "now": observed_at,
                    "observed_at": observed_at,
                },
            )
            new_row = await self._fetch_state_in_conn(conn, fingerprint)
            assert new_row is not None
            await insert_audit(
                conn,
                who=who,
                what="cron.observed_run",
                before=(
                    None
                    if previous is None
                    else {
                        "cron_fingerprint": fingerprint,
                        "observed_runs_total": previous.observed_runs_total,
                    }
                ),
                after={
                    "cron_fingerprint": fingerprint,
                    "observed_runs_total": new_row.observed_runs_total,
                    "host": cron.host,
                },
                ip=ip,
                when=observed_at,
            )
            return new_row

    # ----- single chokepoint for state writes -----

    async def _record_state_transition(  # noqa: PLR0913
        self,
        *,
        fingerprint: str,
        new_state: str,
        now: str,
        last_start_at: str | None = None,
        last_ok_at: str | None = None,
        last_fail_at: str | None = None,
        duration_seconds: float | None = None,
        exit_code: int | None = None,
        who: str,
        ip: str | None,
        audit_what: str,
    ) -> HeartbeatStateRecord:
        """Atomic upsert of state + crons mirror + audit row.

        Caller MUST have already verified ``fingerprint`` exists (404 happens at
        the router; this method assumes the row is present).
        """
        async with self._db.engine.begin() as conn:
            cron = await self._fetch_cron_in_conn(conn, fingerprint)
            if cron is None:  # pragma: no cover  # defensive; router already 404'd
                msg = f"cron not found: {fingerprint}"
                raise LookupError(msg)
            previous = await self._fetch_state_in_conn(conn, fingerprint)

            new_streak = self._compute_streak(previous=previous, new_state=new_state)
            new_last_start_at = self._merge_optional(
                previous=None if previous is None else previous.last_start_at,
                provided=last_start_at,
            )
            new_last_ok_at = self._merge_optional(
                previous=None if previous is None else previous.last_ok_at,
                provided=last_ok_at,
            )
            new_last_fail_at = self._merge_optional(
                previous=None if previous is None else previous.last_fail_at,
                provided=last_fail_at,
            )
            new_duration = (
                duration_seconds
                if duration_seconds is not None
                else (None if previous is None else previous.last_duration_seconds)
            )
            new_exit_code = (
                exit_code
                if exit_code is not None
                else (None if previous is None else previous.last_exit_code)
            )

            new_expected_next: str | None
            if new_state == "ok" and last_ok_at is not None:
                new_expected_next = compute_expected_next_at(
                    last_ok_at_iso=last_ok_at,
                    cadence_seconds=cron.cadence_seconds,
                    grace_seconds=cron.expected_grace_seconds,
                )
            else:
                # Non-OK transitions (start/fail) clear the deadline so vmalert
                # rules in STAGE-002-006 don't fire phantom "late" alerts on
                # a job that's known-failed.
                new_expected_next = None

            await conn.execute(
                _UPSERT_STATE_SQL,
                {
                    "cron_fingerprint": fingerprint,
                    "current_state": new_state,
                    "last_start_at": new_last_start_at,
                    "last_ok_at": new_last_ok_at,
                    "last_fail_at": new_last_fail_at,
                    "current_streak": new_streak,
                    "expected_next_at": new_expected_next,
                    "last_duration_seconds": new_duration,
                    "last_exit_code": new_exit_code,
                    "updated_at": now,
                    "observed_runs_total": (
                        0 if previous is None else previous.observed_runs_total
                    ),
                    "last_observed_run_at": (
                        None if previous is None else previous.last_observed_run_at
                    ),
                },
            )
            await conn.execute(
                _UPDATE_CRONS_LAST_SEEN_SQL,
                {"state": new_state, "updated_at": now, "fingerprint": fingerprint},
            )
            await insert_audit(
                conn,
                who=who,
                what=audit_what,
                before=(
                    None
                    if previous is None
                    else {
                        "cron_fingerprint": fingerprint,
                        "current_state": previous.current_state,
                        "current_streak": previous.current_streak,
                    }
                ),
                after={
                    "cron_fingerprint": fingerprint,
                    "current_state": new_state,
                    "current_streak": new_streak,
                    "duration_seconds": new_duration,
                    "exit_code": new_exit_code,
                    "host": cron.host,
                },
                ip=ip,
                when=now,
            )

            new_row = await self._fetch_state_in_conn(conn, fingerprint)
            assert new_row is not None  # we just upserted it
            return new_row

    # ----- private helpers -----

    @staticmethod
    def _compute_streak(
        *,
        previous: HeartbeatStateRecord | None,
        new_state: str,
    ) -> int:
        if previous is None:
            return 1
        if previous.current_state == new_state:
            return previous.current_streak + 1
        return 1

    @staticmethod
    def _merge_optional(*, previous: str | None, provided: str | None) -> str | None:
        """If the caller supplied a value, use it; else preserve the previous one."""
        if provided is not None:
            return provided
        return previous

    @staticmethod
    async def _fetch_cron_in_conn(conn: AsyncConnection, fingerprint: str) -> CronRecord | None:
        result = await conn.execute(_SELECT_CRON_SQL, {"fingerprint": fingerprint})
        row = result.first()
        return None if row is None else _row_to_cron(row)

    @staticmethod
    async def _fetch_state_in_conn(
        conn: AsyncConnection, fingerprint: str
    ) -> HeartbeatStateRecord | None:
        result = await conn.execute(_SELECT_STATE_SQL, {"cron_fingerprint": fingerprint})
        row = result.first()
        return None if row is None else _row_to_state(row)


__all__ = [
    "CronRecord",
    "HeartbeatRepo",
    "HeartbeatStateRecord",
    "compute_expected_next_at",
]
