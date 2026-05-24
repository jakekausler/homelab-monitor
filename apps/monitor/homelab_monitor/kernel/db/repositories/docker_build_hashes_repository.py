"""Repository for the docker_build_hashes table (STAGE-003-009).

Per-container build-context source hashes for locally-built images.
Mirrors ImageUpdateStateRepository: static *_conn helpers for collector
transactions; instance reads for the API. _VALID_ERROR_REASONS must match
the CHECK constraint in migration 0027.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from homelab_monitor.kernel.db.repository import SqliteRepository

_VALID_ERROR_REASONS: frozenset[str] = frozenset(
    {
        "compose_unreadable",
        "context_missing",
        "context_too_large",
        "permission_denied",
        "unknown",
    }
)


@dataclass(frozen=True, slots=True)
class DockerBuildHashRow:
    container_name: str
    compose_service: str
    build_context_path: str
    last_source_hash: str | None
    last_checked_at: str | None
    check_failed_at: str | None
    check_error_reason: str | None
    update_available: bool
    baseline_source_hash: str | None
    baseline_image_id: str | None


class DockerBuildHashesRepository:
    def __init__(self, repo: SqliteRepository) -> None:
        self._repo: SqliteRepository = repo

    # ---- Static *_conn helpers ----

    @staticmethod
    async def upsert_conn(  # noqa: PLR0913
        conn: AsyncConnection,
        *,
        container_name: str,
        compose_service: str,
        build_context_path: str,
        last_source_hash: str | None,
        last_checked_at: str | None,
        check_failed_at: str | None,
        check_error_reason: str | None,
        update_available: bool,
        baseline_source_hash: str | None,
        baseline_image_id: str | None,
    ) -> None:
        """Insert or update a row, keyed by container_name.

        Validates check_error_reason against the frozenset of allowed reasons.
        """
        if check_error_reason is not None and check_error_reason not in _VALID_ERROR_REASONS:
            msg = f"invalid check_error_reason: {check_error_reason!r}"
            raise ValueError(msg)
        await conn.execute(
            text(
                "INSERT INTO docker_build_hashes "
                "  (container_name, compose_service, build_context_path, "
                "   last_source_hash, last_checked_at, check_failed_at, "
                "   check_error_reason, update_available, "
                "   baseline_source_hash, baseline_image_id) "
                "VALUES (:cn, :svc, :bcp, :lsh, :lca, :cfa, :cer, :ua, :bsh, :bid) "
                "ON CONFLICT(container_name) DO UPDATE SET "
                "  compose_service = excluded.compose_service, "
                "  build_context_path = excluded.build_context_path, "
                "  last_source_hash = excluded.last_source_hash, "
                "  last_checked_at = excluded.last_checked_at, "
                "  check_failed_at = excluded.check_failed_at, "
                "  check_error_reason = excluded.check_error_reason, "
                "  update_available = excluded.update_available, "
                "  baseline_source_hash = excluded.baseline_source_hash, "
                "  baseline_image_id = excluded.baseline_image_id"
            ),
            {
                "cn": container_name,
                "svc": compose_service,
                "bcp": build_context_path,
                "lsh": last_source_hash,
                "lca": last_checked_at,
                "cfa": check_failed_at,
                "cer": check_error_reason,
                "ua": 1 if update_available else 0,
                "bsh": baseline_source_hash,
                "bid": baseline_image_id,
            },
        )

    @staticmethod
    async def get_by_container_conn(
        conn: AsyncConnection,
        *,
        container_name: str,
    ) -> DockerBuildHashRow | None:
        """Fetch a row within an existing transaction."""
        result = await conn.execute(
            text(
                "SELECT container_name, compose_service, build_context_path, "
                "  last_source_hash, last_checked_at, check_failed_at, "
                "  check_error_reason, update_available, "
                "  baseline_source_hash, baseline_image_id "
                "FROM docker_build_hashes WHERE container_name = :cn"
            ),
            {"cn": container_name},
        )
        row = result.first()
        if row is None:
            return None
        return _row_to_dataclass(row)

    @staticmethod
    async def list_all_conn(conn: AsyncConnection) -> list[DockerBuildHashRow]:
        """Fetch all rows within an existing transaction."""
        result = await conn.execute(
            text(
                "SELECT container_name, compose_service, build_context_path, "
                "  last_source_hash, last_checked_at, check_failed_at, "
                "  check_error_reason, update_available, "
                "  baseline_source_hash, baseline_image_id "
                "FROM docker_build_hashes ORDER BY container_name"
            )
        )
        return [_row_to_dataclass(r) for r in result.fetchall()]

    @staticmethod
    async def delete_by_container_conn(
        conn: AsyncConnection,
        *,
        container_names: set[str],
    ) -> int:
        """Delete rows for the given container_names. Returns row count.

        If `container_names` is empty, returns 0 without executing a query.
        Callers should not rely on this for connection liveness checks.
        """
        if not container_names:
            return 0
        placeholders: list[str] = []
        params: dict[str, object] = {}
        for i, cn in enumerate(sorted(container_names)):
            placeholders.append(f":cn_{i}")
            params[f"cn_{i}"] = cn
        in_clause = ", ".join(placeholders)
        result = await conn.execute(
            text(f"DELETE FROM docker_build_hashes WHERE container_name IN ({in_clause})"),
            params,
        )
        return result.rowcount or 0

    # ---- Instance reads ----

    async def get_by_container(self, container_name: str) -> DockerBuildHashRow | None:
        """Fetch a single row by container_name (opens its own connection)."""
        rows = await self._repo.fetch_all(
            text(
                "SELECT container_name, compose_service, build_context_path, "
                "  last_source_hash, last_checked_at, check_failed_at, "
                "  check_error_reason, update_available, "
                "  baseline_source_hash, baseline_image_id "
                "FROM docker_build_hashes WHERE container_name = :cn"
            ),
            {"cn": container_name},
        )
        if not rows:
            return None
        return _row_to_dataclass(rows[0])

    async def list_all(self) -> list[DockerBuildHashRow]:
        """Fetch all rows (opens its own connection)."""
        rows = await self._repo.fetch_all(
            text(
                "SELECT container_name, compose_service, build_context_path, "
                "  last_source_hash, last_checked_at, check_failed_at, "
                "  check_error_reason, update_available, "
                "  baseline_source_hash, baseline_image_id "
                "FROM docker_build_hashes ORDER BY container_name"
            )
        )
        return [_row_to_dataclass(r) for r in rows]


def _row_to_dataclass(r: Any) -> DockerBuildHashRow:  # noqa: ANN401
    """Convert a SQLite row to a DockerBuildHashRow dataclass."""
    return DockerBuildHashRow(
        container_name=str(r.container_name),  # pyright: ignore[reportAttributeAccessIssue]
        compose_service=str(r.compose_service),  # pyright: ignore[reportAttributeAccessIssue]
        build_context_path=str(r.build_context_path),  # pyright: ignore[reportAttributeAccessIssue]
        last_source_hash=(
            None if r.last_source_hash is None else str(r.last_source_hash)  # pyright: ignore[reportAttributeAccessIssue]
        ),
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
        baseline_source_hash=(
            None if r.baseline_source_hash is None else str(r.baseline_source_hash)  # pyright: ignore[reportAttributeAccessIssue]
        ),
        baseline_image_id=(
            None if r.baseline_image_id is None else str(r.baseline_image_id)  # pyright: ignore[reportAttributeAccessIssue]
        ),
    )


__all__ = [
    "_VALID_ERROR_REASONS",
    "DockerBuildHashRow",
    "DockerBuildHashesRepository",
]
