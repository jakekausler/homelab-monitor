"""Repository for the app_settings key/value table (STAGE-004-022).

Generic single-row-per-key string store. Callers serialize/deserialize values.
updated_at is an ISO-8601 UTC string (repo convention via utc_now_iso)."""

from __future__ import annotations

from sqlalchemy import text

from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.db.time import utc_now_iso


class AppSettingsRepository:
    def __init__(self, repo: SqliteRepository) -> None:
        self._repo: SqliteRepository = repo

    async def get(self, key: str) -> str | None:
        """Return the stored value for key, or None if absent."""
        rows = await self._repo.fetch_all(
            text("SELECT value FROM app_settings WHERE key = :key"),
            {"key": key},
        )
        if not rows:
            return None
        return str(rows[0].value)  # pyright: ignore[reportAttributeAccessIssue]

    async def set(self, key: str, value: str) -> None:
        """Upsert key=value, stamping updated_at with the current UTC time."""
        now = utc_now_iso()
        async with self._repo.transaction() as conn:
            await conn.execute(
                text(
                    "INSERT INTO app_settings (key, value, updated_at) "
                    "VALUES (:key, :value, :now) "
                    "ON CONFLICT(key) DO UPDATE SET "
                    "  value = excluded.value, updated_at = excluded.updated_at"
                ),
                {"key": key, "value": value, "now": now},
            )

    async def delete(self, key: str) -> bool:
        """Delete key. Returns True if a row was removed, False if absent."""
        async with self._repo.transaction() as conn:
            result = await conn.execute(
                text("DELETE FROM app_settings WHERE key = :key"),
                {"key": key},
            )
            return (result.rowcount or 0) > 0


__all__ = ["AppSettingsRepository"]
