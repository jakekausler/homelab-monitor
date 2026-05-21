"""TargetsRepository — generic + docker-sidecar CRUD.

Patterns:
  - The generic `targets` row carries (id, name, kind, status, first_seen,
    last_seen, hidden_at, labels JSON, source).
  - The `targets_docker` sidecar row stores Docker-specific fields keyed by
    target_id (FK CASCADE on delete).
  - Inserts use `INSERT OR IGNORE` then `UPDATE` (sqlite upsert that survives
    on older sqlite-WAL builds without ON CONFLICT support — defensive).
  - For atomic multi-row operations the caller passes an `AsyncConnection`
    via the `_conn` suffixed helpers (matches how lifespan.py composes
    repository methods inside `repo.transaction()`).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from homelab_monitor.kernel.db.repository import SqliteRepository


@dataclass(frozen=True, slots=True)
class DockerContainerListRow:
    """Joined view: targets (generic) + targets_docker (sidecar)."""

    id: str
    name: str
    status: str | None
    image: str | None
    restart_count: int | None
    exit_code: int | None
    healthcheck: str | None
    network_mode: str | None
    cpu_pct_cached: float | None
    mem_mib_cached: float | None
    labels: dict[str, str]
    first_seen: str | None
    last_seen: str | None
    hidden_at: str | None


class TargetsRepository:
    """Repository for generic + docker-specific targets."""

    def __init__(self, repo: SqliteRepository) -> None:
        self._repo: SqliteRepository = repo

    # ---- Static helpers usable inside an external transaction ----

    @staticmethod
    async def upsert_docker_container_conn(  # noqa: PLR0913
        conn: AsyncConnection,
        *,
        target_id: str,
        name: str,
        status: str,
        image: str,
        restart_count: int,
        exit_code: int,
        healthcheck: str | None,
        network_mode: str,
        labels: dict[str, str],
        now: str,
        cpu_pct: float | None,
        mem_mib: float | None,
    ) -> None:
        """Insert-or-update generic targets row + sidecar row in one tx."""
        labels_json = json.dumps(labels, sort_keys=True)
        await conn.execute(
            text(
                "INSERT INTO targets "
                "  (id, name, kind, status, first_seen, last_seen, labels, source, created_at) "
                "VALUES (:id, :name, 'docker_container', :status, :now, :now, "
                "        :labels, 'docker_socket', :now) "
                "ON CONFLICT(id) DO UPDATE SET "
                "  name = excluded.name, "
                "  status = excluded.status, "
                "  last_seen = excluded.last_seen, "
                "  labels = excluded.labels, "
                "  kind = excluded.kind, "
                "  source = excluded.source"
            ),
            {
                "id": target_id,
                "name": name,
                "status": status,
                "labels": labels_json,
                "now": now,
            },
        )

        await conn.execute(
            text(
                "INSERT INTO targets_docker (target_id, restart_count, exit_code, "
                "  healthcheck, image, network_mode, cpu_pct_cached, mem_mib_cached, "
                "  metrics_cached_at) "
                "VALUES (:tid, :rc, :ec, :hc, :img, :nm, :cpu, :mem, :mca) "
                "ON CONFLICT(target_id) DO UPDATE SET "
                "  restart_count = excluded.restart_count, "
                "  exit_code = excluded.exit_code, "
                "  healthcheck = excluded.healthcheck, "
                "  image = excluded.image, "
                "  network_mode = excluded.network_mode, "
                "  cpu_pct_cached = CASE WHEN excluded.cpu_pct_cached IS NULL "
                "    THEN targets_docker.cpu_pct_cached ELSE excluded.cpu_pct_cached END, "
                "  mem_mib_cached = CASE WHEN excluded.mem_mib_cached IS NULL "
                "    THEN targets_docker.mem_mib_cached ELSE excluded.mem_mib_cached END, "
                "  metrics_cached_at = CASE WHEN excluded.cpu_pct_cached IS NULL "
                "    AND excluded.mem_mib_cached IS NULL "
                "    THEN targets_docker.metrics_cached_at ELSE excluded.metrics_cached_at END"
            ),
            {
                "tid": target_id,
                "rc": restart_count,
                "ec": exit_code,
                "hc": healthcheck,
                "img": image,
                "nm": network_mode,
                "cpu": cpu_pct,
                "mem": mem_mib,
                "mca": now if (cpu_pct is not None or mem_mib is not None) else None,
            },
        )

    @staticmethod
    async def mark_missing_except_conn(
        conn: AsyncConnection,
        *,
        seen_ids: set[str],
        now: str,
    ) -> None:
        """Mark Docker targets not in `seen_ids` as status='missing' (D-MISSING-NOT-DELETED).

        Uses a SQLite-friendly NOT IN(...) — fine for homelab-scale (<200
        containers). If we ever exceed that, switch to a TEMP-TABLE join.
        """
        if not seen_ids:
            await conn.execute(
                text(
                    "UPDATE targets SET status='missing', last_seen=:now "
                    "WHERE kind='docker_container' AND status != 'missing'"
                ),
                {"now": now},
            )
            return
        # NOTE: Only the placeholder NAMES (`:id_N`) are f-string interpolated. The actual
        # container IDs are bound via the params dict below — never f-string interpolated.
        # This is safe against SQL injection even if Docker were to return malicious IDs.
        placeholders = ", ".join(f":id_{i}" for i in range(len(seen_ids)))
        params: dict[str, Any] = {"now": now}
        for i, sid in enumerate(seen_ids):
            params[f"id_{i}"] = sid
        await conn.execute(
            text(
                f"UPDATE targets SET status='missing', last_seen=:now "
                f"WHERE kind='docker_container' AND id NOT IN ({placeholders}) "
                f"  AND status != 'missing'"
            ),
            params,
        )

    # ---- High-level (non-transactional) reads used by API ----

    async def list_docker_containers(
        self,
        *,
        include_hidden: bool = False,
    ) -> list[DockerContainerListRow]:
        """LEFT JOIN targets + targets_docker; ordered by name ASC."""
        sql = (
            "SELECT t.id AS id, t.name AS name, t.status AS status, t.labels AS labels, "
            "  t.first_seen AS first_seen, t.last_seen AS last_seen, t.hidden_at AS hidden_at, "
            "  d.image AS image, d.restart_count AS restart_count, d.exit_code AS exit_code, "
            "  d.healthcheck AS healthcheck, d.network_mode AS network_mode, "
            "  d.cpu_pct_cached AS cpu_pct_cached, d.mem_mib_cached AS mem_mib_cached "
            "FROM targets t "
            "LEFT JOIN targets_docker d ON d.target_id = t.id "
            "WHERE t.kind = 'docker_container' "
        )
        if not include_hidden:
            sql += "  AND t.hidden_at IS NULL "
        sql += "ORDER BY t.name ASC"
        rows = await self._repo.fetch_all(text(sql))
        result: list[DockerContainerListRow] = []
        for row in rows:
            labels_raw: str | None = row.labels
            labels_dict: dict[str, str] = json.loads(labels_raw) if labels_raw else {}
            result.append(
                DockerContainerListRow(
                    id=str(row.id),
                    name=str(row.name),
                    status=None if row.status is None else str(row.status),
                    image=None if row.image is None else str(row.image),
                    restart_count=None if row.restart_count is None else int(row.restart_count),
                    exit_code=None if row.exit_code is None else int(row.exit_code),
                    healthcheck=None if row.healthcheck is None else str(row.healthcheck),
                    network_mode=None if row.network_mode is None else str(row.network_mode),
                    cpu_pct_cached=None
                    if row.cpu_pct_cached is None
                    else float(row.cpu_pct_cached),
                    mem_mib_cached=None
                    if row.mem_mib_cached is None
                    else float(row.mem_mib_cached),
                    labels=labels_dict,
                    first_seen=None if row.first_seen is None else str(row.first_seen),
                    last_seen=None if row.last_seen is None else str(row.last_seen),
                    hidden_at=None if row.hidden_at is None else str(row.hidden_at),
                )
            )
        return result
