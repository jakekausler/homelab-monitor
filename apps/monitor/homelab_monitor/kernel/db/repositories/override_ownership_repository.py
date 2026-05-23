"""Repository for the docker_override_ownership table.

Tracks the set of container_names currently owned by OverrideLoader.
DockerDiscoverer queries `list_owned_conn` at the start of each tick to
skip the label-upsert path for owned containers (D-OWNERSHIP-COORDINATION-
VIA-SQLITE). OverrideLoader calls `set_owned_conn` once per refresh tick
to replace the full set atomically.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from homelab_monitor.kernel.db.repository import SqliteRepository


class OverrideOwnershipRepository:
    def __init__(self, repo: SqliteRepository) -> None:
        self._repo: SqliteRepository = repo

    # ---- Static *_conn helpers (called inside an external transaction) ----

    @staticmethod
    async def list_owned_conn(conn: AsyncConnection) -> set[str]:
        """Return the set of container_names currently claimed by the loader."""
        result = await conn.execute(text("SELECT container_name FROM docker_override_ownership"))
        return {str(row.container_name) for row in result.fetchall()}

    @staticmethod
    async def set_owned_conn(
        conn: AsyncConnection,
        *,
        container_names: set[str],
        now: str,
    ) -> None:
        """Replace the full owned set in one transaction.

        Pattern: DELETE rows whose container_name is NOT in the new set,
        then upsert all members of the new set. Idempotent.
        """
        if not container_names:
            await conn.execute(text("DELETE FROM docker_override_ownership"))
            return
        placeholders: list[str] = []
        params: dict[str, object] = {"now": now}
        for i, cn in enumerate(container_names):
            placeholders.append(f":cn_{i}")
            params[f"cn_{i}"] = cn
        in_clause = ", ".join(placeholders)
        # Delete rows whose container_name has dropped out of the new set.
        await conn.execute(
            text(
                f"DELETE FROM docker_override_ownership WHERE container_name NOT IN ({in_clause})"
            ),
            params,
        )
        # Upsert each member; reuse claimed_at on conflict (preserves first-claimed time).
        # Early-return at line 42 guarantees container_names is non-empty here.
        sorted_names = sorted(container_names)
        values_clauses = [f"(:cn_{i}, :now)" for i in range(len(sorted_names))]
        params = {"now": now}
        for i, cn in enumerate(sorted_names):
            params[f"cn_{i}"] = cn
        await conn.execute(
            text(
                "INSERT INTO docker_override_ownership (container_name, claimed_at) "
                "VALUES " + ", ".join(values_clauses) + " "
                "ON CONFLICT(container_name) DO NOTHING"
            ),
            params,
        )

    # ---- Instance reads (test helpers / debug; not called in hot path) ----

    async def list_owned(self) -> set[str]:
        rows = await self._repo.fetch_all(
            text("SELECT container_name FROM docker_override_ownership")
        )
        return {str(r.container_name) for r in rows}


__all__ = ["OverrideOwnershipRepository"]
