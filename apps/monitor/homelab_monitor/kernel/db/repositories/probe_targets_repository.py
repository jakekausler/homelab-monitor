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
    exec_authorized: bool


@dataclass(frozen=True, slots=True)
class ProbeSummaryRow:
    container_name: str
    active: int
    failing: int
    # STAGE-003-007 D-SUMMARY-ENDPOINT-EXTENSION:
    source_breakdown: dict[str, int]
    config_errors: list[str] | None


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
        exec_authorized: bool = False,
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
                    "  exec_authorized = :ea, "
                    "  hidden_at = NULL "
                    "WHERE id = :id"
                ),
                {
                    "tv": target_value,
                    "cs": config_source,
                    "is_": interval_seconds,
                    "ts": timeout_seconds,
                    "ea": 1 if exec_authorized else 0,
                    "id": probe_id,
                },
            )
            return probe_id

        probe_id = uuid7()
        await conn.execute(
            text(
                "INSERT INTO probe_targets "
                "  (id, container_name, kind, name, target_value, config_source, "
                "   enabled, interval_seconds, timeout_seconds, exec_authorized, created_at) "
                "VALUES (:id, :cn, :k, :n, :tv, :cs, :en, :is_, :ts, :ea, :now)"
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
                "ea": 1 if exec_authorized else 0,
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

    @staticmethod
    async def update_probe_target_conn(
        conn: AsyncConnection,
        *,
        probe_id: str,
        target_value: str | None,
        interval_seconds: int | None,
        timeout_seconds: int | None,
    ) -> int:
        """Partial update: only patches non-None fields. Returns row count affected.

        kind, name, container_name are NOT updatable here (they form the logical
        UNIQUE key). Use upsert_probe_target_conn for full upserts.
        """
        sets: list[str] = []
        params: dict[str, object] = {"id": probe_id}
        if target_value is not None:
            sets.append("target_value = :tv")
            params["tv"] = target_value
        if interval_seconds is not None:
            sets.append("interval_seconds = :is_")
            params["is_"] = interval_seconds
        if timeout_seconds is not None:
            sets.append("timeout_seconds = :ts")
            params["ts"] = timeout_seconds
        if not sets:  # pragma: no cover -- API short-circuits empty-body PATCH before reaching repo
            # No-op: nothing to update. Still verify the row exists so callers
            # can rely on rowcount==1 for an existing id even when body is empty.
            result = await conn.execute(
                text("SELECT 1 FROM probe_targets WHERE id = :id"),
                {"id": probe_id},
            )
            return 1 if result.first() is not None else 0
        sql = "UPDATE probe_targets SET " + ", ".join(sets) + " WHERE id = :id"
        result = await conn.execute(text(sql), params)
        return result.rowcount or 0  # pragma: no cover -- rowcount defensive None fallback

    @staticmethod
    async def delete_probe_target_conn(
        conn: AsyncConnection,
        *,
        probe_id: str,
    ) -> int:
        """Physically delete a probe_targets row. Returns row count affected.

        NOTE: This is a hard delete — distinct from `mark_missing_except_conn`
        which soft-hides via hidden_at. The UI exposes Delete (hard) and
        Disable (soft via set_enabled_conn). See STAGE-003-012 Refinement
        scope expansion 2026-05-26.
        """
        result = await conn.execute(
            text("DELETE FROM probe_targets WHERE id = :id"),
            {"id": probe_id},
        )
        return result.rowcount or 0  # pragma: no cover -- rowcount defensive None fallback

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
            "  last_status, last_error, created_at, hidden_at, exec_authorized "
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
                    exec_authorized=bool(r.exec_authorized),
                )
            )
        return result

    async def get_by_id(self, probe_id: str) -> ProbeTargetRow | None:
        rows = await self._repo.fetch_all(
            text(
                "SELECT id, container_name, kind, name, target_value, config_source, "
                "  enabled, interval_seconds, timeout_seconds, last_run_at, "
                "  last_status, last_error, created_at, hidden_at, exec_authorized "
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
            exec_authorized=bool(r.exec_authorized),
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

    async def summarize_by_container(
        self,
        *,
        config_errors_by_container: dict[str, list[str]] | None = None,
    ) -> list[ProbeSummaryRow]:
        """One row per container_name that has at least one enabled, non-hidden probe.

        active = count of enabled probes; failing = count where last_status='fail' or 'error'.
        source_breakdown = per-config_source counts for the container's enabled probes.
        config_errors = validation errors for this container's override file, if any
          (sourced from `config_errors_by_container` passed by the API layer; the loader
          keeps the live error map separate from the DB).
        Containers with zero probes AND no config_errors are omitted.
        """
        rows = await self._repo.fetch_all(
            text(
                "SELECT container_name, config_source, "
                "  COUNT(*) AS cnt, "
                "  SUM(CASE WHEN last_status IN ('fail', 'error') THEN 1 ELSE 0 END) "
                "  AS failing_cnt "
                "FROM probe_targets "
                "WHERE enabled = 1 AND hidden_at IS NULL "
                "GROUP BY container_name, config_source "
                "ORDER BY container_name, config_source"
            )
        )
        by_container: dict[str, dict[str, int]] = {}
        failing_by_container: dict[str, int] = {}
        for r in rows:
            cn = str(r.container_name)
            cs = str(r.config_source)
            by_container.setdefault(cn, {})[cs] = int(r.cnt)
            failing_by_container[cn] = failing_by_container.get(cn, 0) + int(r.failing_cnt or 0)

        errors_map = config_errors_by_container or {}
        result_names: set[str] = set(by_container.keys()) | set(errors_map.keys())
        out: list[ProbeSummaryRow] = []
        for cn in sorted(result_names):
            breakdown = dict(sorted(by_container.get(cn, {}).items()))
            active_total = sum(breakdown.values())
            errors_for = errors_map.get(cn)
            out.append(
                ProbeSummaryRow(
                    container_name=cn,
                    active=active_total,
                    failing=failing_by_container.get(cn, 0),
                    source_breakdown=breakdown,
                    config_errors=errors_for if errors_for else None,
                )
            )
        return out


__all__ = [
    "ConfigSource",
    "ProbeKind",
    "ProbeStatus",
    "ProbeSummaryRow",
    "ProbeTargetRow",
    "ProbeTargetsRepository",
]
