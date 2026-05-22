"""ProbeTargetsRepository — probe_targets CRUD.

Mirrors SuggestionsRepository pattern: static *_conn helpers operate
inside an external repo.transaction(); instance methods serve API
reads.

STAGE-003-006: label-derived probe configurations.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from homelab_monitor.kernel.db.ids import uuid7
from homelab_monitor.kernel.db.repository import SqliteRepository

ProbeKind = Literal["http", "tcp", "exec", "metrics"]
ProbeStatus = Literal["ok", "fail"]
ConfigSource = Literal["label", "file_override", "auto_default", "discovered_accepted"]

# Per-probe interval/timeout customization is NOT supported via the
# homelab-monitor.<kind>.<name> label DSL (D-LABEL-NAMESPACE locks the syntax).
# The probe_targets table stores these per-row so future config sources
# (file_override in STAGE-003-007+) can customize them. For now, all
# label-derived probes use these defaults.
_DEFAULT_INTERVAL_SECONDS: Final[int] = 30
_DEFAULT_TIMEOUT_SECONDS: Final[int] = 5


@dataclass(frozen=True, slots=True)
class ProbeTargetRow:
    id: str
    container_name: str
    kind: str
    name: str
    target_value: str
    config_source: str
    enabled: bool
    interval_seconds: int
    timeout_seconds: int
    last_run_at: str | None
    last_status: str | None
    last_error: str | None
    created_at: str
    hidden_at: str | None


@dataclass(frozen=True, slots=True)
class ProbeSummaryRow:
    container_name: str
    active: int
    failing: int


class ProbeTargetsRepository:
    def __init__(self, repo: SqliteRepository) -> None:
        self._repo: SqliteRepository = repo

    # ---- Static *_conn helpers ----

    @staticmethod
    async def upsert_probe_target_conn(  # noqa: PLR0913
        conn: AsyncConnection,
        *,
        container_name: str,
        kind: str,
        name: str,
        target_value: str,
        config_source: str,
        enabled: bool = True,
        interval_seconds: int = _DEFAULT_INTERVAL_SECONDS,
        timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
        now: str,
    ) -> str:
        """Insert or update by (container_name, kind, name). Returns id.

        On UPDATE preserves last_run_at/last_status/last_error AND existing
        enabled flag (manual disable persists across discoverer re-tick).
        Un-hides the row if hidden_at was set.
        """
        # See if a row exists (including hidden ones)
        row = await conn.execute(
            text(
                "SELECT id FROM probe_targets "
                "WHERE container_name = :cn AND kind = :k AND name = :n"
            ),
            {"cn": container_name, "k": kind, "n": name},
        )
        existing = row.first()
        if existing is not None:
            probe_id = str(existing.id)
            # UPDATE: only refresh target_value/config_source/interval/timeout
            # AND clear hidden_at (label reappeared). Do NOT overwrite enabled
            # (manual disable persists) and do NOT overwrite outcome fields.
            await conn.execute(
                text(
                    "UPDATE probe_targets SET "
                    "  target_value = :tv, "
                    "  config_source = :cs, "
                    "  interval_seconds = :is_, "
                    "  timeout_seconds = :ts, "
                    "  hidden_at = NULL "
                    "WHERE id = :id"
                ),
                {
                    "tv": target_value,
                    "cs": config_source,
                    "is_": interval_seconds,
                    "ts": timeout_seconds,
                    "id": probe_id,
                },
            )
            return probe_id

        probe_id = uuid7()
        await conn.execute(
            text(
                "INSERT INTO probe_targets "
                "  (id, container_name, kind, name, target_value, config_source, "
                "   enabled, interval_seconds, timeout_seconds, created_at) "
                "VALUES (:id, :cn, :k, :n, :tv, :cs, :en, :is_, :ts, :now)"
            ),
            {
                "id": probe_id,
                "cn": container_name,
                "k": kind,
                "n": name,
                "tv": target_value,
                "cs": config_source,
                "en": 1 if enabled else 0,
                "is_": interval_seconds,
                "ts": timeout_seconds,
                "now": now,
            },
        )
        return probe_id

    @staticmethod
    async def set_enabled_conn(
        conn: AsyncConnection,
        *,
        probe_id: str,
        enabled: bool,
    ) -> int:
        result = await conn.execute(
            text("UPDATE probe_targets SET enabled = :en WHERE id = :id"),
            {"en": 1 if enabled else 0, "id": probe_id},
        )
        return result.rowcount or 0

    @staticmethod
    async def update_run_outcome_conn(
        conn: AsyncConnection,
        *,
        probe_id: str,
        status: str,
        error: str | None,
        now: str,
    ) -> None:
        await conn.execute(
            text(
                "UPDATE probe_targets SET "
                "  last_run_at = :now, last_status = :st, last_error = :err "
                "WHERE id = :id"
            ),
            {"now": now, "st": status, "err": error, "id": probe_id},
        )

    @staticmethod
    async def mark_missing_except_conn(
        conn: AsyncConnection,
        *,
        container_name: str,
        kept_keys: set[tuple[str, str]],
        now: str,
    ) -> int:
        """Soft-delete (set hidden_at) any probe rows for container_name
        whose (kind, name) is NOT in kept_keys. Returns row count affected.

        Idempotent — already-hidden rows are NOT re-stamped (preserves the
        original hidden_at timestamp).
        """
        if not kept_keys:
            result = await conn.execute(
                text(
                    "UPDATE probe_targets SET hidden_at = :now "
                    "WHERE container_name = :cn AND hidden_at IS NULL"
                ),
                {"cn": container_name, "now": now},
            )
            return result.rowcount or 0
        # Build NOT IN clause — only placeholder names are interpolated, values are bound.
        placeholders: list[str] = []
        params: dict[str, object] = {"cn": container_name, "now": now}
        for i, (k, n) in enumerate(kept_keys):
            placeholders.append(f"(:k_{i}, :n_{i})")
            params[f"k_{i}"] = k
            params[f"n_{i}"] = n
        in_clause = ", ".join(placeholders)
        result = await conn.execute(
            text(
                f"UPDATE probe_targets SET hidden_at = :now "
                f"WHERE container_name = :cn "
                f"  AND hidden_at IS NULL "
                f"  AND (kind, name) NOT IN ({in_clause})"
            ),
            params,
        )
        return result.rowcount or 0

    # ---- Instance reads ----

    async def list_for_container(
        self,
        *,
        container_name: str,
        include_hidden: bool = False,
    ) -> list[ProbeTargetRow]:
        sql = (
            "SELECT id, container_name, kind, name, target_value, config_source, "
            "  enabled, interval_seconds, timeout_seconds, last_run_at, "
            "  last_status, last_error, created_at, hidden_at "
            "FROM probe_targets WHERE container_name = :cn "
        )
        if not include_hidden:
            sql += "  AND hidden_at IS NULL "
        sql += "ORDER BY kind, name"
        rows = await self._repo.fetch_all(text(sql), {"cn": container_name})
        result: list[ProbeTargetRow] = []
        for r in rows:
            result.append(
                ProbeTargetRow(
                    id=str(r.id),
                    container_name=str(r.container_name),
                    kind=str(r.kind),
                    name=str(r.name),
                    target_value=str(r.target_value),
                    config_source=str(r.config_source),
                    enabled=bool(r.enabled),
                    interval_seconds=int(r.interval_seconds),
                    timeout_seconds=int(r.timeout_seconds),
                    last_run_at=None if r.last_run_at is None else str(r.last_run_at),
                    last_status=None if r.last_status is None else str(r.last_status),
                    last_error=None if r.last_error is None else str(r.last_error),
                    created_at=str(r.created_at),
                    hidden_at=None if r.hidden_at is None else str(r.hidden_at),
                )
            )
        return result

    async def get_by_id(self, probe_id: str) -> ProbeTargetRow | None:
        rows = await self._repo.fetch_all(
            text(
                "SELECT id, container_name, kind, name, target_value, config_source, "
                "  enabled, interval_seconds, timeout_seconds, last_run_at, "
                "  last_status, last_error, created_at, hidden_at "
                "FROM probe_targets WHERE id = :id"
            ),
            {"id": probe_id},
        )
        if not rows:
            return None
        r = rows[0]
        return ProbeTargetRow(
            id=str(r.id),
            container_name=str(r.container_name),
            kind=str(r.kind),
            name=str(r.name),
            target_value=str(r.target_value),
            config_source=str(r.config_source),
            enabled=bool(r.enabled),
            interval_seconds=int(r.interval_seconds),
            timeout_seconds=int(r.timeout_seconds),
            last_run_at=None if r.last_run_at is None else str(r.last_run_at),
            last_status=None if r.last_status is None else str(r.last_status),
            last_error=None if r.last_error is None else str(r.last_error),
            created_at=str(r.created_at),
            hidden_at=None if r.hidden_at is None else str(r.hidden_at),
        )

    async def list_distinct_container_names_with_enabled_probes(self) -> list[str]:
        """Return container_names that have at least one enabled, non-hidden probe.

        Used by ProbeSupervisor to reconcile per-container task set.
        """
        rows = await self._repo.fetch_all(
            text(
                "SELECT DISTINCT container_name FROM probe_targets "
                "WHERE enabled = 1 AND hidden_at IS NULL "
                "ORDER BY container_name"
            )
        )
        return [str(r.container_name) for r in rows]

    async def summarize_by_container(self) -> list[ProbeSummaryRow]:
        """One row per container_name that has at least one enabled, non-hidden probe.

        active = count of enabled probes; failing = count where last_status='fail' or 'error'.
        Containers with zero probes omitted.
        """
        rows = await self._repo.fetch_all(
            text(
                "SELECT container_name, "
                "  COUNT(*) AS active, "
                "  SUM(CASE WHEN last_status IN ('fail', 'error') THEN 1 ELSE 0 END) AS failing "
                "FROM probe_targets "
                "WHERE enabled = 1 AND hidden_at IS NULL "
                "GROUP BY container_name "
                "ORDER BY container_name"
            )
        )
        return [
            ProbeSummaryRow(
                container_name=str(r.container_name),
                active=int(r.active),
                failing=int(r.failing or 0),
            )
            for r in rows
        ]


__all__ = [
    "ConfigSource",
    "ProbeKind",
    "ProbeStatus",
    "ProbeSummaryRow",
    "ProbeTargetRow",
    "ProbeTargetsRepository",
]
