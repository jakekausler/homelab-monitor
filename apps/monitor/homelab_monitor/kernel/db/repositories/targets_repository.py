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

from homelab_monitor.kernel.db.ids import uuid7
from homelab_monitor.kernel.db.repository import SqliteRepository


@dataclass(frozen=True, slots=True)
class DockerContainerListRow:
    """Joined view: targets (generic) + targets_docker (sidecar)."""

    id: str
    name: str
    status: str | None
    image: str | None
    container_id: str | None  # NEW — the current Docker container ID
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
    logical_key_kind: str | None  # NEW
    logical_key: str | None  # NEW
    previous_container_id: str | None  # NEW
    recreated_at: str | None  # NEW
    compose_project: str | None  # STAGE-003-005 Q2
    compose_service: str | None  # STAGE-003-005 Q2
    compose_file_path: str | None  # STAGE-003-005 Q2
    restart_count_24h_cached: int | None  # STAGE-003-005 Q1


class TargetsRepository:
    """Repository for generic + docker-specific targets."""

    def __init__(self, repo: SqliteRepository) -> None:
        self._repo: SqliteRepository = repo

    # ---- Static helpers usable inside an external transaction ----

    @staticmethod
    async def upsert_docker_container_conn(  # noqa: PLR0913
        conn: AsyncConnection,
        *,
        container_id: str,
        name: str,
        status: str,
        image: str,
        restart_count: int,
        exit_code: int,
        healthcheck: str | None,
        network_mode: str,
        labels: dict[str, str],
        logical_key_kind: str,
        logical_key: str,
        now: str,
        cpu_pct: float | None,
        mem_mib: float | None,
        compose_project: str | None = None,
        compose_service: str | None = None,
        compose_file_path: str | None = None,
        restart_count_24h: int | None = None,
    ) -> str:
        """Insert-or-update generic targets row + sidecar by LOGICAL KEY.

        Returns the resolved targets.id UUID. The caller uses this as the
        member of `seen_ids` passed to `mark_missing_except_conn` so the
        mark-missing logic continues to operate on UUIDs.

        On UPDATE where the container_id has CHANGED vs the prior sidecar
        row, we set previous_container_id to the OLD container_id and
        recreated_at to `now`. This is the one-level recreation forensic
        trail (D-FORENSICS-ONE-LEVEL).
        """
        labels_json = json.dumps(labels, sort_keys=True)

        # 1. Resolve target_id via logical_key lookup (partial unique index
        #    ux_targets_docker_logical_key keeps this O(1)).
        row = await conn.execute(
            text(
                "SELECT t.id AS id, d.container_id AS prior_container_id "
                "FROM targets t "
                "LEFT JOIN targets_docker d ON d.target_id = t.id "
                "WHERE t.kind = 'docker_container' "
                "  AND t.logical_key_kind = :lkk AND t.logical_key = :lk"
            ),
            {"lkk": logical_key_kind, "lk": logical_key},
        )
        existing = row.first()
        is_recreation = False
        prior_container_id: str | None = None
        if existing is None:
            target_id = uuid7()
        else:
            target_id = str(existing.id)
            prior_container_id = (
                None if existing.prior_container_id is None else str(existing.prior_container_id)
            )
            is_recreation = prior_container_id is not None and prior_container_id != container_id

        # 2. Upsert anchor row. The anchor INSERT path runs only when this is
        #    a brand-new logical service; the UPDATE path runs on every tick
        #    for existing services. logical_key_* are written on INSERT only
        #    (they never change for a given target.id).
        # TODO: remove `kind = excluded.kind` and `source = excluded.source` from
        # this UPDATE clause — they're literals in INSERT and never change. Costs
        # a redundant write on every upsert. See code review I6.
        await conn.execute(
            text(
                "INSERT INTO targets "
                "  (id, name, kind, status, first_seen, last_seen, "
                "   logical_key_kind, logical_key, labels, source, created_at) "
                "VALUES (:id, :name, 'docker_container', :status, :now, :now, "
                "        :lkk, :lk, :labels, 'docker_socket', :now) "
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
                "lkk": logical_key_kind,
                "lk": logical_key,
                "labels": labels_json,
                "now": now,
            },
        )

        # 3. Upsert sidecar. On recreation we promote prior container_id and
        #    stamp recreated_at; otherwise we preserve whatever was there.
        await conn.execute(
            text(
                "INSERT INTO targets_docker "
                "  (target_id, container_id, restart_count, exit_code, healthcheck, "
                "   image, network_mode, cpu_pct_cached, mem_mib_cached, "
                "   metrics_cached_at, previous_container_id, recreated_at, "
                "   compose_project, compose_service, compose_file_path, restart_count_24h_cached) "
                "VALUES (:tid, :cid, :rc, :ec, :hc, :img, :nm, :cpu, :mem, :mca, "
                "        :prev, :rec, :cp, :cs, :cfp, :r24h) "
                "ON CONFLICT(target_id) DO UPDATE SET "
                "  container_id = excluded.container_id, "
                "  restart_count = excluded.restart_count, "
                "  exit_code = excluded.exit_code, "
                "  healthcheck = excluded.healthcheck, "
                "  image = excluded.image, "
                "  network_mode = excluded.network_mode, "
                "  cpu_pct_cached = CASE WHEN excluded.cpu_pct_cached IS NULL "
                "    THEN targets_docker.cpu_pct_cached ELSE excluded.cpu_pct_cached END, "
                "  mem_mib_cached = CASE WHEN excluded.mem_mib_cached IS NULL "
                "    THEN targets_docker.mem_mib_cached ELSE excluded.mem_mib_cached END, "
                # TODO: metrics_cached_at is ambiguous under partial-NULL caching.
                # Consider splitting into cpu_pct_cached_at and mem_mib_cached_at.
                # See code review I7.
                "  metrics_cached_at = CASE WHEN excluded.cpu_pct_cached IS NULL "
                "    AND excluded.mem_mib_cached IS NULL "
                "    THEN targets_docker.metrics_cached_at ELSE excluded.metrics_cached_at END, "
                # Only overwrite the recreation columns when this tick is a recreation.
                # When it's NOT a recreation, preserve whatever was there from the
                # MOST RECENT prior recreation. We keep one level only.
                "  previous_container_id = CASE WHEN :is_recreation = 1 "
                "    THEN :prev ELSE targets_docker.previous_container_id END, "
                "  recreated_at = CASE WHEN :is_recreation = 1 "
                "    THEN :rec ELSE targets_docker.recreated_at END, "
                "  compose_project = excluded.compose_project, "
                "  compose_service = excluded.compose_service, "
                "  compose_file_path = excluded.compose_file_path, "
                "  restart_count_24h_cached = CASE WHEN excluded.restart_count_24h_cached IS NULL "
                "    THEN targets_docker.restart_count_24h_cached ELSE excluded.restart_count_24h_cached END"  # noqa: E501
            ),
            {
                "tid": target_id,
                "cid": container_id,
                "rc": restart_count,
                "ec": exit_code,
                "hc": healthcheck,
                "img": image,
                "nm": network_mode,
                "cpu": cpu_pct,
                "mem": mem_mib,
                "mca": now if (cpu_pct is not None or mem_mib is not None) else None,
                "prev": prior_container_id if is_recreation else None,
                "rec": now if is_recreation else None,
                "is_recreation": 1 if is_recreation else 0,
                "cp": compose_project,
                "cs": compose_service,
                "cfp": compose_file_path,
                "r24h": restart_count_24h,
            },
        )

        return target_id

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
        """LEFT JOIN targets + targets_docker; ordered by compose_file_path NULLS LAST, name ASC."""
        sql = (
            "SELECT t.id AS id, t.name AS name, t.status AS status, t.labels AS labels, "
            "  t.first_seen AS first_seen, t.last_seen AS last_seen, t.hidden_at AS hidden_at, "
            "  t.logical_key_kind AS logical_key_kind, t.logical_key AS logical_key, "
            "  d.container_id AS container_id, d.image AS image, "
            "  d.restart_count AS restart_count, d.exit_code AS exit_code, "
            "  d.healthcheck AS healthcheck, d.network_mode AS network_mode, "
            "  d.cpu_pct_cached AS cpu_pct_cached, d.mem_mib_cached AS mem_mib_cached, "
            "  d.previous_container_id AS previous_container_id, "
            "  d.recreated_at AS recreated_at, "
            "  d.compose_project AS compose_project, "
            "  d.compose_service AS compose_service, "
            "  d.compose_file_path AS compose_file_path, "
            "  d.restart_count_24h_cached AS restart_count_24h_cached "
            "FROM targets t "
            "LEFT JOIN targets_docker d ON d.target_id = t.id "
            "WHERE t.kind = 'docker_container' "
        )
        if not include_hidden:
            sql += "  AND t.hidden_at IS NULL "
        sql += "ORDER BY d.compose_file_path IS NULL, d.compose_file_path, t.name ASC"
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
                    container_id=None if row.container_id is None else str(row.container_id),
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
                    logical_key_kind=None
                    if row.logical_key_kind is None
                    else str(row.logical_key_kind),
                    logical_key=None if row.logical_key is None else str(row.logical_key),
                    previous_container_id=None
                    if row.previous_container_id is None
                    else str(row.previous_container_id),
                    recreated_at=None if row.recreated_at is None else str(row.recreated_at),
                    compose_project=None
                    if row.compose_project is None
                    else str(row.compose_project),
                    compose_service=None
                    if row.compose_service is None
                    else str(row.compose_service),
                    compose_file_path=None
                    if row.compose_file_path is None
                    else str(row.compose_file_path),
                    restart_count_24h_cached=None
                    if row.restart_count_24h_cached is None
                    else int(row.restart_count_24h_cached),
                )
            )
        return result
