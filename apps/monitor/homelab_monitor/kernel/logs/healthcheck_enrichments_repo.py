"""Repository for the container_healthcheck_enrichments table (STAGE-004-033).

One row per detected healthcheck-unhealthy episode, keyed by a UUID incident_id
but deduped on the UNIQUE (logical_key, healthcheck_changed_at) pair so the
reconciler's INSERT OR IGNORE is idempotent across ticks. lines_json is the
persisted VictoriaLogs window (a JSON array of LogLine.model_dump() dicts).
created_at is an ISO-8601 UTC TEXT via utc_now_iso(). Prune mirrors
CrashEnrichmentsRepository.prune (age delete + per-logical_key cap in one transaction).
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

_HEALTHCHECK_COLS = (
    "incident_id, logical_key, container_name, container_id, previous_healthcheck, "
    "new_state, healthcheck_changed_at, image_name, compose_project, compose_service, "
    "lines_json, line_count, truncated, degraded, window_start, window_end, created_at"
)


@dataclass(frozen=True, slots=True)
class HealthcheckEnrichmentRow:
    """Hydrated row from container_healthcheck_enrichments.

    lines_json is kept as the raw JSON TEXT; call parse_lines() to materialize
    the list[LogLine] lazily (the summary list endpoint never needs them).
    """

    incident_id: str
    logical_key: str
    container_name: str
    container_id: str | None
    previous_healthcheck: str | None
    new_state: str
    healthcheck_changed_at: str
    image_name: str | None
    compose_project: str | None
    compose_service: str | None
    lines_json: str
    line_count: int
    truncated: bool
    degraded: bool
    window_start: str
    window_end: str
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
                try:
                    out.append(LogLine(**fields))
                except ValidationError:
                    continue
        return out


def _row_to_healthcheck(r: Any) -> HealthcheckEnrichmentRow:  # noqa: ANN401
    return HealthcheckEnrichmentRow(
        incident_id=str(r.incident_id),  # pyright: ignore[reportAttributeAccessIssue]
        logical_key=str(r.logical_key),  # pyright: ignore[reportAttributeAccessIssue]
        container_name=str(r.container_name),  # pyright: ignore[reportAttributeAccessIssue]
        container_id=(
            None if r.container_id is None else str(r.container_id)  # pyright: ignore[reportAttributeAccessIssue]
        ),
        previous_healthcheck=(
            None if r.previous_healthcheck is None else str(r.previous_healthcheck)  # pyright: ignore[reportAttributeAccessIssue]
        ),
        new_state=str(r.new_state),  # pyright: ignore[reportAttributeAccessIssue]
        healthcheck_changed_at=str(r.healthcheck_changed_at),  # pyright: ignore[reportAttributeAccessIssue]
        image_name=None if r.image_name is None else str(r.image_name),  # pyright: ignore[reportAttributeAccessIssue]
        compose_project=(
            None if r.compose_project is None else str(r.compose_project)  # pyright: ignore[reportAttributeAccessIssue]
        ),
        compose_service=(
            None if r.compose_service is None else str(r.compose_service)  # pyright: ignore[reportAttributeAccessIssue]
        ),
        lines_json=str(r.lines_json),  # pyright: ignore[reportAttributeAccessIssue]
        line_count=int(r.line_count),  # pyright: ignore[reportAttributeAccessIssue]
        truncated=bool(r.truncated),  # pyright: ignore[reportAttributeAccessIssue]
        degraded=bool(r.degraded),  # pyright: ignore[reportAttributeAccessIssue]
        window_start=str(r.window_start),  # pyright: ignore[reportAttributeAccessIssue]
        window_end=str(r.window_end),  # pyright: ignore[reportAttributeAccessIssue]
        created_at=str(r.created_at),  # pyright: ignore[reportAttributeAccessIssue]
    )


class HealthcheckEnrichmentsRepository:
    """Async CRUD for container_healthcheck_enrichments."""

    def __init__(self, repo: SqliteRepository) -> None:
        self._repo: SqliteRepository = repo

    async def insert(  # noqa: PLR0913
        self,
        *,
        incident_id: str,
        logical_key: str,
        container_name: str,
        container_id: str | None,
        previous_healthcheck: str | None,
        new_state: str,
        healthcheck_changed_at: str,
        image_name: str | None,
        compose_project: str | None,
        compose_service: str | None,
        lines: list[LogLine],
        truncated: bool,
        degraded: bool,
        window_start: str,
        window_end: str,
    ) -> bool:
        """INSERT OR IGNORE one healthcheck enrichment. Returns True if a NEW row was
        inserted, False if the (logical_key, healthcheck_changed_at) pair already existed.

        The UNIQUE index ux_hc_enrich_logical_changed makes a re-detect of the same
        episode on a later tick an idempotent no-op.
        """
        lines_json = json.dumps([ln.model_dump() for ln in lines])
        now = utc_now_iso()
        async with self._repo.transaction() as conn:
            result = await conn.execute(
                text(
                    "INSERT OR IGNORE INTO container_healthcheck_enrichments ("
                    "  incident_id, logical_key, container_name, container_id, "
                    "  previous_healthcheck, new_state, healthcheck_changed_at, image_name, "
                    "  compose_project, compose_service, lines_json, line_count, "
                    "  truncated, degraded, window_start, window_end, created_at"
                    ") VALUES ("
                    "  :incident_id, :logical_key, :container_name, :container_id, "
                    "  :previous_healthcheck, :new_state, :healthcheck_changed_at, :image_name, "
                    "  :compose_project, :compose_service, :lines_json, :line_count, "
                    "  :truncated, :degraded, :window_start, :window_end, :created_at"
                    ")"
                ),
                {
                    "incident_id": incident_id,
                    "logical_key": logical_key,
                    "container_name": container_name,
                    "container_id": container_id,
                    "previous_healthcheck": previous_healthcheck,
                    "new_state": new_state,
                    "healthcheck_changed_at": healthcheck_changed_at,
                    "image_name": image_name,
                    "compose_project": compose_project,
                    "compose_service": compose_service,
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

    async def list_for_container(self, logical_key: str) -> list[HealthcheckEnrichmentRow]:
        """All incident rows for one logical container, newest episode first."""
        rows = await self._repo.fetch_all(
            text(
                f"SELECT {_HEALTHCHECK_COLS} FROM container_healthcheck_enrichments "
                "WHERE logical_key = :lk "
                "ORDER BY healthcheck_changed_at DESC, created_at DESC"
            ),
            {"lk": logical_key},
        )
        return [_row_to_healthcheck(r) for r in rows]

    async def get(self, incident_id: str) -> HealthcheckEnrichmentRow | None:
        """Return one incident row by incident_id, or None."""
        rows = await self._repo.fetch_all(
            text(
                f"SELECT {_HEALTHCHECK_COLS} FROM container_healthcheck_enrichments "
                "WHERE incident_id = :id"
            ),
            {"id": incident_id},
        )
        if not rows:
            return None
        return _row_to_healthcheck(rows[0])

    async def prune(self, *, retention_cutoff_iso: str, max_rows_per_container: int) -> int:
        """Prune by age (healthcheck_changed_at < cutoff) AND per-logical_key row cap.

        Two passes in one transaction (mirrors CrashEnrichmentsRepository.prune):
        age delete first, then for each remaining distinct logical_key delete rows
        beyond the newest max_rows_per_container (by healthcheck_changed_at DESC,
        created_at DESC). Returns total rows deleted.
        """
        deleted = 0
        async with self._repo.transaction() as conn:
            age_result = await conn.execute(
                text(
                    "DELETE FROM container_healthcheck_enrichments "
                    "WHERE healthcheck_changed_at < :cutoff"
                ),
                {"cutoff": retention_cutoff_iso},
            )
            deleted += age_result.rowcount or 0
            lk_rows = (
                await conn.execute(
                    text("SELECT DISTINCT logical_key FROM container_healthcheck_enrichments")
                )
            ).fetchall()
            for lk_row in lk_rows:
                lk = str(lk_row.logical_key)
                count_result = await conn.execute(
                    text(
                        "DELETE FROM container_healthcheck_enrichments "
                        "WHERE logical_key = :lk AND incident_id NOT IN ("
                        "  SELECT incident_id FROM container_healthcheck_enrichments "
                        "  WHERE logical_key = :lk "
                        "  ORDER BY healthcheck_changed_at DESC, created_at DESC LIMIT :max_rows"
                        ")"
                    ),
                    {"lk": lk, "max_rows": max_rows_per_container},
                )
                deleted += count_result.rowcount or 0
        return deleted


__all__ = ["HealthcheckEnrichmentRow", "HealthcheckEnrichmentsRepository"]
