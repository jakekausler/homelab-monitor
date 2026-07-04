"""runbook_runs persistence for the auto-fix orchestrator (STAGE-009-005)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

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
    " ended_at, fixer_user, host, runbook_hash, initiated_by) "
    "VALUES (:id, :runbook_id, :created_at, :alert_id, :mode, :prompt, "
    " :started_at, NULL, :fixer_user, :host, :runbook_hash, :initiated_by)"
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

_UPDATE_KILLED_SQL = text(
    "UPDATE runbook_runs SET killed_at = :killed_at WHERE id = :id AND killed_at IS NULL"
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


def _derive_status_from_row(
    *,
    mode: str,
    ended_at: str | None,
    exit_code: int | None,
    killed_at: str | None,
) -> str:
    """Same rules as the router's ``_derive_outcome``, but returns the
    'last_run_status' string used in ``RunbookStatsRow``. Duplicated here
    (rather than imported from the router) to keep the repo layer independent
    of the API layer.

    Precedence (top wins): killed_at > in_flight > dry_run > success > failure.
    Intentional: a killed run is 'killed' regardless of mode, matching the
    `outcome=killed` filter semantics in RunbookRunsRepository.list_paged.
    Keep the two in lock-step — see test:
    tests/kernel/autofix/test_runs_repository_stats.py::test_status_matches_router_derivation
    and test_dry_run_killed_at_precedence.
    """
    if killed_at is not None:
        return "killed"
    if ended_at is None:
        return "in_flight"
    if mode == "dry_run":
        return "dry_run"
    if exit_code == 0:
        return "success"
    return "failure"


_SELECT_RUN_BY_ID_SQL = text(
    "SELECT id, runbook_id, created_at, alert_id, mode, prompt, started_at, "
    "ended_at, fixer_user, host, runbook_hash, transcript_path, exit_code, "
    "initiated_by, transcript_pruned_at "
    "FROM runbook_runs WHERE id = :id"
)

# ---- STAGE-009-011: runs-history read layer ----

_LIST_PAGED_COLS = (
    "rr.id, rr.runbook_id, rb.path AS runbook_path, "
    "rr.created_at, rr.alert_id, rr.mode, rr.prompt, "
    "rr.started_at, rr.ended_at, rr.fixer_user, rr.host, rr.runbook_hash, "
    "rr.transcript_path, rr.exit_code, rr.initiated_by, rr.killed_at, "
    "rr.transcript_pruned_at"
)

# NB: JOIN is inner — a run without a matching runbook row is a data-integrity
# violation (runbook_id is a NOT NULL FK) so we treat it as invisible rather
# than nullable-out the path. Callers who see a run present in the DB will
# see its path here.

_LIST_PAGED_BASE = (
    f"SELECT {_LIST_PAGED_COLS} FROM runbook_runs rr "
    "INNER JOIN runbooks rb ON rb.id = rr.runbook_id"
)

_COUNT_PAGED_BASE = (
    "SELECT COUNT(*) AS n FROM runbook_runs rr INNER JOIN runbooks rb ON rb.id = rr.runbook_id"
)

_GET_BY_ID_PAGED_SQL = text(
    f"SELECT {_LIST_PAGED_COLS} FROM runbook_runs rr "
    "INNER JOIN runbooks rb ON rb.id = rr.runbook_id "
    "WHERE rr.id = :id"
)

# 30-day per-runbook aggregate. LEFT JOIN from `runbooks` so runbooks with
# 0 runs in the window still appear (all-null stats, run_count_30d=0).
_STATS_AGG_SQL = text(
    """
    SELECT
      r.id AS runbook_id,
      COALESCE(agg.run_count_30d, 0) AS run_count_30d,
      agg.last_run_at,
      agg.real_ended_count,
      agg.real_success_count
    FROM runbooks r
    LEFT JOIN (
      SELECT
        runbook_id,
        COUNT(*) AS run_count_30d,
        MAX(started_at) AS last_run_at,
        SUM(CASE WHEN mode='real' AND ended_at IS NOT NULL THEN 1 ELSE 0 END)
          AS real_ended_count,
        SUM(CASE WHEN mode='real' AND ended_at IS NOT NULL AND exit_code=0
                 THEN 1 ELSE 0 END) AS real_success_count
      FROM runbook_runs
      WHERE started_at >= :window_start
      GROUP BY runbook_id
    ) agg ON agg.runbook_id = r.id
    """
)

# For each runbook that had a most-recent run in the window, fetch the row
# fields we need to derive last_run_status. Uses a window function approach.
# Uses SQLite window functions (ROW_NUMBER OVER PARTITION BY) — requires
# SQLite >= 3.25.0 (2018-09). The aiosqlite driver ships a bundled recent
# SQLite so this is satisfied on all supported platforms.
_STATS_LAST_ROW_SQL = text(
    """
    SELECT runbook_id, mode, ended_at, exit_code, killed_at
    FROM (
      SELECT rr.*,
        ROW_NUMBER() OVER (
          PARTITION BY runbook_id
          ORDER BY started_at DESC, id DESC
        ) AS rn
      FROM runbook_runs rr
      WHERE started_at >= :window_start
    ) t WHERE rn = 1
    """
)


@dataclass(frozen=True, slots=True)
class RunsFilter:
    """Query-string filters for the runs-list endpoint. Fields are all
    optional; any None is skipped when building the WHERE clause.

    ``outcome`` accepts only user-visible filter values (in_flight / success /
    failure / killed); ``dry_run`` is NOT a filter option because the UI
    filters mode='dry_run' directly.
    """

    runbook_id: str | None = None
    mode: str | None = None  # 'dry_run' | 'real'
    outcome: str | None = None  # 'in_flight' | 'success' | 'failure' | 'killed'
    initiator: str | None = None  # 'alert' | 'operator'
    since: str | None = None  # ISO started_at >= since
    until: str | None = None  # ISO started_at < until


@dataclass(frozen=True, slots=True)
class RunRow:
    """Hydrated row from ``runbook_runs`` JOIN ``runbooks`` (for the runs-list
    and run-detail endpoints). Column names match the DB.

    ``transcript_pruned_at`` (STAGE-009-012) is ISO UTC when the transcript
    FILE was deleted by the rotator; the audit ROW is retained. NULL means
    either the file is still present OR was never generated.
    """

    id: str
    runbook_id: str
    runbook_path: str
    created_at: str
    alert_id: str | None
    mode: str
    prompt: str | None
    started_at: str | None
    ended_at: str | None
    fixer_user: str | None
    host: str | None
    runbook_hash: str | None
    transcript_path: str | None
    exit_code: int | None
    initiated_by: str
    killed_at: str | None
    transcript_pruned_at: str | None


@dataclass(frozen=True, slots=True)
class RunbookStatsRow:
    """Hydrated per-runbook 30-day aggregate row.

    ``last_run_status`` is one of {success, failure, killed, in_flight,
    dry_run} or None (no runs in window).
    ``success_rate_30d`` is None if there are no ended, mode='real' runs in
    the window (i.e. denominator zero).
    """

    runbook_id: str
    last_run_at: str | None
    last_run_status: str | None
    success_rate_30d: float | None
    run_count_30d: int


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
        alert_id: str | None,
        prompt: str,
        fixer_user: str,
        host: str,
        runbook_hash: str | None,
        mode: RunMode,
        initiated_by: Literal["alert", "operator"],
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
                "initiated_by": initiated_by,
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

    async def mark_killed_conn(
        self,
        conn: AsyncConnection,
        *,
        run_id: str,
        killed_at: str,
    ) -> None:
        """Conn-taking UPDATE of runbook_runs.killed_at (STAGE-009-007).

        Written on the caller's txn so the killed_at stamp + audit row commit
        atomically. Does NOT touch ended_at — a natural exec exit may still
        stamp ended_at afterwards; the two columns are independent.
        """
        await conn.execute(
            _UPDATE_KILLED_SQL,
            {"id": run_id, "killed_at": killed_at},
        )

    async def mark_transcript_pruned_conn(
        self,
        conn: AsyncConnection,
        *,
        run_id: str,
        pruned_at: str,
    ) -> None:
        """Mark the transcript file for ``run_id`` as pruned.

        Sets ``transcript_path = NULL`` and ``transcript_pruned_at = pruned_at``.
        NEVER deletes the ``runbook_runs`` row (non-negotiable #4 — audit
        immutability). Callers pass the caller's txn so the marker update +
        audit row commit atomically.

        Idempotent: re-marking an already-pruned row is a no-op (both column
        updates are already at their target values).
        """
        await conn.execute(
            text(
                "UPDATE runbook_runs "
                "SET transcript_path = NULL, transcript_pruned_at = :pruned_at "
                "WHERE id = :id"
            ),
            {"id": run_id, "pruned_at": pruned_at},
        )

    async def list_prune_candidates(
        self,
        *,
        keep_last_n: int,
        older_than_iso: str,
    ) -> list[tuple[str, str, str, bool, bool]]:
        """Return runs whose transcript FILE is eligible for rotation.

        A candidate is either:
          - The (row_number > keep_last_n) tail per-runbook, ordered by
            ``started_at DESC, id DESC`` (COUNT-limit rule); OR
          - ``started_at < older_than_iso`` (AGE-limit rule).

        Only rows with ``transcript_path IS NOT NULL AND
        transcript_pruned_at IS NULL`` are returned (never re-prune;
        never touch runs that had no transcript).

        Returns:
          List of ``(run_id, runbook_id, transcript_path, count_exceeded,
          age_exceeded)`` tuples. ``count_exceeded`` is True when the row's
          per-runbook ``rn`` is greater than ``keep_last_n``; ``age_exceeded``
          is True when ``started_at < older_than_iso``. At least one is True
          for every returned row.

        The rotator applies its own path-safety check against the configured
        ``transcript_dir`` base before deleting; this method does no path
        validation.
        """
        sql = text(
            """
            WITH ranked AS (
              SELECT
                id, runbook_id, transcript_path, started_at,
                ROW_NUMBER() OVER (
                  PARTITION BY runbook_id
                  ORDER BY started_at DESC, id DESC
                ) AS rn
              FROM runbook_runs
              WHERE transcript_path IS NOT NULL
                AND transcript_pruned_at IS NULL
            )
            SELECT
              id,
              runbook_id,
              transcript_path,
              CASE WHEN rn > :keep_last_n THEN 1 ELSE 0 END AS count_exceeded,
              CASE WHEN started_at < :older_than THEN 1 ELSE 0 END AS age_exceeded
            FROM ranked
            WHERE rn > :keep_last_n
               OR started_at < :older_than
            ORDER BY runbook_id, started_at DESC, id DESC
            """
        )
        rows = await self._db.fetch_all(
            sql, {"keep_last_n": keep_last_n, "older_than": older_than_iso}
        )
        return [
            (
                str(r.id),
                str(r.runbook_id),
                str(r.transcript_path),
                bool(r.count_exceeded),
                bool(r.age_exceeded),
            )
            for r in rows
        ]

    async def count_distinct_runbooks_with_runs(self) -> int:
        """COUNT distinct runbook_id in runbook_runs. Used by the rotator to
        report ``runbooks_scanned`` in ``RotationOutcome`` — an operator-
        visible sanity check that the rotator saw something to scan.
        """
        row = await self._db.fetch_one(
            text("SELECT COUNT(DISTINCT runbook_id) AS n FROM runbook_runs"),
            {},
        )
        return 0 if row is None else int(row[0])

    async def list_paged(
        self,
        filt: RunsFilter,
        *,
        limit: int,
        offset: int,
    ) -> tuple[list[RunRow], int]:
        """List runs with filters + offset pagination. Returns (rows, total_count).

        Ordering: started_at DESC, id DESC (id tie-break for uuid7 monotonicity
        + stable determinism when two runs share started_at ISO ms).
        """
        clauses: list[str] = []
        params: dict[str, Any] = {}
        if filt.runbook_id is not None:
            clauses.append("rr.runbook_id = :runbook_id")
            params["runbook_id"] = filt.runbook_id
        if filt.mode is not None:
            clauses.append("rr.mode = :mode")
            params["mode"] = filt.mode
        if filt.initiator is not None:
            clauses.append("rr.initiated_by = :initiator")
            params["initiator"] = filt.initiator
        if filt.since is not None:
            clauses.append("rr.started_at >= :since")
            params["since"] = filt.since
        if filt.until is not None:
            clauses.append("rr.started_at < :until")
            params["until"] = filt.until
        if filt.outcome is not None:
            # outcome is DERIVED — translate to SQL predicates.
            if filt.outcome == "in_flight":
                clauses.append("rr.ended_at IS NULL AND rr.killed_at IS NULL")
            elif filt.outcome == "killed":
                clauses.append("rr.killed_at IS NOT NULL")
            elif filt.outcome == "success":
                clauses.append(
                    "rr.ended_at IS NOT NULL AND rr.killed_at IS NULL "
                    "AND rr.mode = 'real' AND rr.exit_code = 0"
                )
            elif filt.outcome == "failure":
                clauses.append(
                    "rr.ended_at IS NOT NULL AND rr.killed_at IS NULL "
                    "AND rr.mode = 'real' AND rr.exit_code != 0"
                )
            else:  # pragma: no cover -- FastAPI Literal narrows to the four above
                pass

        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        list_sql = text(
            f"{_LIST_PAGED_BASE}{where} "
            "ORDER BY rr.started_at DESC, rr.id DESC "
            "LIMIT :limit OFFSET :offset"
        )
        count_sql = text(f"{_COUNT_PAGED_BASE}{where}")

        list_params = {**params, "limit": limit, "offset": offset}
        rows = await self._db.fetch_all(list_sql, list_params)
        count_row = await self._db.fetch_one(count_sql, params)
        total = 0 if count_row is None else int(count_row[0])
        return [self._row_to_run_row(r) for r in rows], total

    async def get_by_id(self, run_id: str) -> RunRow | None:
        """Return one JOINed row (with runbook_path) or None."""
        row = await self._db.fetch_one(_GET_BY_ID_PAGED_SQL, {"id": run_id})
        if row is None:
            return None
        return self._row_to_run_row(row)

    async def stats_per_runbook(self, *, window_start_iso: str) -> list[RunbookStatsRow]:
        """Per-runbook 30-day aggregates. Runbooks with zero runs still appear
        (all-null stats + run_count_30d=0).

        ``window_start_iso`` is the caller-computed ISO cutoff (utc_now - 30d).
        """
        agg_rows = await self._db.fetch_all(_STATS_AGG_SQL, {"window_start": window_start_iso})
        last_rows = await self._db.fetch_all(
            _STATS_LAST_ROW_SQL, {"window_start": window_start_iso}
        )

        last_status_by_runbook: dict[str, str] = {}
        for lr in last_rows:
            status = _derive_status_from_row(
                mode=str(lr.mode),
                ended_at=lr.ended_at,
                exit_code=lr.exit_code,
                killed_at=lr.killed_at,
            )
            last_status_by_runbook[str(lr.runbook_id)] = status

        out: list[RunbookStatsRow] = []
        for r in agg_rows:
            runbook_id = str(r.runbook_id)
            run_count = int(r.run_count_30d)
            real_ended = 0 if r.real_ended_count is None else int(r.real_ended_count)
            real_success = 0 if r.real_success_count is None else int(r.real_success_count)
            success_rate: float | None = None if real_ended == 0 else real_success / real_ended
            out.append(
                RunbookStatsRow(
                    runbook_id=runbook_id,
                    last_run_at=None if r.last_run_at is None else str(r.last_run_at),
                    last_run_status=last_status_by_runbook.get(runbook_id),
                    success_rate_30d=success_rate,
                    run_count_30d=run_count,
                )
            )
        return out

    @staticmethod
    def _row_to_run_row(row: Row[Any]) -> RunRow:
        return RunRow(
            id=str(row.id),
            runbook_id=str(row.runbook_id),
            runbook_path=str(row.runbook_path),
            created_at=str(row.created_at),
            alert_id=None if row.alert_id is None else str(row.alert_id),
            mode=str(row.mode),
            prompt=None if row.prompt is None else str(row.prompt),
            started_at=None if row.started_at is None else str(row.started_at),
            ended_at=None if row.ended_at is None else str(row.ended_at),
            fixer_user=None if row.fixer_user is None else str(row.fixer_user),
            host=None if row.host is None else str(row.host),
            runbook_hash=None if row.runbook_hash is None else str(row.runbook_hash),
            transcript_path=(None if row.transcript_path is None else str(row.transcript_path)),
            exit_code=None if row.exit_code is None else int(row.exit_code),
            initiated_by=str(row.initiated_by),
            killed_at=None if row.killed_at is None else str(row.killed_at),
            transcript_pruned_at=(
                None if row.transcript_pruned_at is None else str(row.transcript_pruned_at)
            ),
        )


__all__ = [
    "RunRow",
    "RunbookRunsRepository",
    "RunbookStatsRow",
    "RunsFilter",
]
