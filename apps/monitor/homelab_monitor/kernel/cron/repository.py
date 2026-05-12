"""Async write-side CRUD repository for the cron registry.

This module is the single source of truth for ``CronRecord`` (the hydrated
row dataclass) — ``HeartbeatRepo`` imports from here.

Audit-log discipline: every mutation writes a row in the SAME transaction
as the data change (atomicity). Verb taxonomy:

- ``crons.create`` — POST /api/crons (deprecated; removed in STAGE-002-004)
- ``crons.update`` — PATCH that changed at least one non-hidden_at field
- ``crons.hide`` — PATCH that set ``hidden_at`` to a non-null value, or DELETE
- ``crons.unhide`` — PATCH that set ``hidden_at`` back to null

Empty-diff PATCH (no fields actually changed): NO audit row is written and
the existing record is returned unchanged.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncConnection

from homelab_monitor.kernel.cron.fingerprint import compute_fingerprint
from homelab_monitor.kernel.cron.schedule import (
    canonicalize_schedule,
    compute_average_interval_seconds,
)
from homelab_monitor.kernel.cron.schemas import CronCreate, CronUpdate
from homelab_monitor.kernel.db.audit import insert_audit
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.db.time import utc_now_iso

# ---------------------------------------------------------------------------
# Dataclasses (single source of truth — HeartbeatRepo imports CronRecord)
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class CronRecord:
    """Hydrated row from ``crons`` (fingerprint-keyed)."""

    fingerprint: str
    name: str
    host: str
    command: str
    schedule: str
    schedule_canonical: str | None
    cadence_seconds: int
    expected_grace_seconds: int
    enabled: bool
    last_seen_state: str
    created_at: str
    updated_at: str
    hidden_at: str | None
    source_path: str | None
    wrapper_installed_at: str | None


@dataclass(slots=True, frozen=True)
class HeartbeatStateRecord:
    """Hydrated row from ``heartbeats_state``. Re-exported here so the cron
    module can return ``CronWithState`` without circular imports."""

    cron_fingerprint: str
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
        fingerprint=str(row.fingerprint),
        name=str(row.name),
        host=str(row.host),
        command=str(row.command),
        schedule="" if row.schedule is None else str(row.schedule),
        schedule_canonical=(
            None if row.schedule_canonical is None else str(row.schedule_canonical)
        ),
        cadence_seconds=int(row.cadence_seconds),
        expected_grace_seconds=int(row.expected_grace_seconds),
        enabled=bool(row.enabled),
        last_seen_state=str(row.last_seen_state),
        created_at=str(row.created_at),
        updated_at=str(row.updated_at),
        hidden_at=None if row.hidden_at is None else str(row.hidden_at),
        source_path=None if row.source_path is None else str(row.source_path),
        wrapper_installed_at=(
            None if row.wrapper_installed_at is None else str(row.wrapper_installed_at)
        ),
    )


def _row_to_state(row: Row[Any]) -> HeartbeatStateRecord:
    return HeartbeatStateRecord(
        cron_fingerprint=str(row.cron_fingerprint),
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
    "fingerprint, name, host, command, schedule, schedule_canonical, "
    "cadence_seconds, expected_grace_seconds, enabled, last_seen_state, "
    "created_at, updated_at, hidden_at, source_path, wrapper_installed_at"
)

_SELECT_BY_FINGERPRINT_SQL = text(
    f"SELECT {_CRON_COLS} FROM crons WHERE fingerprint = :fingerprint"
)

_SELECT_STATE_SQL = text(
    "SELECT cron_fingerprint, current_state, last_start_at, last_ok_at, "
    "last_fail_at, current_streak, expected_next_at, last_duration_seconds, "
    "last_exit_code, updated_at FROM heartbeats_state "
    "WHERE cron_fingerprint = :cron_fingerprint"
)

_INSERT_CRON_SQL = text(
    f"INSERT INTO crons ({_CRON_COLS}) VALUES ("
    "  :fingerprint, :name, :host, :command, :schedule, :schedule_canonical, "
    "  :cadence_seconds, :expected_grace_seconds, :enabled, :last_seen_state, "
    "  :created_at, :updated_at, :hidden_at, :source_path, :wrapper_installed_at"
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

    async def list_crons(  # noqa: PLR0913
        self,
        *,
        page: int,
        page_size: int,
        host: str | None,
        enabled: bool | None,
        state: str | None,
        q: str | None,
        include_hidden: bool,
    ) -> CronListPage:
        """List crons with combinatorial filters + offset/limit pagination.

        Returns ordered by name ASC. ``q`` matches name OR command
        case-insensitively (LIKE with lower() coercion).
        """
        where_clauses: list[str] = []
        params: dict[str, Any] = {}

        if not include_hidden:
            where_clauses.append("hidden_at IS NULL")
        if host is not None:
            where_clauses.append("host = :host")
            params["host"] = host
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
            "ORDER BY name ASC, fingerprint ASC LIMIT :limit OFFSET :offset"
        )
        rows = await self._db.fetch_all(list_sql, list_params)
        items = [_row_to_cron(r) for r in rows]
        return CronListPage(items=items, total=total, page=page, page_size=page_size)

    async def get_cron_with_state(
        self, fingerprint: str, *, include_hidden: bool = False
    ) -> CronWithState | None:
        """Return the cron (joined with heartbeat state) or None if not found.

        When ``include_hidden=False`` (the default), a hidden row is
        treated as not-found. When ``True``, the row is returned regardless.
        """
        row = await self._db.fetch_one(_SELECT_BY_FINGERPRINT_SQL, {"fingerprint": fingerprint})
        if row is None:
            return None
        cron = _row_to_cron(row)
        if not include_hidden and cron.hidden_at is not None:
            return None
        state_row = await self._db.fetch_one(_SELECT_STATE_SQL, {"cron_fingerprint": fingerprint})
        state = None if state_row is None else _row_to_state(state_row)
        return CronWithState(cron=cron, state=state)

    async def get_cron(
        self, fingerprint: str, *, include_hidden: bool = False
    ) -> CronRecord | None:
        """Return the bare cron record (no heartbeat join). Used by routers
        that need the cron only, e.g. ``GET /api/crons/{fingerprint}/preview-runs``."""
        row = await self._db.fetch_one(_SELECT_BY_FINGERPRINT_SQL, {"fingerprint": fingerprint})
        if row is None:
            return None
        cron = _row_to_cron(row)
        if not include_hidden and cron.hidden_at is not None:
            return None
        return cron

    # ----- writes -----

    async def create_cron(self, payload: CronCreate, *, who: str, ip: str | None) -> CronRecord:
        """INSERT a new cron + audit ``crons.create`` in one transaction.

        DEPRECATED: STAGE-002-004 removes ``POST /api/crons`` entirely. This method
        survives through STAGE-002-003 so existing test fixtures continue to work
        during the transition. Discovery (STAGE-002-007) and ``/register``
        (STAGE-002-005) are the going-forward creation paths.
        """
        now = utc_now_iso()
        schedule = payload.schedule or ""
        schedule_canonical = canonicalize_schedule(schedule) if schedule else None
        cadence_seconds = (
            compute_average_interval_seconds(schedule) if schedule else payload.cadence_seconds
        )
        fingerprint = compute_fingerprint(
            host=payload.host,
            source_path=payload.source_path,
            schedule=schedule,
            command=payload.command,
        )
        row_params: dict[str, Any] = {
            "fingerprint": fingerprint,
            "name": payload.name,
            "host": payload.host,
            "command": payload.command,
            "schedule": schedule if schedule else None,
            "schedule_canonical": schedule_canonical,
            "cadence_seconds": cadence_seconds,
            "expected_grace_seconds": payload.expected_grace_seconds,
            "enabled": 1 if payload.enabled else 0,
            "last_seen_state": "unknown",
            "created_at": now,
            "updated_at": now,
            "hidden_at": None,
            "source_path": payload.source_path,
            "wrapper_installed_at": None,
        }
        async with self._db.transaction() as conn:
            existing = (
                await conn.execute(_SELECT_BY_FINGERPRINT_SQL, {"fingerprint": fingerprint})
            ).first()
            if existing is not None:
                msg = f"cron already exists: {fingerprint}"
                raise ValueError(msg)
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
            row = (
                await conn.execute(_SELECT_BY_FINGERPRINT_SQL, {"fingerprint": fingerprint})
            ).first()
            assert row is not None
            return _row_to_cron(row)

    async def update_cron(  # noqa: PLR0915
        self,
        fingerprint: str,
        payload: CronUpdate,
        *,
        who: str,
        ip: str | None,
    ) -> CronRecord:
        """PATCH a cron with the trimmed editable-fields whitelist + audit verb routing.

        Editable fields: ``name``, ``expected_grace_seconds``, ``enabled``,
        ``hidden_at``. All others are rejected by Pydantic ``extra='forbid'``
        before this method is called.

        Verb routing:
        - hidden_at None → non-None: ``crons.hide``
        - hidden_at non-None → None: ``crons.unhide``
        - other-field change: ``crons.update``
        - empty diff: no audit row, return existing record unchanged.
        """
        async with self._db.transaction() as conn:
            existing_row = (
                await conn.execute(_SELECT_BY_FINGERPRINT_SQL, {"fingerprint": fingerprint})
            ).first()
            if existing_row is None:
                msg = f"cron not found: {fingerprint}"
                raise LookupError(msg)
            existing = _row_to_cron(existing_row)

            provided = payload.model_fields_set
            diff_before: dict[str, Any] = {}
            diff_after: dict[str, Any] = {}
            updates: dict[str, Any] = {}

            field_map: dict[str, Any] = {
                "name": existing.name,
                "expected_grace_seconds": existing.expected_grace_seconds,
                "enabled": existing.enabled,
                "hidden_at": existing.hidden_at,
            }

            for field in ("name", "expected_grace_seconds", "enabled", "hidden_at"):
                if field not in provided:
                    continue
                new_val = getattr(payload, field)
                old_val = field_map[field]
                if field == "enabled":
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
                return existing

            hidden_changed = "hidden_at" in updates
            went_to_hidden = (
                hidden_changed and updates["hidden_at"] is not None and existing.hidden_at is None
            )
            went_to_visible = (
                hidden_changed and updates["hidden_at"] is None and existing.hidden_at is not None
            )
            other_fields_changed = any(f != "hidden_at" for f in updates)

            if went_to_hidden and not other_fields_changed:
                verb = "crons.hide"
            elif went_to_visible and not other_fields_changed:
                verb = "crons.unhide"
            elif went_to_hidden or went_to_visible:
                verb = "crons.hide" if went_to_hidden else "crons.unhide"
            else:
                verb = "crons.update"

            now = utc_now_iso()
            updates["updated_at"] = now
            diff_before["updated_at"] = existing.updated_at
            diff_after["updated_at"] = now

            set_clause = ", ".join(f"{col} = :{col}" for col in updates)
            update_sql = text(f"UPDATE crons SET {set_clause} WHERE fingerprint = :fingerprint")
            update_params = {**updates, "fingerprint": fingerprint}
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

            row = (
                await conn.execute(_SELECT_BY_FINGERPRINT_SQL, {"fingerprint": fingerprint})
            ).first()
            assert row is not None
            return _row_to_cron(row)

    async def soft_delete_cron(self, fingerprint: str, *, who: str, ip: str | None) -> CronRecord:
        """Soft-delete (hide) a cron. 404 if missing OR already hidden."""
        existing = await self.get_cron(fingerprint, include_hidden=True)
        if existing is None:
            msg = f"cron not found: {fingerprint}"
            raise LookupError(msg)
        if existing.hidden_at is not None:
            msg = f"cron already hidden: {fingerprint}"
            raise LookupError(msg)
        payload = CronUpdate.model_validate({"hidden_at": utc_now_iso()})
        return await self.update_cron(fingerprint, payload, who=who, ip=ip)


def _audit_after_for_create(row_params: Mapping[str, Any]) -> dict[str, Any]:
    """Project the INSERT params into an audit-friendly ``after`` dict."""
    return {
        "fingerprint": row_params["fingerprint"],
        "name": row_params["name"],
        "host": row_params["host"],
        "command": row_params["command"],
        "schedule": row_params["schedule"],
        "schedule_canonical": row_params["schedule_canonical"],
        "cadence_seconds": row_params["cadence_seconds"],
        "expected_grace_seconds": row_params["expected_grace_seconds"],
        "enabled": bool(row_params["enabled"]),
        "last_seen_state": row_params["last_seen_state"],
        "created_at": row_params["created_at"],
        "source_path": row_params["source_path"],
        "wrapper_installed_at": row_params["wrapper_installed_at"],
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
