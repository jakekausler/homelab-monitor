"""Repository for the image_update_state table.

Per-container image-update check state (STAGE-003-008).
Mirrors ProbeTargetsRepository pattern: static *_conn helpers for external
transactions; instance reads for API consumers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from homelab_monitor.kernel.db.repository import SqliteRepository

_VALID_ERROR_REASONS: frozenset[str] = frozenset(
    {
        "parse_failed",
        "network_error",
        "auth_failed",
        "rate_limited",
        "not_found",
    }
)


@dataclass(frozen=True, slots=True)
class ImageUpdateStateRow:
    container_name: str
    last_local_digest: str | None
    last_registry_digest: str | None
    last_image_ref: str
    last_checked_at: str | None
    check_failed_at: str | None
    check_error_reason: str | None
    update_available: bool


class ImageUpdateStateRepository:
    def __init__(self, repo: SqliteRepository) -> None:
        self._repo: SqliteRepository = repo

    # ---- Static *_conn helpers ----

    @staticmethod
    async def upsert_state_conn(  # noqa: PLR0913
        conn: AsyncConnection,
        *,
        container_name: str,
        last_image_ref: str,
        last_local_digest: str | None,
        last_registry_digest: str | None,
        last_checked_at: str | None,
        check_failed_at: str | None,
        check_error_reason: str | None,
        update_available: bool,
        now: str,
    ) -> None:
        """Insert or update a row, keyed by container_name.

        NOTE: last_image_ref is stored verbatim from the Docker API (raw ref, not canonicalized).
        NOTE: rows are only written for containers whose image ref parses successfully.
        Unparseable refs (<none>, sha256-only) are silently skipped; they do not get
        a DB row. This is intentional — there is no meaningful digest to track.
        (I5: NOT NULL on last_image_ref is safe because we only write parseable refs.)
        """
        if check_error_reason is not None and check_error_reason not in _VALID_ERROR_REASONS:
            msg = f"invalid check_error_reason: {check_error_reason!r}"
            raise ValueError(msg)
        await conn.execute(
            text(
                "INSERT INTO image_update_state "
                "  (container_name, last_local_digest, last_registry_digest, "
                "   last_image_ref, last_checked_at, check_failed_at, "
                "   check_error_reason, update_available) "
                "VALUES (:cn, :lld, :lrd, :lir, :lca, :cfa, :cer, :ua) "
                "ON CONFLICT(container_name) DO UPDATE SET "
                "  last_local_digest = excluded.last_local_digest, "
                "  last_registry_digest = excluded.last_registry_digest, "
                "  last_image_ref = excluded.last_image_ref, "
                "  last_checked_at = excluded.last_checked_at, "
                "  check_failed_at = excluded.check_failed_at, "
                "  check_error_reason = excluded.check_error_reason, "
                "  update_available = excluded.update_available"
            ),
            {
                "cn": container_name,
                "lld": last_local_digest,
                "lrd": last_registry_digest,
                "lir": last_image_ref,
                "lca": last_checked_at,
                "cfa": check_failed_at,
                "cer": check_error_reason,
                "ua": 1 if update_available else 0,
            },
        )

    @staticmethod
    async def get_by_container_conn(
        conn: AsyncConnection,
        *,
        container_name: str,
    ) -> ImageUpdateStateRow | None:
        result = await conn.execute(
            text(
                "SELECT container_name, last_local_digest, last_registry_digest, "
                "  last_image_ref, last_checked_at, check_failed_at, "
                "  check_error_reason, update_available "
                "FROM image_update_state WHERE container_name = :cn"
            ),
            {"cn": container_name},
        )
        row = result.first()
        if row is None:
            return None
        return _row_to_dataclass(row)

    @staticmethod
    async def delete_by_container_conn(
        conn: AsyncConnection,
        *,
        container_names: set[str],
    ) -> int:
        """Delete rows for the given container_names. Returns row count."""
        if not container_names:
            return 0
        placeholders: list[str] = []
        params: dict[str, object] = {}
        for i, cn in enumerate(container_names):
            placeholders.append(f":cn_{i}")
            params[f"cn_{i}"] = cn
        in_clause = ", ".join(placeholders)
        result = await conn.execute(
            text(f"DELETE FROM image_update_state WHERE container_name IN ({in_clause})"),
            params,
        )
        return result.rowcount or 0

    @staticmethod
    async def list_all_conn(conn: AsyncConnection) -> list[ImageUpdateStateRow]:
        """Fetch all rows within an existing transaction."""
        result = await conn.execute(
            text(
                "SELECT container_name, last_local_digest, last_registry_digest, "
                "  last_image_ref, last_checked_at, check_failed_at, "
                "  check_error_reason, update_available "
                "FROM image_update_state ORDER BY container_name"
            )
        )
        return [_row_to_dataclass(r) for r in result.fetchall()]

    # ---- Instance reads ----

    async def get_by_container(self, container_name: str) -> ImageUpdateStateRow | None:
        rows = await self._repo.fetch_all(
            text(
                "SELECT container_name, last_local_digest, last_registry_digest, "
                "  last_image_ref, last_checked_at, check_failed_at, "
                "  check_error_reason, update_available "
                "FROM image_update_state WHERE container_name = :cn"
            ),
            {"cn": container_name},
        )
        if not rows:
            return None
        return _row_to_dataclass(rows[0])

    async def list_all(self) -> list[ImageUpdateStateRow]:
        rows = await self._repo.fetch_all(
            text(
                "SELECT container_name, last_local_digest, last_registry_digest, "
                "  last_image_ref, last_checked_at, check_failed_at, "
                "  check_error_reason, update_available "
                "FROM image_update_state ORDER BY container_name"
            )
        )
        return [_row_to_dataclass(r) for r in rows]


def _row_to_dataclass(r: Any) -> ImageUpdateStateRow:  # noqa: ANN401
    return ImageUpdateStateRow(
        container_name=str(r.container_name),  # pyright: ignore[reportAttributeAccessIssue]
        last_local_digest=(
            None if r.last_local_digest is None else str(r.last_local_digest)  # pyright: ignore[reportAttributeAccessIssue]
        ),
        last_registry_digest=(
            None if r.last_registry_digest is None else str(r.last_registry_digest)  # pyright: ignore[reportAttributeAccessIssue]
        ),
        last_image_ref=str(r.last_image_ref),  # pyright: ignore[reportAttributeAccessIssue]
        last_checked_at=(
            None if r.last_checked_at is None else str(r.last_checked_at)  # pyright: ignore[reportAttributeAccessIssue]
        ),
        check_failed_at=(
            None if r.check_failed_at is None else str(r.check_failed_at)  # pyright: ignore[reportAttributeAccessIssue]
        ),
        check_error_reason=(
            None if r.check_error_reason is None else str(r.check_error_reason)  # pyright: ignore[reportAttributeAccessIssue]
        ),
        update_available=bool(r.update_available),  # pyright: ignore[reportAttributeAccessIssue]
    )


__all__ = ["_VALID_ERROR_REASONS", "ImageUpdateStateRepository", "ImageUpdateStateRow"]
