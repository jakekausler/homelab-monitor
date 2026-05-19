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


__all__ = [
    "CronRunListPage",
    "CronRunRecord",
    "CronRunRepository",
]
