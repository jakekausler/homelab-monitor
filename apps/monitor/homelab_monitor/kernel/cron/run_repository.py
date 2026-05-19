"""Async CRUD repository for the cron_runs table (per-invocation run history).

Mirrors kernel/cron/repository.py — SQLAlchemy Core (not ORM), text() SQL
constants, a frozen slotted CronRunRecord dataclass, a _row_to_cron_run
hydrator. STAGE-002-011 writes only `source='wrapper'` (A-mode) rows via the
heartbeat receiver; B-mode rows and VL-enrichment columns land in later stages.

run_id is the TEXT PRIMARY KEY:
- insert_run uses INSERT OR IGNORE — a duplicate run_id on /start is an
  idempotent no-op (wrapper retry / replay).
- close_run uses ON CONFLICT DO UPDATE (UPSERT) — a /ok or /fail with no prior
  /start row (a lost /start, a NORMAL case because heartbeat POSTs are
  best-effort `curl --max-time 5 ... || true`) still yields one closed row. On
  that lost-/start INSERT path, started_at and vl_window_start are both derived
  best-effort as ended_at - duration_seconds.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Row

from homelab_monitor.kernel.db.repository import SqliteRepository

# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class CronRunRecord:
    """Hydrated row from ``cron_runs`` (run_id-keyed)."""

    run_id: str
    cron_fingerprint: str
    source: str
    state: str
    started_at: str
    ended_at: str | None
    duration_seconds: float | None
    exit_code: int | None
    vl_window_start: str | None
    vl_window_end: str | None
    overlapping: bool
    enriched_at: str | None
    line_count: int | None
    byte_count: int | None
    content_digest: str | None
    anomaly_flags: str


@dataclass(slots=True, frozen=True)
class CronRunListPage:
    """Cursor-pagination envelope for list_runs."""

    items: list[CronRunRecord]
    next_cursor: str | None


# ---------------------------------------------------------------------------
# Row hydrator
# ---------------------------------------------------------------------------


def _row_to_cron_run(row: Row[Any]) -> CronRunRecord:
    return CronRunRecord(
        run_id=str(row.run_id),
        cron_fingerprint=str(row.cron_fingerprint),
        source=str(row.source),
        state=str(row.state),
        started_at=str(row.started_at),
        ended_at=None if row.ended_at is None else str(row.ended_at),
        duration_seconds=(None if row.duration_seconds is None else float(row.duration_seconds)),
        exit_code=None if row.exit_code is None else int(row.exit_code),
        vl_window_start=(None if row.vl_window_start is None else str(row.vl_window_start)),
        vl_window_end=None if row.vl_window_end is None else str(row.vl_window_end),
        overlapping=bool(row.overlapping),
        enriched_at=None if row.enriched_at is None else str(row.enriched_at),
        line_count=None if row.line_count is None else int(row.line_count),
        byte_count=None if row.byte_count is None else int(row.byte_count),
        content_digest=(None if row.content_digest is None else str(row.content_digest)),
        anomaly_flags=str(row.anomaly_flags),
    )


# ---------------------------------------------------------------------------
# SQL constants
# ---------------------------------------------------------------------------

_RUN_COLS = (
    "run_id, cron_fingerprint, source, state, started_at, ended_at, "
    "duration_seconds, exit_code, vl_window_start, vl_window_end, overlapping, "
    "enriched_at, line_count, byte_count, content_digest, anomaly_flags"
)

_SELECT_RUN_SQL = text(f"SELECT {_RUN_COLS} FROM cron_runs WHERE run_id = :run_id")

# INSERT OR IGNORE — idempotent on the run_id PRIMARY KEY.
_INSERT_RUN_SQL = text(
    "INSERT OR IGNORE INTO cron_runs ("
    "  run_id, cron_fingerprint, source, state, started_at, ended_at, "
    "  duration_seconds, exit_code, vl_window_start, vl_window_end, "
    "  overlapping, enriched_at, line_count, byte_count, content_digest, "
    "  anomaly_flags"
    ") VALUES ("
    "  :run_id, :cron_fingerprint, :source, :state, :started_at, NULL, "
    "  NULL, NULL, :vl_window_start, NULL, 0, NULL, NULL, NULL, NULL, ''"
    ")"
)

# UPSERT — close an existing /start row OR insert a closed row on lost /start.
# On the INSERT branch, started_at + vl_window_start are the caller-derived
# (ended_at - duration_seconds) value. On the conflict branch, started_at and
# vl_window_start are LEFT ALONE (the /start row already set them correctly).
_CLOSE_RUN_SQL = text(
    "INSERT INTO cron_runs ("
    "  run_id, cron_fingerprint, source, state, started_at, ended_at, "
    "  duration_seconds, exit_code, vl_window_start, vl_window_end, "
    "  overlapping, enriched_at, line_count, byte_count, content_digest, "
    "  anomaly_flags"
    ") VALUES ("
    "  :run_id, :cron_fingerprint, :source, :state, :derived_started_at, "
    "  :ended_at, :duration_seconds, :exit_code, :derived_started_at, "
    "  :vl_window_end, 0, NULL, NULL, NULL, NULL, ''"
    ") "
    "ON CONFLICT(run_id) DO UPDATE SET "
    "  state = excluded.state, "
    "  ended_at = excluded.ended_at, "
    "  duration_seconds = excluded.duration_seconds, "
    "  exit_code = excluded.exit_code, "
    "  vl_window_end = excluded.vl_window_end"
)

# B-mode open-run scan — runs still 'running' for a fingerprint, oldest-first.
_SELECT_OPEN_BMODE_RUNS_SQL = text(
    f"SELECT {_RUN_COLS} FROM cron_runs "
    "WHERE source = 'logscrape' AND state = 'running' "
    "ORDER BY cron_fingerprint ASC, started_at ASC"
)

# Most-recent open run of one fingerprint whose start <= :at_ts. Used by the
# B-mode exit= correlation AND by window-finalize's next-CMD close.
_SELECT_OPEN_RUN_BY_FINGERPRINT_SQL = text(
    f"SELECT {_RUN_COLS} FROM cron_runs "
    "WHERE cron_fingerprint = :fp AND state = 'running' "
    "AND started_at <= :at_ts "
    "ORDER BY started_at DESC, run_id DESC LIMIT 1"
)

# Enrich work-queue — closed, un-enriched runs whose ended_at is past the grace
# cutoff. Uses ix_cron_runs_enrich_queue (partial on enriched_at IS NULL AND
# state != 'running').
_SELECT_RUNS_NEEDING_ENRICH_SQL = text(
    f"SELECT {_RUN_COLS} FROM cron_runs "
    "WHERE enriched_at IS NULL AND state != 'running' "
    "AND ended_at IS NOT NULL AND ended_at <= :grace_cutoff "
    "ORDER BY ended_at ASC"
)

# Window-finalize: close a B-mode run by next-CMD or timeout.
_FINALIZE_BMODE_RUN_SQL = text(
    "UPDATE cron_runs SET "
    "  state = :state, ended_at = :ended_at, "
    "  duration_seconds = :duration_seconds, vl_window_end = :vl_window_end "
    "WHERE run_id = :run_id AND state = 'running' AND source = 'logscrape'"
)

_SET_OVERLAPPING_SQL = text("UPDATE cron_runs SET overlapping = 1 WHERE run_id = :run_id")

_SET_ENRICHMENT_SQL = text(
    "UPDATE cron_runs SET "
    "  line_count = :line_count, byte_count = :byte_count, "
    "  content_digest = :content_digest, enriched_at = :enriched_at "
    "WHERE run_id = :run_id"
)

# Prune: rows older than the retention cutoff (by started_at).
_PRUNE_BY_AGE_SQL = text("DELETE FROM cron_runs WHERE started_at < :retention_cutoff")

# Prune: per-cron row cap. Delete the oldest rows of one fingerprint beyond the
# newest `max_rows` rows. Subquery selects the run_ids to KEEP.
_PRUNE_BY_COUNT_SQL = text(
    "DELETE FROM cron_runs WHERE cron_fingerprint = :fp AND run_id NOT IN ("
    "  SELECT run_id FROM cron_runs WHERE cron_fingerprint = :fp "
    "  ORDER BY started_at DESC, run_id DESC LIMIT :max_rows"
    ")"
)

# TODO: at scale (millions of cron_runs rows), verify SQLite uses the
# ix_cron_runs_fingerprint_started index for this DISTINCT scan via
# EXPLAIN QUERY PLAN. If the index isn't used, rewrite as
# ``SELECT cron_fingerprint FROM cron_runs GROUP BY cron_fingerprint``
# (which more reliably triggers index-based dedup) or add a covering
# index. Current scale (~7k rows, ~13 fingerprints) is fine.
# Distinct fingerprints (for the per-cron count-prune pass).
_DISTINCT_FINGERPRINTS_SQL = text("SELECT DISTINCT cron_fingerprint FROM cron_runs")


def _derive_started_at(ended_at: str, duration_seconds: float | None) -> str:
    """Best-effort started_at for the lost-/start UPSERT INSERT branch.

    started_at = ended_at - duration_seconds. When duration is unknown, fall
    back to ended_at itself (a zero-length window — better than NULL, which the
    NOT NULL started_at column forbids). `ended_at` is expected to be a
    `utc_now_iso()`-shaped UTC ISO-8601 string (with a `+00:00` offset); the
    derived `started_at` is re-serialized in the same shape.
    """
    end_dt = datetime.fromisoformat(ended_at)
    if duration_seconds is None:
        return ended_at
    return (end_dt - timedelta(seconds=duration_seconds)).isoformat()


# ---------------------------------------------------------------------------
# CronRunRepository
# ---------------------------------------------------------------------------


class CronRunRepository:
    """Async CRUD for cron_runs. Mirrors CronRepo's Core-SQL style."""

    def __init__(self, db: SqliteRepository) -> None:
        self._db = db

    async def insert_run(
        self,
        *,
        run_id: str,
        cron_fingerprint: str,
        source: str,
        started_at: str,
        vl_window_start: str,
    ) -> None:
        """INSERT a new run row in state='running'.

        INSERT OR IGNORE on the run_id PK — a duplicate run_id (wrapper retry /
        replay of /start) is an idempotent no-op, not an error.
        """
        async with self._db.transaction() as conn:
            await conn.execute(
                _INSERT_RUN_SQL,
                {
                    "run_id": run_id,
                    "cron_fingerprint": cron_fingerprint,
                    "source": source,
                    "state": "running",
                    "started_at": started_at,
                    "vl_window_start": vl_window_start,
                },
            )

    async def close_run(  # noqa: PLR0913
        self,
        *,
        run_id: str,
        cron_fingerprint: str,
        source: str,
        state: str,
        ended_at: str,
        duration_seconds: float | None,
        exit_code: int | None,
        vl_window_end: str,
    ) -> None:
        """Close a run via UPSERT.

        If a /start row exists → UPDATE state/ended_at/duration_seconds/
        exit_code/vl_window_end (started_at + vl_window_start untouched).
        If no row exists (lost /start — a NORMAL case) → INSERT a closed row;
        started_at and vl_window_start are both derived as
        ended_at - duration_seconds. On the UPSERT conflict (UPDATE) branch,
        `cron_fingerprint` and `source` are NOT updated — the existing `/start`
        row owns those values; the params are used only on the lost-`/start`
        INSERT branch.
        """
        derived_started_at = _derive_started_at(ended_at, duration_seconds)
        async with self._db.transaction() as conn:
            await conn.execute(
                _CLOSE_RUN_SQL,
                {
                    "run_id": run_id,
                    "cron_fingerprint": cron_fingerprint,
                    "source": source,
                    "state": state,
                    "derived_started_at": derived_started_at,
                    "ended_at": ended_at,
                    "duration_seconds": duration_seconds,
                    "exit_code": exit_code,
                    "vl_window_end": vl_window_end,
                },
            )

    async def get_run(self, run_id: str) -> CronRunRecord | None:
        """Return the run record for run_id, or None."""
        row = await self._db.fetch_one(_SELECT_RUN_SQL, {"run_id": run_id})
        if row is None:
            return None
        return _row_to_cron_run(row)

    async def list_runs(
        self,
        *,
        cron_fingerprint: str,
        limit: int,
        cursor: str | None = None,
        state_filter: str | None = None,
    ) -> CronRunListPage:
        """Paginated run history for one cron, newest-first.

        Ordered by (started_at DESC, run_id DESC). The cursor is the
        ``started_at`` of the last item of the previous page; rows strictly
        older than the cursor are returned. ``next_cursor`` is the started_at of
        the last item of THIS page, or None when fewer than ``limit`` rows
        remain. ``state_filter`` (when given) restricts to that state.
        """
        where = ["cron_fingerprint = :fp"]
        params: dict[str, Any] = {"fp": cron_fingerprint, "limit": limit + 1}
        if state_filter is not None:
            where.append("state = :state")
            params["state"] = state_filter
        if cursor is not None:
            where.append("started_at < :cursor")
            params["cursor"] = cursor
        where_sql = " AND ".join(where)
        # Fetch limit+1 to detect whether a further page exists.
        sql = text(
            f"SELECT {_RUN_COLS} FROM cron_runs WHERE {where_sql} "
            "ORDER BY started_at DESC, run_id DESC LIMIT :limit"
        )
        rows = await self._db.fetch_all(sql, params)
        items = [_row_to_cron_run(r) for r in rows]
        next_cursor: str | None = None
        if len(items) > limit:
            items = items[:limit]
            next_cursor = items[-1].started_at
        return CronRunListPage(items=items, next_cursor=next_cursor)

    async def list_open_bmode_runs(self) -> list[CronRunRecord]:
        """Return all B-mode (source='logscrape') runs still in state='running'.

        Ordered (cron_fingerprint ASC, started_at ASC) so the reconciler can group
        consecutive runs of the same cron and apply the next-CMD rule.

        Invariant: rows in ``cron_runs`` carry ``started_at`` values that the
        ingest path (``CronEventItem._validate_timestamp``) and ``utc_now_iso``
        both emit in normalized ``+00:00`` ISO-8601 form. Under that invariant
        the SQL ``ORDER BY started_at ASC`` on this TEXT column is
        chronological. A hand-inserted row in a different format (``Z``,
        offset omitted, etc.) would mis-sort; we do not currently enforce the
        format at the schema level.
        """
        rows = await self._db.fetch_all(_SELECT_OPEN_BMODE_RUNS_SQL)
        return [_row_to_cron_run(r) for r in rows]

    async def find_open_run_by_fingerprint(
        self, cron_fingerprint: str, at_ts: str
    ) -> CronRunRecord | None:
        """Return the most-recent open run of `cron_fingerprint` started at or
        before `at_ts`, or None.

        Used by the B-mode exit= correlation: an exit line at time T closes the
        most-recent running run of that fingerprint whose window contains T (its
        started_at <= T). Also reused by window-finalize.
        """
        row = await self._db.fetch_one(
            _SELECT_OPEN_RUN_BY_FINGERPRINT_SQL,
            {"fp": cron_fingerprint, "at_ts": at_ts},
        )
        return None if row is None else _row_to_cron_run(row)

    async def list_runs_needing_enrich(self, grace_cutoff: str) -> list[CronRunRecord]:
        """Return closed, un-enriched runs whose ended_at <= grace_cutoff.

        `grace_cutoff` is (now - enrich_grace_seconds) as an ISO-8601 string — runs
        that ended within the grace window are excluded so VL has time to ingest
        trailing lines. Ordered ended_at ASC (oldest first).
        """
        rows = await self._db.fetch_all(
            _SELECT_RUNS_NEEDING_ENRICH_SQL, {"grace_cutoff": grace_cutoff}
        )
        return [_row_to_cron_run(r) for r in rows]

    async def finalize_bmode_run(
        self,
        *,
        run_id: str,
        state: str,
        ended_at: str,
        duration_seconds: float | None,
    ) -> None:
        """Close a still-running B-mode run (window-finalize phase).

        Sets state, ended_at, duration_seconds, and vl_window_end (= ended_at, so
        the run-log endpoint has a populated upper bound for B-mode runs — D-BMODE-WINDOW).
        The `state = 'running'` guard in the UPDATE makes this idempotent: a
        re-run tick that already closed the row is a no-op.
        """
        async with self._db.transaction() as conn:
            await conn.execute(
                _FINALIZE_BMODE_RUN_SQL,
                {
                    "run_id": run_id,
                    "state": state,
                    "ended_at": ended_at,
                    "duration_seconds": duration_seconds,
                    "vl_window_end": ended_at,
                },
            )

    async def set_overlapping(self, run_id: str) -> None:
        """Set overlapping=1 on a run (window-finalize phase)."""
        async with self._db.transaction() as conn:
            await conn.execute(_SET_OVERLAPPING_SQL, {"run_id": run_id})

    async def set_enrichment(
        self,
        *,
        run_id: str,
        line_count: int,
        byte_count: int,
        content_digest: str,
        enriched_at: str,
    ) -> None:
        """Write the VL-derived enrichment fields + enriched_at (enrich phase).

        anomaly_flags is intentionally NOT touched — STAGE-002-014 owns it; this
        stage leaves it at its '' default.
        """
        async with self._db.transaction() as conn:
            await conn.execute(
                _SET_ENRICHMENT_SQL,
                {
                    "run_id": run_id,
                    "line_count": line_count,
                    "byte_count": byte_count,
                    "content_digest": content_digest,
                    "enriched_at": enriched_at,
                },
            )

    async def prune_runs(self, *, retention_cutoff: str, max_rows_per_cron: int) -> int:
        """Prune cron_runs by age AND per-cron row cap. Returns rows deleted.

        Two passes in ONE transaction:
          1. Age: delete every row with started_at < retention_cutoff.
          2. Count: for each remaining distinct fingerprint, delete rows beyond the
             newest `max_rows_per_cron` (by started_at DESC, run_id DESC).
        Whichever bound prunes a given row first wins; the count pass runs after
        the age pass so it operates on the already-age-pruned set.

        Concurrency note: the age-prune DELETE and every per-fingerprint
        count-prune run inside ONE transaction. SQLite serializes writers,
        so this holds the writer lock for the full prune duration —
        competing with the cron-events B-mode ingest path. At current
        scale (~ms) this is fine. If prune duration grows to a meaningful
        fraction of a 30s tick, split this into one transaction per
        fingerprint to release the writer lock between rows.
        """
        deleted = 0
        async with self._db.transaction() as conn:
            age_result = await conn.execute(
                _PRUNE_BY_AGE_SQL, {"retention_cutoff": retention_cutoff}
            )
            deleted += age_result.rowcount
            fp_rows = (await conn.execute(_DISTINCT_FINGERPRINTS_SQL)).fetchall()
            for fp_row in fp_rows:
                fp = str(fp_row.cron_fingerprint)
                count_result = await conn.execute(
                    _PRUNE_BY_COUNT_SQL, {"fp": fp, "max_rows": max_rows_per_cron}
                )
                deleted += count_result.rowcount
        return deleted


__all__ = [
    "CronRunListPage",
    "CronRunRecord",
    "CronRunRepository",
]
