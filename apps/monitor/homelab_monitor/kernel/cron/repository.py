"""Async write-side CRUD repository for the cron registry.

This module is the single source of truth for ``CronRecord`` (the hydrated
row dataclass) — ``HeartbeatRepo`` imports from here.

Audit-log discipline: every mutation writes a row in the SAME transaction
as the data change (atomicity). Verb taxonomy:

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

from homelab_monitor.kernel.cron.fingerprint import derive_name
from homelab_monitor.kernel.cron.log_match import canonical_log_key
from homelab_monitor.kernel.cron.schedule import (
    canonicalize_schedule,
    compute_average_interval_seconds,
)
from homelab_monitor.kernel.cron.schemas import CronUpdate, RegisterCronBody
from homelab_monitor.kernel.db.audit import insert_audit
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.db.time import utc_now_iso

# ---------------------------------------------------------------------------
# Module-private helpers
# ---------------------------------------------------------------------------


def _log_match_key_or_none(command: str) -> str | None:
    """Return the canonical log-match key, or ``None`` if it canonicalizes empty.

    Stored as NULL rather than "" so empty-key rows never collide on the
    ``(host, log_match_key)`` equality join used by ``match_by_log_key``.
    """
    key = canonical_log_key(command)
    return key if key.strip() else None


def _cron_to_audit_after(record: CronRecord) -> dict[str, Any]:
    """Convert a CronRecord to a JSON-safe dict for audit log 'after' field.

    Serializes all CronRecord fields to JSON-compatible Python values.
    """
    return {
        "fingerprint": record.fingerprint,
        "name": record.name,
        "host": record.host,
        "command": record.command,
        "schedule": record.schedule,
        "schedule_canonical": record.schedule_canonical,
        "cadence_seconds": record.cadence_seconds,
        "expected_grace_seconds": record.expected_grace_seconds,
        "enabled": record.enabled,
        "last_seen_state": record.last_seen_state,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "hidden_at": record.hidden_at,
        "source_path": record.source_path,
        "wrapper_last_seen_at": record.wrapper_last_seen_at,
        "last_discovered_at": record.last_discovered_at,
        "soft_deleted_at": record.soft_deleted_at,
        "log_match_key": record.log_match_key,
    }


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
    wrapper_last_seen_at: str | None
    last_discovered_at: str | None
    soft_deleted_at: str | None
    log_match_key: str | None


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
    observed_runs_total: int
    last_observed_run_at: str | None


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
        wrapper_last_seen_at=(
            None if row.wrapper_last_seen_at is None else str(row.wrapper_last_seen_at)
        ),
        last_discovered_at=(
            None if row.last_discovered_at is None else str(row.last_discovered_at)
        ),
        soft_deleted_at=(None if row.soft_deleted_at is None else str(row.soft_deleted_at)),
        log_match_key=(None if row.log_match_key is None else str(row.log_match_key)),
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
        observed_runs_total=int(row.observed_runs_total),
        last_observed_run_at=(
            None if row.last_observed_run_at is None else str(row.last_observed_run_at)
        ),
    )


# ---------------------------------------------------------------------------
# SQL constants
# ---------------------------------------------------------------------------

_CRON_COLS = (
    "fingerprint, name, host, command, schedule, schedule_canonical, "
    "cadence_seconds, expected_grace_seconds, enabled, last_seen_state, "
    "created_at, updated_at, hidden_at, source_path, wrapper_last_seen_at, "
    "last_discovered_at, soft_deleted_at, log_match_key"
)

_SELECT_BY_FINGERPRINT_SQL = text(
    f"SELECT {_CRON_COLS} FROM crons WHERE fingerprint = :fingerprint"
)

_INSERT_CRON_SQL = text(
    "INSERT INTO crons ("
    "  fingerprint, name, host, command, schedule, schedule_canonical, "
    "  cadence_seconds, expected_grace_seconds, enabled, last_seen_state, "
    "  created_at, updated_at, hidden_at, source_path, wrapper_last_seen_at, "
    "  last_discovered_at, soft_deleted_at, log_match_key"
    ") VALUES ("
    "  :fingerprint, :name, :host, :command, :schedule, :schedule_canonical, "
    "  :cadence_seconds, :expected_grace_seconds, :enabled, :last_seen_state, "
    "  :created_at, :updated_at, :hidden_at, :source_path, :wrapper_last_seen_at, "
    "  :last_discovered_at, :soft_deleted_at, :log_match_key"
    ")"
)

_UPDATE_WRAPPER_LAST_SEEN_SQL = text(
    "UPDATE crons SET wrapper_last_seen_at = :wlsa, updated_at = :now "
    "WHERE fingerprint = :fingerprint"
)

_SELECT_STATE_SQL = text(
    "SELECT cron_fingerprint, current_state, last_start_at, last_ok_at, "
    "last_fail_at, current_streak, expected_next_at, last_duration_seconds, "
    "last_exit_code, updated_at, observed_runs_total, last_observed_run_at FROM heartbeats_state "
    "WHERE cron_fingerprint = :cron_fingerprint"
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
        include_soft_deleted: bool = False,
        wrapper_installed: bool | None = None,
    ) -> CronListPage:
        """List crons with combinatorial filters + offset/limit pagination.

        Returns ordered by name ASC. ``q`` matches name OR command
        case-insensitively (LIKE with lower() coercion).
        """
        where_clauses: list[str] = []
        params: dict[str, Any] = {}

        if not include_hidden:
            where_clauses.append("hidden_at IS NULL")
        if not include_soft_deleted:
            where_clauses.append("soft_deleted_at IS NULL")
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
        if wrapper_installed is not None:
            if wrapper_installed:
                where_clauses.append("wrapper_last_seen_at IS NOT NULL")
            else:
                where_clauses.append("wrapper_last_seen_at IS NULL")

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

        Soft-deleted rows (``soft_deleted_at IS NOT NULL``) are ALWAYS
        returned by this method — direct fetch is unfiltered for soft-delete
        (STAGE-002-007A). Only ``hidden_at`` gates this method.
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
        that need the cron only, e.g. ``GET /api/crons/{fingerprint}/preview-runs``.

        Soft-deleted rows (``soft_deleted_at IS NOT NULL``) are ALWAYS
        returned by this method — direct fetch is unfiltered for soft-delete
        (STAGE-002-007A). Only ``hidden_at`` gates this method.
        """
        row = await self._db.fetch_one(_SELECT_BY_FINGERPRINT_SQL, {"fingerprint": fingerprint})
        if row is None:
            return None
        cron = _row_to_cron(row)
        if not include_hidden and cron.hidden_at is not None:
            return None
        return cron

    async def match_by_log_key(self, host: str, log_match_key: str) -> list[CronRecord]:
        """Return all active (non-soft-deleted) crons matching (host, log_match_key).

        Used by the cron-events ingest endpoint to resolve a log event to a
        fingerprint. Hidden crons ARE included (hidden = display suppression,
        not data-capture suppression — see cron-identity.md). Soft-deleted rows
        are EXCLUDED: a cron deleted from disk should not absorb log evidence.

        An empty / whitespace-only ``log_match_key`` matches NOTHING: a command
        canonicalizing to "" cannot identify a cron, and an equality join would
        otherwise match every empty-key row at once (spurious AMBIGUOUS). Such
        keys are never stored — discovery writes NULL for an empty canonical key
        — so this guard is defence-in-depth for legacy / hand-written rows.
        """
        if not log_match_key.strip():
            return []
        rows = await self._db.fetch_all(
            text(
                f"SELECT {_CRON_COLS} FROM crons "
                "WHERE host = :host AND log_match_key = :key "
                "AND soft_deleted_at IS NULL"
            ),
            {"host": host, "key": log_match_key},
        )
        return [_row_to_cron(r) for r in rows]

    async def try_claim_cursor(self, journal_cursor: str, now: str) -> bool:
        """Attempt to claim a journald cursor. Returns True if newly claimed,
        False if it was already processed (replay).

        Uses INSERT OR IGNORE on cron_log_cursors(journal_cursor PRIMARY KEY).
        Single-host single-writer: no compound key needed.

        At-most-once delivery (KNOWN LIMITATION): the cursor is committed in
        THIS transaction, while the subsequent ``record_observed_run`` /
        ``record_ok`` / ``record_fail`` state write runs in its OWN separate
        transaction (see ``cron_events._process_one``). A process crash in the
        window between the two commits leaves the cursor claimed but the run
        unrecorded; the re-POSTed event is then treated as a replay and the run
        is permanently dropped. This is an accepted trade-off: cron observed-run
        evidence is advisory (it never gates alerting decisions on its own), the
        window is sub-millisecond on a single-writer SQLite, and threading one
        connection through the ``HeartbeatRepo`` mutators would require changing
        their public signatures and every heartbeat-receiver caller. Documented
        in docs/architecture/cron-logscrape.md.
        """
        async with self._db.transaction() as conn:
            result = await conn.execute(
                text(
                    "INSERT OR IGNORE INTO cron_log_cursors "
                    "(journal_cursor, processed_at) VALUES (:c, :now)"
                ),
                {"c": journal_cursor, "now": now},
            )
            return result.rowcount == 1

    # ----- writes -----

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
            # TODO: [STAGE-002-005 review] Combined-flip branch (hide+rename OR unhide+rename)
            # has only the hide+rename case covered in test_cron_repo.py. Add explicit test for
            # unhide+rename verb routing in a future hardening stage. Pre-existing pattern;
            # behavior is correct, just lacks the parallel test.
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

            # Backfill fingerprint into audit payload for future audit
            # surfaces (STAGE-002-007A and later).
            diff_before["fingerprint"] = fingerprint
            diff_after["fingerprint"] = fingerprint

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

    async def register_cron(
        self,
        payload: RegisterCronBody,
        *,
        url_fingerprint: str,
        who: str,
        ip: str | None,
    ) -> tuple[CronRecord, bool]:
        """Idempotent upsert for ``POST /api/hb/{fingerprint}/register``.

        Returns ``(record, created)`` where ``created`` is True on first insert
        (-> 201) and False when the row already existed (-> 200).

        Validation responsibilities (raised before this method by the router):
        - URL fingerprint == server-recomputed fingerprint from body fields
          (router raises 422 with ``fingerprint_mismatch`` flag)
        - ``payload.schedule`` is a valid cron expression (router raises 422
          with ``invalid_schedule`` flag)

        Locked behavior (STAGE-002-005 D7 + D10):
        - First insert: set name/expected_grace_seconds/enabled defaults; set
          ``wrapper_last_seen_at`` to now if ``payload.wrapper`` else NULL.
          Audit row written with ``before=None, after=<full row>``.
        - Re-register with ``payload.wrapper=False``: NO state change, NO audit
          row, return existing record.
        - Re-register with ``payload.wrapper=True``: SET ``wrapper_last_seen_at``
          to now (regardless of prior value — NULL or older ts). Audit row
          written with ``before={wrapper_last_seen_at: <prev>}``,
          ``after={wrapper_last_seen_at: <new>}``.
        - Re-register NEVER touches ``name``, ``expected_grace_seconds``,
          ``enabled``, ``hidden_at``, or the identity fields
          (host/source_path/schedule/command — they're already pinned by the
          fingerprint).
        """
        now = utc_now_iso()
        schedule_canonical = canonicalize_schedule(payload.schedule)
        cadence_seconds = compute_average_interval_seconds(payload.schedule)

        async with self._db.transaction() as conn:
            existing_row = (
                await conn.execute(
                    _SELECT_BY_FINGERPRINT_SQL,
                    {"fingerprint": url_fingerprint},
                )
            ).first()

            if existing_row is None:
                # ---- 201 path: create ----
                new_record_params: dict[str, Any] = {
                    "fingerprint": url_fingerprint,
                    "name": derive_name(payload.command),
                    "host": payload.host,
                    "command": payload.command,
                    "schedule": payload.schedule,
                    "schedule_canonical": schedule_canonical,
                    "cadence_seconds": cadence_seconds,
                    "expected_grace_seconds": 300,
                    "enabled": 1,
                    "last_seen_state": "unknown",
                    "created_at": now,
                    "updated_at": now,
                    "hidden_at": None,
                    "source_path": payload.source_path,
                    "wrapper_last_seen_at": now if payload.wrapper else None,
                    "last_discovered_at": None,
                    "soft_deleted_at": None,
                    "log_match_key": _log_match_key_or_none(payload.command),
                }
                await conn.execute(_INSERT_CRON_SQL, new_record_params)

                # Hydrate the row we just wrote (so audit `after` carries the
                # canonical projection).
                inserted_row = (
                    await conn.execute(
                        _SELECT_BY_FINGERPRINT_SQL,
                        {"fingerprint": url_fingerprint},
                    )
                ).first()
                assert inserted_row is not None
                inserted = _row_to_cron(inserted_row)

                await insert_audit(
                    conn,
                    who=who,
                    what="crons.register",
                    before=None,
                    after=_cron_to_audit_after(inserted),
                    ip=ip,
                    when=now,
                )
                return inserted, True

            # ---- 200 path: exists ----
            existing = _row_to_cron(existing_row)

            # D5: if this fingerprint was auto-soft-deleted, /register restores
            # it. Write the crons.restore audit row FIRST (before any
            # crons.register row), in this same transaction.
            was_soft_deleted = existing.soft_deleted_at is not None
            if was_soft_deleted:
                await conn.execute(
                    text(
                        "UPDATE crons SET soft_deleted_at = NULL, updated_at = :now "
                        "WHERE fingerprint = :fingerprint"
                    ),
                    {"now": now, "fingerprint": url_fingerprint},
                )
                await insert_audit(
                    conn,
                    who=who,
                    what="crons.restore",
                    before={
                        "fingerprint": url_fingerprint,
                        "soft_deleted_at": existing.soft_deleted_at,
                    },
                    after={"fingerprint": url_fingerprint, "soft_deleted_at": None},
                    ip=ip,
                    when=now,
                )

            if not payload.wrapper:
                # No wrapper refresh. If we restored above, re-hydrate and
                # return the updated row; otherwise true no-op.
                if was_soft_deleted:
                    restored_row = (
                        await conn.execute(
                            _SELECT_BY_FINGERPRINT_SQL,
                            {"fingerprint": url_fingerprint},
                        )
                    ).first()
                    assert restored_row is not None
                    return _row_to_cron(restored_row), False
                return existing, False

            # wrapper=True on existing row → refresh wrapper_last_seen_at.
            prev_ts = existing.wrapper_last_seen_at
            await conn.execute(
                _UPDATE_WRAPPER_LAST_SEEN_SQL,
                {"wlsa": now, "now": now, "fingerprint": url_fingerprint},
            )
            await insert_audit(
                conn,
                who=who,
                what="crons.register",
                before={"fingerprint": url_fingerprint, "wrapper_last_seen_at": prev_ts},
                after={"fingerprint": url_fingerprint, "wrapper_last_seen_at": now},
                ip=ip,
                when=now,
            )

            refreshed_row = (
                await conn.execute(
                    _SELECT_BY_FINGERPRINT_SQL,
                    {"fingerprint": url_fingerprint},
                )
            ).first()
            assert refreshed_row is not None
            return _row_to_cron(refreshed_row), False

    async def record_wrapper_installed(
        self,
        fingerprint: str,
        *,
        who: str,
        ip: str | None,
    ) -> None:
        """Write a crons.wrapper_installed audit row.

        Used by the install endpoint after a successful wrapper install.
        Does NOT set wrapper_last_seen_at — that column is the
        wrapper-HEALTH signal and is populated only by a real heartbeat
        (or a /register with wrapper=True). Setting it at install time
        would falsely report a healthy wrapper before it has run once.
        The audit row is the durable record that an install occurred.
        """
        from homelab_monitor.kernel.cron.wrapper_constants import WRAPPER_PATH  # noqa: PLC0415

        now = utc_now_iso()
        async with self._db.transaction() as conn:
            await insert_audit(
                conn,
                who=who,
                what="crons.wrapper_installed",
                before=None,
                after={
                    "fingerprint": fingerprint,
                    "wrapper_path": WRAPPER_PATH,
                },
                ip=ip,
                when=now,
            )

    async def upsert_discovered(  # noqa: PLR0915
        self,
        *,
        host: str,
        source_path: str,
        schedule: str,
        command: str,
        now: str,
    ) -> tuple[CronRecord, bool, bool]:
        """Upsert a discovered cron row.

        Returns (record, inserted, updated_non_bump):
        - inserted: True iff a new row was created (audit verb `crons.discover` written)
        - updated_non_bump: True iff an existing row had a non-bump field change
          (audit verb `crons.discover.update` written)
        - bump-only path (existing row, no non-bump diff): only `last_discovered_at`
          is updated, NO audit row written.

        Bump field set: ONLY `last_discovered_at`.
        Non-bump diffable fields (parser/helper may evolve): `name`, `schedule_canonical`,
        `cadence_seconds`. `host`, `source_path`, `schedule`, `command` are baked into
        the fingerprint and cannot change without producing a new fingerprint, so they
        are NEVER diffed.

        DOES NOT TOUCH: `enabled`, `expected_grace_seconds`, `last_seen_state`,
        `hidden_at`, `wrapper_last_seen_at`. These are operator-controlled or set
        by other code paths; discovery is read-only for them.

        IMPORTANT: Fingerprint is computed from the RAW command (before scrubbing) to
        ensure convergence with the wrapper installer. The scrubbed version is stored
        in the database for display/audit purposes.
        """
        from homelab_monitor.kernel.cron.fingerprint import (  # noqa: PLC0415
            compute_fingerprint,
        )
        from homelab_monitor.kernel.cron.schedule import (  # noqa: PLC0415
            canonicalize_schedule,
            compute_average_interval_seconds,
        )
        from homelab_monitor.kernel.cron.secrets import scrub_secrets  # noqa: PLC0415

        # Compute fingerprint from RAW command (for wrapper convergence)
        fp = compute_fingerprint(
            host=host, source_path=source_path, schedule=schedule, command=command
        )
        # Scrub secrets for storage
        scrubbed_command = scrub_secrets(command)
        schedule_canonical = canonicalize_schedule(schedule)
        cadence_seconds = compute_average_interval_seconds(schedule)

        async with self._db.transaction() as conn:
            existing_row = (
                await conn.execute(_SELECT_BY_FINGERPRINT_SQL, {"fingerprint": fp})
            ).first()

            if existing_row is None:
                # ---- INSERT path: audit verb `crons.discover`, before=None ----
                params: dict[str, Any] = {
                    "fingerprint": fp,
                    "name": derive_name(command),
                    "host": host,
                    "command": scrubbed_command,
                    "schedule": schedule,
                    "schedule_canonical": schedule_canonical,
                    "cadence_seconds": cadence_seconds,
                    "expected_grace_seconds": 300,
                    "enabled": 1,
                    "last_seen_state": "unknown",
                    "created_at": now,
                    "updated_at": now,
                    "hidden_at": None,
                    "source_path": source_path,
                    "wrapper_last_seen_at": None,
                    "last_discovered_at": now,
                    "soft_deleted_at": None,
                    "log_match_key": _log_match_key_or_none(command),
                }
                await conn.execute(_INSERT_CRON_SQL, params)
                inserted_row = (
                    await conn.execute(_SELECT_BY_FINGERPRINT_SQL, {"fingerprint": fp})
                ).first()
                assert inserted_row is not None
                record = _row_to_cron(inserted_row)
                await insert_audit(
                    conn,
                    who="system",
                    what="crons.discover",
                    before=None,
                    after=_cron_to_audit_after(record),
                    ip=None,
                    when=now,
                )
                return record, True, False

            # ---- existing row path ----
            existing = _row_to_cron(existing_row)

            # Detect non-bump field drift (command, name, schedule_canonical, cadence_seconds).
            # Command can drift if scrub_secrets() logic changes (e.g., fixing false-positive
            # patterns). Name derives from the RAW command; it drifts if either the command
            # or derive_name() logic changes.
            #
            # `name` only drifts if `derive_name(command)` produces something
            # different from the stored value — which can only happen if a prior
            # write mutated `name` separately. Discovery does NOT overwrite name
            # if the user has edited it; we ONLY rewrite if it's still the
            # `derive_name(command)` default. (See "name re-derivation rule" below.)
            # SKIP automatic name re-derivation for now — see Risk §R5. Discovery
            # leaves `name` alone after first insert. If a future stage wants to
            # re-derive when the user has not edited it, gate on a separate
            # `name_was_user_edited` flag (out of scope for 007).

            diff_before: dict[str, Any] = {}
            diff_after: dict[str, Any] = {}
            updates: dict[str, Any] = {}

            if existing.command != scrubbed_command:
                diff_before["command"] = existing.command
                diff_after["command"] = scrubbed_command
                updates["command"] = scrubbed_command

            derived_name = derive_name(command)
            if existing.name != derived_name:
                diff_before["name"] = existing.name
                diff_after["name"] = derived_name
                updates["name"] = derived_name

            if existing.schedule_canonical != schedule_canonical:
                diff_before["schedule_canonical"] = existing.schedule_canonical
                diff_after["schedule_canonical"] = schedule_canonical
                updates["schedule_canonical"] = schedule_canonical
            if existing.cadence_seconds != cadence_seconds:
                diff_before["cadence_seconds"] = existing.cadence_seconds
                diff_after["cadence_seconds"] = cadence_seconds
                updates["cadence_seconds"] = cadence_seconds

            new_log_match_key = _log_match_key_or_none(command)
            if existing.log_match_key != new_log_match_key:
                diff_before["log_match_key"] = existing.log_match_key
                diff_after["log_match_key"] = new_log_match_key
                updates["log_match_key"] = new_log_match_key

            # ALWAYS bump last_discovered_at + updated_at on the existing row.
            updates["last_discovered_at"] = now
            updates["updated_at"] = now

            set_clause = ", ".join(f"{col} = :{col}" for col in updates)
            update_sql = text(f"UPDATE crons SET {set_clause} WHERE fingerprint = :fp")
            await conn.execute(update_sql, {**updates, "fp": fp})

            updated_non_bump = bool(diff_before)  # any non-bump field changed
            if updated_non_bump:
                diff_before["fingerprint"] = fp
                diff_after["fingerprint"] = fp
                diff_before["updated_at"] = existing.updated_at
                diff_after["updated_at"] = now
                await insert_audit(
                    conn,
                    who="system",
                    what="crons.discover.update",
                    before=diff_before,
                    after=diff_after,
                    ip=None,
                    when=now,
                )

            refreshed_row = (
                await conn.execute(_SELECT_BY_FINGERPRINT_SQL, {"fingerprint": fp})
            ).first()
            assert refreshed_row is not None
            return _row_to_cron(refreshed_row), False, updated_non_bump

    async def list_source_paths_for_host(self, host: str) -> frozenset[str]:
        """Return the distinct non-NULL source_paths registered for ``host``.

        Used by the cron-discoverer to find known-but-absent source files
        (operator-deleted /etc/cron.d/* files) so reconciliation can soft-delete
        their rows. Rows with ``source_path IS NULL`` are excluded.
        """
        rows = await self._db.fetch_all(
            text(
                "SELECT DISTINCT source_path FROM crons "
                "WHERE host = :host AND source_path IS NOT NULL"
            ),
            {"host": host},
        )
        paths: list[str] = [str(r.source_path) for r in rows]
        return frozenset(paths)

    async def reconcile_soft_deletes(
        self,
        *,
        host: str,
        clean_paths: frozenset[str],
        found_by_path: Mapping[str, frozenset[str]],
        now: str,
    ) -> tuple[int, int]:
        """Reconcile soft-delete state for cleanly-scanned source files.

        For every source path in ``clean_paths``, compare the DB rows for
        ``host=host AND source_path=path`` against the fingerprints the scan
        found for that path (``found_by_path.get(path, frozenset())``).

        Transitions (one-field ``soft_deleted_at`` flips, per D4):
        - Row IN DB, fingerprint NOT in scan, ``soft_deleted_at IS NULL``
          -> SET ``soft_deleted_at = now``; audit verb ``crons.soft_delete``.
        - Row IN DB, fingerprint IN scan, ``soft_deleted_at IS NOT NULL``
          -> SET ``soft_deleted_at = NULL``; audit verb ``crons.restore``.
        - Any other combination -> no-op, no audit row.

        Returns ``(soft_deleted_count, restored_count)``.

        Invariants:
        - Per-host filter is mandatory: only rows with this exact ``host`` are
          touched. Cross-host-registered rows are never affected.
        - ``source_path IS NULL`` rows are excluded automatically because
          ``clean_paths`` only ever contains real path strings.
        - This method NEVER writes ``last_discovered_at`` — that is the upsert
          path's job. It writes only ``soft_deleted_at`` and ``updated_at``.
        - All writes for one call happen in ONE transaction.
        - ``who="system"`` for all audit rows written here (discovery-driven).
        """
        if not clean_paths:
            return 0, 0

        soft_deleted_count = 0
        restored_count = 0

        select_rows_sql = text(
            "SELECT fingerprint, soft_deleted_at FROM crons "
            "WHERE host = :host AND source_path = :source_path"
        )
        set_soft_deleted_sql = text(
            "UPDATE crons SET soft_deleted_at = :sda, updated_at = :now "
            "WHERE fingerprint = :fingerprint"
        )

        async with self._db.transaction() as conn:
            for path in sorted(clean_paths):
                found = found_by_path.get(path, frozenset())
                rows = (
                    await conn.execute(select_rows_sql, {"host": host, "source_path": path})
                ).fetchall()
                for row in rows:
                    fp = str(row.fingerprint)
                    currently_soft_deleted = row.soft_deleted_at is not None
                    in_scan = fp in found

                    if not in_scan and not currently_soft_deleted:
                        # absent from a clean scan -> soft-delete
                        await conn.execute(
                            set_soft_deleted_sql,
                            {"sda": now, "now": now, "fingerprint": fp},
                        )
                        await insert_audit(
                            conn,
                            who="system",
                            what="crons.soft_delete",
                            before={"fingerprint": fp, "soft_deleted_at": None},
                            after={"fingerprint": fp, "soft_deleted_at": now},
                            ip=None,
                            when=now,
                        )
                        soft_deleted_count += 1
                    elif in_scan and currently_soft_deleted:
                        # found again -> restore
                        prev = str(row.soft_deleted_at)
                        await conn.execute(
                            set_soft_deleted_sql,
                            {"sda": None, "now": now, "fingerprint": fp},
                        )
                        await insert_audit(
                            conn,
                            who="system",
                            what="crons.restore",
                            before={"fingerprint": fp, "soft_deleted_at": prev},
                            after={"fingerprint": fp, "soft_deleted_at": None},
                            ip=None,
                            when=now,
                        )
                        restored_count += 1
                    # else: no-op (present+active OR absent+already-soft-deleted)

        return soft_deleted_count, restored_count


# Re-export AsyncConnection so static-analyzer-driven imports stay tidy.
__all__ = [
    "AsyncConnection",
    "CronListPage",
    "CronRecord",
    "CronRepo",
    "CronWithState",
    "HeartbeatStateRecord",
]
