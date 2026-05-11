"""Async write-side CRUD repository for the cron registry.

This module is the single source of truth for ``CronRecord`` (the hydrated
row dataclass) — ``HeartbeatRepo`` imports from here as of STAGE-002-002.

Audit-log discipline: every mutation writes a row in the SAME transaction
as the data change (atomicity). Verb taxonomy:

- ``crons.create`` — POST
- ``crons.update`` — PATCH that changed at least one non-archive field
- ``crons.delete`` — PATCH that set ``archived_at`` to a non-null value
- ``crons.restore`` — PATCH that set ``archived_at`` back to null

Empty-diff PATCH (no fields actually changed): NO audit row is written and
the existing record is returned unchanged.

xor invariant: every write recomputes ``schedule_canonical`` if ``schedule``
changed, and the repo enforces the schedule XOR cadence_seconds rule against
the merged row state (the migration's CHECK constraint is the second line of
defense).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncConnection

from homelab_monitor.kernel.cron.schedule import (
    canonicalize_schedule,
    compute_average_interval_seconds,
)
from homelab_monitor.kernel.cron.schemas import CronCreate, CronUpdate
from homelab_monitor.kernel.db.audit import insert_audit
from homelab_monitor.kernel.db.ids import uuid7
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.db.time import utc_now_iso

# ---------------------------------------------------------------------------
# Dataclasses (single source of truth — HeartbeatRepo imports CronRecord)
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class CronRecord:
    """Hydrated row from ``crons``. Includes ``schedule_canonical`` from 0007."""

    id: str
    name: str
    host: str
    command: str
    schedule: str
    schedule_canonical: str | None
    cadence_seconds: int
    expected_grace_seconds: int
    integration_mode: str  # observe | heartbeat | both
    enabled: bool
    last_seen_state: str
    created_at: str
    updated_at: str
    archived_at: str | None


@dataclass(slots=True, frozen=True)
class HeartbeatStateRecord:
    """Hydrated row from ``heartbeats_state``. Re-exported here so the cron
    module can return ``CronWithState`` without circular imports."""

    cron_id: str
    current_state: str
    last_start_at: str | None
    last_ok_at: str | None
    last_fail_at: str | None
    current_streak: int
    expected_next_at: str | None
    last_duration_seconds: float | None
    last_exit_code: int | None
    updated_at: str


@dataclass(slots=True, frozen=True)
class CronWithState:
    """Joined cron + heartbeat state (None state = no pings yet)."""

    cron: CronRecord
    state: HeartbeatStateRecord | None


@dataclass(slots=True, frozen=True)
class CronListPage:
    """Pagination envelope for list_crons."""

    items: list[CronRecord]
    total: int
    page: int
    page_size: int


# ---------------------------------------------------------------------------
# Row hydrators
# ---------------------------------------------------------------------------


def _row_to_cron(row: Row[Any]) -> CronRecord:
    return CronRecord(
        id=str(row.id),
        name=str(row.name),
        host=str(row.host),
        command=str(row.command),
        schedule=str(row.schedule),
        schedule_canonical=(
            None if row.schedule_canonical is None else str(row.schedule_canonical)
        ),
        cadence_seconds=int(row.cadence_seconds),
        expected_grace_seconds=int(row.expected_grace_seconds),
        integration_mode=str(row.integration_mode),
        enabled=bool(row.enabled),
        last_seen_state=str(row.last_seen_state),
        created_at=str(row.created_at),
        updated_at=str(row.updated_at),
        archived_at=None if row.archived_at is None else str(row.archived_at),
    )


def _row_to_state(row: Row[Any]) -> HeartbeatStateRecord:
    return HeartbeatStateRecord(
        cron_id=str(row.cron_id),
        current_state=str(row.current_state),
        last_start_at=None if row.last_start_at is None else str(row.last_start_at),
        last_ok_at=None if row.last_ok_at is None else str(row.last_ok_at),
        last_fail_at=None if row.last_fail_at is None else str(row.last_fail_at),
        current_streak=int(row.current_streak),
        expected_next_at=(None if row.expected_next_at is None else str(row.expected_next_at)),
        last_duration_seconds=(
            None if row.last_duration_seconds is None else float(row.last_duration_seconds)
        ),
        last_exit_code=None if row.last_exit_code is None else int(row.last_exit_code),
        updated_at=str(row.updated_at),
    )


# ---------------------------------------------------------------------------
# SQL constants
# ---------------------------------------------------------------------------

_CRON_COLS = (
    "id, name, host, command, schedule, schedule_canonical, cadence_seconds, "
    "expected_grace_seconds, integration_mode, enabled, last_seen_state, "
    "created_at, updated_at, archived_at"
)

_SELECT_BY_ID_SQL = text(f"SELECT {_CRON_COLS} FROM crons WHERE id = :id")

_SELECT_STATE_SQL = text(
    "SELECT cron_id, current_state, last_start_at, last_ok_at, last_fail_at, "
    "current_streak, expected_next_at, last_duration_seconds, last_exit_code, "
    "updated_at FROM heartbeats_state WHERE cron_id = :cron_id"
)

_INSERT_CRON_SQL = text(
    f"INSERT INTO crons ({_CRON_COLS}) VALUES ("
    "  :id, :name, :host, :command, :schedule, :schedule_canonical, :cadence_seconds, "
    "  :expected_grace_seconds, :integration_mode, :enabled, :last_seen_state, "
    "  :created_at, :updated_at, :archived_at"
    ")"
)

# ---------------------------------------------------------------------------
# CronRepo
# ---------------------------------------------------------------------------


class CronRepo:
    """Async write-side CRUD for the cron registry.

    Reads (``list_crons``, ``get_cron_with_state``) live here too even though
    the heartbeat receiver still has ``HeartbeatRepo.get_cron`` for its own
    code path — single-responsibility-wise the cron domain owns the cron data.
    """

    def __init__(self, db: SqliteRepository) -> None:
        self._db = db

    # ----- reads -----

    async def list_crons(  # noqa: PLR0913 -- query DSL has many filter knobs by design
        self,
        *,
        page: int,
        page_size: int,
        host: str | None,
        integration_mode: str | None,
        enabled: bool | None,
        state: str | None,
        q: str | None,
        include_archived: bool,
    ) -> CronListPage:
        """List crons with combinatorial filters + offset/limit pagination.

        Returns ordered by name ASC. ``q`` matches name OR command
        case-insensitively (LIKE with lower() coercion).
        """
        where_clauses: list[str] = []
        params: dict[str, Any] = {}

        if not include_archived:
            where_clauses.append("archived_at IS NULL")
        if host is not None:
            where_clauses.append("host = :host")
            params["host"] = host
        if integration_mode is not None:
            where_clauses.append("integration_mode = :integration_mode")
            params["integration_mode"] = integration_mode
        if enabled is not None:
            where_clauses.append("enabled = :enabled")
            params["enabled"] = 1 if enabled else 0
        if state is not None:
            where_clauses.append("last_seen_state = :state")
            params["state"] = state
        if q is not None and q.strip():
            where_clauses.append("(LOWER(name) LIKE :q OR LOWER(command) LIKE :q)")
            params["q"] = f"%{q.strip().lower()}%"

        where_sql = (" WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

        # COUNT
        count_sql = text(f"SELECT COUNT(*) AS c FROM crons{where_sql}")
        count_row = await self._db.fetch_one(count_sql, params)
        total = int(count_row.c) if count_row is not None else 0

        # PAGE
        offset = (page - 1) * page_size
        list_params = {**params, "limit": page_size, "offset": offset}
        list_sql = text(
            f"SELECT {_CRON_COLS} FROM crons{where_sql} "
            "ORDER BY name ASC, id ASC LIMIT :limit OFFSET :offset"
        )
        rows = await self._db.fetch_all(list_sql, list_params)
        items = [_row_to_cron(r) for r in rows]
        return CronListPage(items=items, total=total, page=page, page_size=page_size)

    async def get_cron_with_state(
        self, cron_id: str, *, include_archived: bool = False
    ) -> CronWithState | None:
        """Return the cron (joined with heartbeat state) or None if not found.

        When ``include_archived=False`` (the default), an archived row is
        treated as not-found. When ``True``, the row is returned regardless.
        """
        row = await self._db.fetch_one(_SELECT_BY_ID_SQL, {"id": cron_id})
        if row is None:
            return None
        cron = _row_to_cron(row)
        if not include_archived and cron.archived_at is not None:
            return None
        state_row = await self._db.fetch_one(_SELECT_STATE_SQL, {"cron_id": cron_id})
        state = None if state_row is None else _row_to_state(state_row)
        return CronWithState(cron=cron, state=state)

    async def get_cron(self, cron_id: str, *, include_archived: bool = False) -> CronRecord | None:
        """Return the bare cron record (no heartbeat join). Used by routers
        that need the cron only, e.g. ``GET /api/crons/{id}/preview-runs``."""
        row = await self._db.fetch_one(_SELECT_BY_ID_SQL, {"id": cron_id})
        if row is None:
            return None
        cron = _row_to_cron(row)
        if not include_archived and cron.archived_at is not None:
            return None
        return cron

    # ----- writes -----

    async def create_cron(self, payload: CronCreate, *, who: str, ip: str | None) -> CronRecord:
        """INSERT a new cron + audit ``crons.create`` in one transaction."""
        new_id = uuid7()
        now = utc_now_iso()
        schedule = payload.schedule or ""
        schedule_canonical = canonicalize_schedule(schedule) if schedule else None
        # When schedule is set, mirror it into cadence_seconds via the
        # average-interval helper so cadence is a fast-lookup mirror of the
        # cron expression (DB CHECK only forbids "neither set").
        cadence_seconds = (
            compute_average_interval_seconds(schedule) if schedule else payload.cadence_seconds
        )
        row_params: dict[str, Any] = {
            "id": new_id,
            "name": payload.name,
            "host": payload.host,
            "command": payload.command,
            "schedule": schedule,
            "schedule_canonical": schedule_canonical,
            "cadence_seconds": cadence_seconds,
            "expected_grace_seconds": payload.expected_grace_seconds,
            "integration_mode": payload.integration_mode,
            "enabled": 1 if payload.enabled else 0,
            "last_seen_state": "unknown",
            "created_at": now,
            "updated_at": now,
            "archived_at": None,
        }
        async with self._db.transaction() as conn:
            await conn.execute(_INSERT_CRON_SQL, row_params)
            await insert_audit(
                conn,
                who=who,
                what="crons.create",
                before=None,
                after=_audit_after_for_create(row_params),
                ip=ip,
                when=now,
            )
            row = (await conn.execute(_SELECT_BY_ID_SQL, {"id": new_id})).first()
            assert row is not None  # we just inserted it
            return _row_to_cron(row)

    async def update_cron(  # noqa: PLR0912, PLR0915
        self,
        cron_id: str,
        payload: CronUpdate,
        *,
        who: str,
        ip: str | None,
    ) -> CronRecord:
        """PATCH a cron with changed-fields-only semantics + audit verb routing.

        Verb routing:
        - if archived_at went from None -> non-None: ``crons.delete``
        - if archived_at went from non-None -> None: ``crons.restore``
        - else if any other field changed: ``crons.update``
        - else (empty diff): no audit row, returns existing record unchanged.

        Raises:
            LookupError: when ``cron_id`` does not exist.
            ValueError: when the merged row would violate the schedule-XOR-cadence
                invariant (also enforced at the DB CHECK level).
        """
        async with self._db.transaction() as conn:
            existing_row = (await conn.execute(_SELECT_BY_ID_SQL, {"id": cron_id})).first()
            if existing_row is None:
                msg = f"cron not found: {cron_id}"
                raise LookupError(msg)
            existing = _row_to_cron(existing_row)

            # Compute change set: only fields the client explicitly supplied
            # (model_fields_set) AND whose value differs from the current row.
            provided = payload.model_fields_set
            diff_before: dict[str, Any] = {}
            diff_after: dict[str, Any] = {}
            updates: dict[str, Any] = {}

            field_map: dict[str, Any] = {
                "name": existing.name,
                "host": existing.host,
                "command": existing.command,
                "schedule": existing.schedule,
                "cadence_seconds": existing.cadence_seconds,
                "expected_grace_seconds": existing.expected_grace_seconds,
                "integration_mode": existing.integration_mode,
                "enabled": existing.enabled,
                "archived_at": existing.archived_at,
            }

            for field in (
                "name",
                "host",
                "command",
                "schedule",
                "cadence_seconds",
                "expected_grace_seconds",
                "integration_mode",
                "enabled",
                "archived_at",
            ):
                if field not in provided:
                    continue
                new_val = getattr(payload, field)
                # Normalize: payload.schedule None means "set to empty"; the
                # column is NOT NULL with default ''.
                if field == "schedule" and new_val is None:
                    new_val = ""
                old_val = field_map[field]
                if field == "enabled":
                    # Compare booleans (existing is bool; payload is bool|None).
                    if bool(new_val) == bool(old_val):
                        continue
                    diff_before[field] = bool(old_val)
                    diff_after[field] = bool(new_val)
                    updates[field] = 1 if bool(new_val) else 0
                else:
                    if new_val == old_val:
                        continue
                    diff_before[field] = old_val
                    diff_after[field] = new_val
                    updates[field] = new_val

            if not updates:
                # Empty diff: return existing unchanged, no audit row.
                return existing

            # Recompute canonical if schedule changed.
            if "schedule" in updates:
                sched = updates["schedule"]
                updates["schedule_canonical"] = canonicalize_schedule(sched) if sched else None
                diff_after["schedule_canonical"] = updates["schedule_canonical"]
                diff_before["schedule_canonical"] = existing.schedule_canonical
                # Mirror cadence_seconds from the new schedule (unless the
                # client also explicitly set cadence_seconds in this PATCH).
                if sched and "cadence_seconds" not in updates:
                    new_cadence = compute_average_interval_seconds(sched)
                    if new_cadence != existing.cadence_seconds:
                        updates["cadence_seconds"] = new_cadence
                        diff_before["cadence_seconds"] = existing.cadence_seconds
                        diff_after["cadence_seconds"] = new_cadence
                elif not sched and "cadence_seconds" not in updates:
                    # schedule cleared and cadence not explicitly set — zero
                    # the mirror so the row stays at "cadence-driven" semantics.
                    if existing.cadence_seconds != 0:
                        updates["cadence_seconds"] = 0
                        diff_before["cadence_seconds"] = existing.cadence_seconds
                        diff_after["cadence_seconds"] = 0

            # Validate "at-least-one" against merged state (DB CHECK mirror).
            merged_schedule = updates.get("schedule", existing.schedule)
            merged_cadence = updates.get("cadence_seconds", existing.cadence_seconds)
            has_schedule = isinstance(merged_schedule, str) and merged_schedule != ""
            has_cadence = int(merged_cadence) > 0
            if not has_schedule and not has_cadence:
                msg = "merged row violates: neither schedule nor cadence_seconds set"
                raise ValueError(msg)

            # Determine audit verb.
            archive_changed = "archived_at" in updates
            went_to_archived = (
                archive_changed
                and updates["archived_at"] is not None
                and existing.archived_at is None
            )
            went_to_active = (
                archive_changed
                and updates["archived_at"] is None
                and existing.archived_at is not None
            )
            other_fields_changed = any(
                f in updates for f in updates if f not in {"archived_at", "schedule_canonical"}
            )
            # NOTE: schedule_canonical alone is bookkeeping; the verb decision
            # tracks user-meaningful field changes only.

            if went_to_archived and not other_fields_changed:
                verb = "crons.delete"
            elif went_to_active and not other_fields_changed:
                verb = "crons.restore"
            elif went_to_archived or went_to_active:
                # Mixed: archive flag flipped AND other fields changed in the
                # same PATCH. We follow D2/D3: emit the archive-side verb so
                # operators see the audit trail of the destructive action; the
                # other field changes are recorded in the same row's after JSON.
                verb = "crons.delete" if went_to_archived else "crons.restore"
            else:
                verb = "crons.update"

            now = utc_now_iso()
            updates["updated_at"] = now
            diff_before["updated_at"] = existing.updated_at
            diff_after["updated_at"] = now

            # Build dynamic UPDATE.
            set_clause = ", ".join(f"{col} = :{col}" for col in updates)
            update_sql = text(f"UPDATE crons SET {set_clause} WHERE id = :id")
            update_params = {**updates, "id": cron_id}
            await conn.execute(update_sql, update_params)

            await insert_audit(
                conn,
                who=who,
                what=verb,
                before=diff_before,
                after=diff_after,
                ip=ip,
                when=now,
            )

            row = (await conn.execute(_SELECT_BY_ID_SQL, {"id": cron_id})).first()
            assert row is not None
            return _row_to_cron(row)

    async def soft_delete_cron(self, cron_id: str, *, who: str, ip: str | None) -> CronRecord:
        """Soft-delete (archive) a cron. 404 semantics: also raises LookupError
        if the cron is already archived (clear semantics — no double-delete)."""
        existing = await self.get_cron(cron_id, include_archived=True)
        if existing is None:
            msg = f"cron not found: {cron_id}"
            raise LookupError(msg)
        if existing.archived_at is not None:
            msg = f"cron already archived: {cron_id}"
            raise LookupError(msg)
        # Reuse update_cron so the verb routing logic stays in one place.
        payload = CronUpdate.model_validate({"archived_at": utc_now_iso()})
        return await self.update_cron(cron_id, payload, who=who, ip=ip)


def _audit_after_for_create(row_params: Mapping[str, Any]) -> dict[str, Any]:
    """Project the INSERT params into an audit-friendly ``after`` dict.

    Drops the ``id`` (already discoverable from the audit_log row's foreign
    context) and serializes ``enabled`` as a bool for human-readable trails.
    """
    return {
        "name": row_params["name"],
        "host": row_params["host"],
        "command": row_params["command"],
        "schedule": row_params["schedule"],
        "schedule_canonical": row_params["schedule_canonical"],
        "cadence_seconds": row_params["cadence_seconds"],
        "expected_grace_seconds": row_params["expected_grace_seconds"],
        "integration_mode": row_params["integration_mode"],
        "enabled": bool(row_params["enabled"]),
        "last_seen_state": row_params["last_seen_state"],
        "created_at": row_params["created_at"],
    }


# Re-export AsyncConnection so static-analyzer-driven imports stay tidy.
__all__ = [
    "AsyncConnection",
    "CronListPage",
    "CronRecord",
    "CronRepo",
    "CronWithState",
    "HeartbeatStateRecord",
]
