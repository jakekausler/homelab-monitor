"""Repository for the log_signature_silence_allowlist table (STAGE-004-038).

Expected-silence allowlist entries. Per D-SILENCE-ALLOWLIST-NO-UPDATE-V1 entries
are IMMUTABLE: edit = delete+recreate (matches annotations_repo's CRUD-without-update
shape). created_at is ISO-8601 UTC TEXT (utc_now_iso). expires_at is nullable ISO
TEXT. template_hash NULL => the entry applies to ALL signatures of service_key.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import text

from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.db.time import utc_now_iso


@dataclass(frozen=True, slots=True)
class SilenceAllowlistEntry:
    id: int
    template_hash: str | None
    service_key: str
    schedule_kind: str
    schedule_value: str
    reason: str
    created_at: str
    expires_at: str | None


class SilenceAllowlistRepository:
    def __init__(self, repo: SqliteRepository) -> None:
        self._repo: SqliteRepository = repo

    async def create(  # noqa: PLR0913
        self,
        *,
        template_hash: str | None,
        service_key: str,
        schedule_kind: str,
        schedule_value: str,
        reason: str,
        expires_at: str | None,
    ) -> SilenceAllowlistEntry:
        now = utc_now_iso()
        async with self._repo.transaction() as conn:
            result = await conn.execute(
                text(
                    "INSERT INTO log_signature_silence_allowlist "
                    "  (template_hash, service_key, schedule_kind, schedule_value, "
                    "   reason, created_at, expires_at) "
                    "VALUES (:h, :s, :kind, :val, :reason, :now, :exp)"
                ),
                {
                    "h": template_hash,
                    "s": service_key,
                    "kind": schedule_kind,
                    "val": schedule_value,
                    "reason": reason,
                    "now": now,
                    "exp": expires_at,
                },
            )
            new_id = int(result.lastrowid)
        created = await self.get(new_id)
        if created is None:  # pragma: no cover
            msg = f"silence allowlist entry vanished after insert: id={new_id}"
            raise RuntimeError(msg)
        return created

    async def get(self, entry_id: int) -> SilenceAllowlistEntry | None:
        rows = await self._repo.fetch_all(
            text(
                "SELECT id, template_hash, service_key, schedule_kind, schedule_value, "
                "  reason, created_at, expires_at "
                "FROM log_signature_silence_allowlist WHERE id = :id"
            ),
            {"id": entry_id},
        )
        if not rows:
            return None
        return _row_to_entry(rows[0])

    async def list_all(self) -> list[SilenceAllowlistEntry]:
        rows = await self._repo.fetch_all(
            text(
                "SELECT id, template_hash, service_key, schedule_kind, schedule_value, "
                "  reason, created_at, expires_at "
                "FROM log_signature_silence_allowlist "
                "ORDER BY created_at DESC, id DESC"
            )
        )
        return [_row_to_entry(r) for r in rows]

    async def delete(self, entry_id: int) -> bool:
        async with self._repo.transaction() as conn:
            result = await conn.execute(
                text("DELETE FROM log_signature_silence_allowlist WHERE id = :id"),
                {"id": entry_id},
            )
            return (result.rowcount or 0) > 0


def _row_to_entry(r: Any) -> SilenceAllowlistEntry:  # noqa: ANN401 -- SQLite Row
    raw_hash = r.template_hash  # pyright: ignore[reportAttributeAccessIssue]
    raw_exp = r.expires_at  # pyright: ignore[reportAttributeAccessIssue]
    return SilenceAllowlistEntry(
        id=int(r.id),  # pyright: ignore[reportAttributeAccessIssue]
        template_hash=(None if raw_hash is None else str(raw_hash)),
        service_key=str(r.service_key),  # pyright: ignore[reportAttributeAccessIssue]
        schedule_kind=str(r.schedule_kind),  # pyright: ignore[reportAttributeAccessIssue]
        schedule_value=str(r.schedule_value),  # pyright: ignore[reportAttributeAccessIssue]
        reason=str(r.reason),  # pyright: ignore[reportAttributeAccessIssue]
        created_at=str(r.created_at),  # pyright: ignore[reportAttributeAccessIssue]
        expires_at=(None if raw_exp is None else str(raw_exp)),
    )


__all__ = ["SilenceAllowlistEntry", "SilenceAllowlistRepository"]
