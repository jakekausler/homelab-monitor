"""Repository for the log_signature_annotations table (STAGE-004-029).

Timestamped plain-text notes per signature. `author` is the denormalized
session username at creation (Decision A2). `created_at` is an ISO-8601 UTC
TEXT (repo convention via utc_now_iso). Rows cascade-delete when the parent
log_signatures row is deleted (composite FK + ON DELETE CASCADE).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import text

from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.db.time import utc_now_iso


@dataclass(frozen=True, slots=True)
class Annotation:
    id: int
    template_hash: str
    service_key: str
    note: str
    author: str
    created_at: str


class AnnotationsRepository:
    def __init__(self, repo: SqliteRepository) -> None:
        self._repo: SqliteRepository = repo

    async def create(
        self,
        *,
        template_hash: str,
        service_key: str,
        note: str,
        author: str,
    ) -> Annotation:
        now = utc_now_iso()
        async with self._repo.transaction() as conn:
            result = await conn.execute(
                text(
                    "INSERT INTO log_signature_annotations "
                    "  (template_hash, service_key, note, author, created_at) "
                    "VALUES (:h, :s, :note, :author, :now)"
                ),
                {
                    "h": template_hash,
                    "s": service_key,
                    "note": note,
                    "author": author,
                    "now": now,
                },
            )
            new_id = int(result.lastrowid)
        created = await self.get(new_id)
        if created is None:  # pragma: no cover
            msg = f"annotation vanished after insert: id={new_id}"
            raise RuntimeError(msg)
        return created

    async def get(self, annotation_id: int) -> Annotation | None:
        rows = await self._repo.fetch_all(
            text(
                "SELECT id, template_hash, service_key, note, author, created_at "
                "FROM log_signature_annotations WHERE id = :id"
            ),
            {"id": annotation_id},
        )
        if not rows:
            return None
        return _row_to_annotation(rows[0])

    async def list_for_signature(self, template_hash: str, service_key: str) -> list[Annotation]:
        rows = await self._repo.fetch_all(
            text(
                "SELECT id, template_hash, service_key, note, author, created_at "
                "FROM log_signature_annotations "
                "WHERE template_hash = :h AND service_key = :s "
                "ORDER BY created_at DESC, id DESC"
            ),
            {"h": template_hash, "s": service_key},
        )
        return [_row_to_annotation(r) for r in rows]

    async def delete(self, annotation_id: int, template_hash: str, service_key: str) -> bool:
        """Delete by id scoped to the composite key. Returns True if a row was
        deleted, False if absent OR belongs to a different signature (404)."""
        async with self._repo.transaction() as conn:
            result = await conn.execute(
                text(
                    "DELETE FROM log_signature_annotations "
                    "WHERE id = :id AND template_hash = :h AND service_key = :s"
                ),
                {"id": annotation_id, "h": template_hash, "s": service_key},
            )
            return (result.rowcount or 0) > 0


def _row_to_annotation(r: Any) -> Annotation:  # noqa: ANN401
    return Annotation(
        id=int(r.id),  # pyright: ignore[reportAttributeAccessIssue]
        template_hash=str(r.template_hash),  # pyright: ignore[reportAttributeAccessIssue]
        service_key=str(r.service_key),  # pyright: ignore[reportAttributeAccessIssue]
        note=str(r.note),  # pyright: ignore[reportAttributeAccessIssue]
        author=str(r.author),  # pyright: ignore[reportAttributeAccessIssue]
        created_at=str(r.created_at),  # pyright: ignore[reportAttributeAccessIssue]
    )


__all__ = ["Annotation", "AnnotationsRepository"]
