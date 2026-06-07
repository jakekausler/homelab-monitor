"""Repository for the cron_run_failure_enrichments table (STAGE-004-034).

One row per failed cron run, keyed by a UUID failure_id but deduped on the
UNIQUE (cron_fingerprint, run_id) pair so the reconciler's INSERT OR IGNORE is
idempotent across ticks. lines_json is the persisted last-N VictoriaLogs window
(a JSON array of LogLine.model_dump() dicts). created_at is an ISO-8601 UTC TEXT
via utc_now_iso(). Prune mirrors CrashEnrichmentsRepository.prune (age delete on
created_at + per-cron_fingerprint cap in one transaction).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, cast

from pydantic import ValidationError
from sqlalchemy import text

from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.db.time import utc_now_iso
from homelab_monitor.kernel.logs.models import LogLine

_COLS = (
    "failure_id, cron_fingerprint, run_id, exit_code, started_at, ended_at, "
    "lines_json, line_count, truncated, degraded, window_start, window_end, created_at"
)


@dataclass(frozen=True, slots=True)
class CronRunFailureEnrichmentRow:
    """Hydrated row from cron_run_failure_enrichments.

    lines_json is kept as raw JSON TEXT; call parse_lines() to materialize the
    list[LogLine] lazily.
    """

    failure_id: str
    cron_fingerprint: str
    run_id: str
    exit_code: int | None
    started_at: str | None
    ended_at: str | None
    lines_json: str
    line_count: int
    truncated: bool
    degraded: bool
    window_start: str | None
    window_end: str | None
    created_at: str

    def parse_lines(self) -> list[LogLine]:
        """Materialize lines_json into a list[LogLine]."""
        raw: object = json.loads(self.lines_json)
        if not isinstance(raw, list):
            return []
        out: list[LogLine] = []
        for item in raw:  # pyright: ignore[reportUnknownVariableType]
            if isinstance(item, dict):
                fields: dict[str, Any] = cast("dict[str, Any]", item)
                # lines_json is self-produced from LogLine.model_dump(), so a
                # malformed entry implies DB corruption. Fail-soft (skip the bad
                # line) rather than 500 the detail endpoint on the whole window.
                try:
                    out.append(LogLine(**fields))
                except ValidationError:
                    continue
        return out


def _row_to_failure(r: Any) -> CronRunFailureEnrichmentRow:  # noqa: ANN401
    return CronRunFailureEnrichmentRow(
        failure_id=str(r.failure_id),  # pyright: ignore[reportAttributeAccessIssue]
        cron_fingerprint=str(r.cron_fingerprint),  # pyright: ignore[reportAttributeAccessIssue]
        run_id=str(r.run_id),  # pyright: ignore[reportAttributeAccessIssue]
        exit_code=(None if r.exit_code is None else int(r.exit_code)),  # pyright: ignore[reportAttributeAccessIssue]
        started_at=(None if r.started_at is None else str(r.started_at)),  # pyright: ignore[reportAttributeAccessIssue]
        ended_at=(None if r.ended_at is None else str(r.ended_at)),  # pyright: ignore[reportAttributeAccessIssue]
        lines_json=str(r.lines_json),  # pyright: ignore[reportAttributeAccessIssue]
        line_count=int(r.line_count),  # pyright: ignore[reportAttributeAccessIssue]
        truncated=bool(r.truncated),  # pyright: ignore[reportAttributeAccessIssue]
        degraded=bool(r.degraded),  # pyright: ignore[reportAttributeAccessIssue]
        window_start=(None if r.window_start is None else str(r.window_start)),  # pyright: ignore[reportAttributeAccessIssue]
        window_end=(None if r.window_end is None else str(r.window_end)),  # pyright: ignore[reportAttributeAccessIssue]
        created_at=str(r.created_at),  # pyright: ignore[reportAttributeAccessIssue]
    )


class CronRunFailureEnrichmentsRepository:
    """Async CRUD for cron_run_failure_enrichments."""

    def __init__(self, repo: SqliteRepository) -> None:
        self._repo: SqliteRepository = repo

    async def insert(  # noqa: PLR0913
        self,
        *,
        failure_id: str,
        cron_fingerprint: str,
        run_id: str,
        exit_code: int | None,
        started_at: str | None,
        ended_at: str | None,
        lines: list[LogLine],
        truncated: bool,
        degraded: bool,
        window_start: str | None,
        window_end: str | None,
    ) -> bool:
        """INSERT OR IGNORE one failure enrichment. Returns True if a NEW row was
        inserted, False if the (cron_fingerprint, run_id) pair already existed.

        The UNIQUE index ux_cron_failure_enrich_fp_run makes a re-enrich of the
        same failed run on a later tick an idempotent no-op.
        """
        lines_json = json.dumps([ln.model_dump() for ln in lines])
        now = utc_now_iso()
        async with self._repo.transaction() as conn:
            result = await conn.execute(
                text(
                    "INSERT OR IGNORE INTO cron_run_failure_enrichments ("
                    "  failure_id, cron_fingerprint, run_id, exit_code, started_at, "
                    "  ended_at, lines_json, line_count, truncated, degraded, "
                    "  window_start, window_end, created_at"
                    ") VALUES ("
                    "  :failure_id, :cron_fingerprint, :run_id, :exit_code, :started_at, "
                    "  :ended_at, :lines_json, :line_count, :truncated, :degraded, "
                    "  :window_start, :window_end, :created_at"
                    ")"
                ),
                {
                    "failure_id": failure_id,
                    "cron_fingerprint": cron_fingerprint,
                    "run_id": run_id,
                    "exit_code": exit_code,
                    "started_at": started_at,
                    "ended_at": ended_at,
                    "lines_json": lines_json,
                    "line_count": len(lines),
                    "truncated": 1 if truncated else 0,
                    "degraded": 1 if degraded else 0,
                    "window_start": window_start,
                    "window_end": window_end,
                    "created_at": now,
                },
            )
            return (result.rowcount or 0) > 0

    async def get_by_run(
        self, cron_fingerprint: str, run_id: str
    ) -> CronRunFailureEnrichmentRow | None:
        """Return the failure enrichment for (cron_fingerprint, run_id), or None."""
        rows = await self._repo.fetch_all(
            text(
                f"SELECT {_COLS} FROM cron_run_failure_enrichments "
                "WHERE cron_fingerprint = :fp AND run_id = :run_id"
            ),
            {"fp": cron_fingerprint, "run_id": run_id},
        )
        if not rows:
            return None
        return _row_to_failure(rows[0])

    async def prune(self, *, retention_cutoff_iso: str, max_rows_per_cron: int) -> int:
        """Prune by age (created_at < cutoff) AND per-cron_fingerprint row cap.

        Two passes in one transaction (mirrors CrashEnrichmentsRepository.prune):
        age delete first, then for each remaining distinct cron_fingerprint delete
        rows beyond the newest max_rows_per_cron (by created_at DESC). Returns total
        rows deleted.
        """
        deleted = 0
        async with self._repo.transaction() as conn:
            age_result = await conn.execute(
                text("DELETE FROM cron_run_failure_enrichments WHERE created_at < :cutoff"),
                {"cutoff": retention_cutoff_iso},
            )
            deleted += age_result.rowcount or 0
            fp_rows = (
                await conn.execute(
                    text("SELECT DISTINCT cron_fingerprint FROM cron_run_failure_enrichments")
                )
            ).fetchall()
            for fp_row in fp_rows:
                fp = str(fp_row.cron_fingerprint)
                count_result = await conn.execute(
                    text(
                        "DELETE FROM cron_run_failure_enrichments "
                        "WHERE cron_fingerprint = :fp AND failure_id NOT IN ("
                        "  SELECT failure_id FROM cron_run_failure_enrichments "
                        "  WHERE cron_fingerprint = :fp "
                        "  ORDER BY created_at DESC LIMIT :max_rows"
                        ")"
                    ),
                    {"fp": fp, "max_rows": max_rows_per_cron},
                )
                deleted += count_result.rowcount or 0
        return deleted


__all__ = ["CronRunFailureEnrichmentRow", "CronRunFailureEnrichmentsRepository"]
