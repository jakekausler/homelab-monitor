"""Repository for the log_saved_queries table (STAGE-004-013).

One row per named saved Explorer query. Timestamps are ISO-8601 UTC strings
(repo convention via utc_now_iso). selected_services is stored as a JSON array
of {"service": str, "source_type": str} objects.

Invariant (enforced by callers/schema, not the DB): either since_preset is set,
OR (range_start_iso AND range_end_iso) are both set — never neither. The DB
columns are all nullable; the API request schema validates the invariant.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.db.time import utc_now_iso


class DuplicateNameError(Exception):
    """Raised when create/rename would violate the UNIQUE(name) constraint."""

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"saved query name already exists: {name!r}")


@dataclass(frozen=True, slots=True)
class SavedQueryRow:
    id: int
    name: str
    logs_ql: str
    selected_services: list[dict[str, str]]
    since_preset: str | None
    range_start_iso: str | None
    range_end_iso: str | None
    advanced_mode: bool
    created_at: str
    updated_at: str


class SavedQueriesRepository:
    def __init__(self, repo: SqliteRepository) -> None:
        self._repo: SqliteRepository = repo

    async def create(  # noqa: PLR0913 -- keyword-only fields mirror the row
        self,
        *,
        name: str,
        logs_ql: str,
        selected_services: list[dict[str, str]],
        since_preset: str | None,
        range_start_iso: str | None,
        range_end_iso: str | None,
        advanced_mode: bool,
    ) -> SavedQueryRow:
        now = utc_now_iso()
        services_json = json.dumps(selected_services)
        try:
            async with self._repo.transaction() as conn:
                result = await conn.execute(
                    text(
                        "INSERT INTO log_saved_queries "
                        "  (name, logs_ql, selected_services, since_preset, "
                        "   range_start_iso, range_end_iso, advanced_mode, "
                        "   created_at, updated_at) "
                        "VALUES "
                        "  (:name, :lq, :svc, :preset, :rs, :re, :adv, :now, :now)"
                    ),
                    {
                        "name": name,
                        "lq": logs_ql,
                        "svc": services_json,
                        "preset": since_preset,
                        "rs": range_start_iso,
                        "re": range_end_iso,
                        "adv": 1 if advanced_mode else 0,
                        "now": now,
                    },
                )
                new_id = int(result.lastrowid)
        except IntegrityError as exc:
            raise DuplicateNameError(name) from exc
        # Re-read to return the full row (cheap; keeps one mapping path).
        created = await self.get(new_id)
        if created is None:  # pragma: no cover -- just inserted
            msg = f"saved query vanished after insert: id={new_id}"
            raise RuntimeError(msg)
        return created

    async def list_sorted(self) -> list[SavedQueryRow]:
        rows = await self._repo.fetch_all(
            text(
                "SELECT id, name, logs_ql, selected_services, since_preset, "
                "  range_start_iso, range_end_iso, advanced_mode, "
                "  created_at, updated_at "
                "FROM log_saved_queries "
                "ORDER BY name COLLATE NOCASE ASC"
            )
        )
        return [_row_to_dataclass(r) for r in rows]

    async def get(self, query_id: int) -> SavedQueryRow | None:
        rows = await self._repo.fetch_all(
            text(
                "SELECT id, name, logs_ql, selected_services, since_preset, "
                "  range_start_iso, range_end_iso, advanced_mode, "
                "  created_at, updated_at "
                "FROM log_saved_queries WHERE id = :id"
            ),
            {"id": query_id},
        )
        if not rows:
            return None
        return _row_to_dataclass(rows[0])

    async def delete(self, query_id: int) -> bool:
        """Delete by id. Returns True if a row was deleted, False if absent (404)."""
        async with self._repo.transaction() as conn:
            result = await conn.execute(
                text("DELETE FROM log_saved_queries WHERE id = :id"),
                {"id": query_id},
            )
            return (result.rowcount or 0) > 0

    async def rename(self, *, query_id: int, new_name: str) -> SavedQueryRow | None:
        """Rename by id. Returns the updated row, or None if the id is absent (404).

        Raises DuplicateNameError if new_name collides with another row.
        """
        now = utc_now_iso()
        try:
            async with self._repo.transaction() as conn:
                result = await conn.execute(
                    text(
                        "UPDATE log_saved_queries "
                        "SET name = :name, updated_at = :now WHERE id = :id"
                    ),
                    {"name": new_name, "now": now, "id": query_id},
                )
                if (result.rowcount or 0) == 0:
                    return None
        except IntegrityError as exc:
            raise DuplicateNameError(new_name) from exc
        return await self.get(query_id)

    async def update(  # noqa: PLR0913 -- keyword-only fields mirror the row
        self,
        *,
        query_id: int,
        logs_ql: str,
        selected_services: list[dict[str, str]],
        since_preset: str | None,
        range_start_iso: str | None,
        range_end_iso: str | None,
        advanced_mode: bool,
    ) -> SavedQueryRow | None:
        """Overwrite a saved query's PAYLOAD (not its name) by id.

        Returns the updated row, or None if the id is absent (404). The `name`
        column is intentionally NOT changed, so no UNIQUE(name) collision is
        possible and no DuplicateNameError path is needed.
        """
        now = utc_now_iso()
        services_json = json.dumps(selected_services)
        async with self._repo.transaction() as conn:
            result = await conn.execute(
                text(
                    "UPDATE log_saved_queries SET "
                    "  logs_ql = :lq, selected_services = :svc, "
                    "  since_preset = :preset, range_start_iso = :rs, "
                    "  range_end_iso = :re, advanced_mode = :adv, "
                    "  updated_at = :now "
                    "WHERE id = :id"
                ),
                {
                    "lq": logs_ql,
                    "svc": services_json,
                    "preset": since_preset,
                    "rs": range_start_iso,
                    "re": range_end_iso,
                    "adv": 1 if advanced_mode else 0,
                    "now": now,
                    "id": query_id,
                },
            )
            if (result.rowcount or 0) == 0:
                return None
        return await self.get(query_id)


def _row_to_dataclass(r: Any) -> SavedQueryRow:  # noqa: ANN401 -- SQLite Row
    services_raw: str = str(r.selected_services)  # pyright: ignore[reportAttributeAccessIssue]
    parsed: list[dict[str, str]] = json.loads(services_raw) if services_raw else []
    return SavedQueryRow(
        id=int(r.id),  # pyright: ignore[reportAttributeAccessIssue]
        name=str(r.name),  # pyright: ignore[reportAttributeAccessIssue]
        logs_ql=str(r.logs_ql),  # pyright: ignore[reportAttributeAccessIssue]
        selected_services=parsed,
        since_preset=(None if r.since_preset is None else str(r.since_preset)),  # pyright: ignore[reportAttributeAccessIssue]
        range_start_iso=(None if r.range_start_iso is None else str(r.range_start_iso)),  # pyright: ignore[reportAttributeAccessIssue]
        range_end_iso=(None if r.range_end_iso is None else str(r.range_end_iso)),  # pyright: ignore[reportAttributeAccessIssue]
        advanced_mode=bool(r.advanced_mode),  # pyright: ignore[reportAttributeAccessIssue]
        created_at=str(r.created_at),  # pyright: ignore[reportAttributeAccessIssue]
        updated_at=str(r.updated_at),  # pyright: ignore[reportAttributeAccessIssue]
    )


__all__ = ["DuplicateNameError", "SavedQueriesRepository", "SavedQueryRow"]
