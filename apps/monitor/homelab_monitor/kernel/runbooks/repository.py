"""Runbook registry repository.

Persists the runbook registry table. SELECT-by-path drives INSERT-vs-UPDATE.
Reconcile UPDATEs ONLY the file-authoritative cached columns; the operator gates
(enabled, auto_trigger) are NEVER in any reconcile UPDATE set so a refresh can
never clobber an operator's allow-list decision (structural clobber-safety).

content_hash is the whole-folder v2 hash (compute_runbook_content_hash, STAGE-009-016):
covers runbook.yaml plus every allow-listed sibling file (e.g. CLAUDE.md, README.md).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Row
from sqlalchemy.exc import IntegrityError

from homelab_monitor.kernel.api._audit_helpers import principal_label
from homelab_monitor.kernel.auth.models import ApiToken, User
from homelab_monitor.kernel.config import get_runbooks_dir
from homelab_monitor.kernel.db.audit import insert_audit
from homelab_monitor.kernel.db.ids import uuid7
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.db.time import utc_now_iso
from homelab_monitor.kernel.runbooks.config import AlertMatcher, RunbookConfig
from homelab_monitor.kernel.runbooks.hashing import (
    RunbookHashError,
    compute_runbook_content_hash,
)
from homelab_monitor.kernel.runbooks.loader import LoadError, ScanResult

logger = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True)
class RunbookRecord:
    """A hydrated row from the ``runbooks`` table."""

    id: str
    path: str
    created_at: str
    alert_match_patterns: list[dict[str, Any]]
    risk_tag: str
    dry_run_required: bool
    rate_limit_per_hour: int | None
    cooldown_seconds: int | None
    enabled: bool
    auto_trigger: bool
    content_hash: str | None


@dataclass(slots=True, frozen=True)
class RefreshOutcome:
    """Result of a refresh scan+reconcile, echoed by the API."""

    registered: list[str] = field(default_factory=list[str])  # newly inserted
    refreshed: list[str] = field(default_factory=list[str])  # cached fields updated
    skipped: list[str] = field(default_factory=list[str])  # unchanged (hash matched)
    errors: list[LoadError] = field(default_factory=list[LoadError])  # validation errors
    pruned: list[str] = field(default_factory=list[str])  # DB rows deleted
    prune_skipped: list[str] = field(default_factory=list[str])  # folder gone but has run history


_RUNBOOK_COLS = (
    "id, path, created_at, alert_match_patterns, risk_tag, dry_run_required, "
    "rate_limit_per_hour, cooldown_seconds, enabled, auto_trigger, content_hash"
)

_SELECT_BY_PATH_SQL = text(f"SELECT {_RUNBOOK_COLS} FROM runbooks WHERE path = :path")

_SELECT_BY_ID_SQL = text(f"SELECT {_RUNBOOK_COLS} FROM runbooks WHERE id = :id")

# homelab-scale: registries expected < 100 rows. If registry grows,
# replace the prune loop's full-table fetch_all(_SELECT_ALL_SQL) with a
# targeted query, e.g. WHERE path NOT IN (:scanned_paths).
_SELECT_ALL_SQL = text(f"SELECT {_RUNBOOK_COLS} FROM runbooks ORDER BY path ASC")

_INSERT_RUNBOOK_SQL = text(
    "INSERT INTO runbooks ("
    "id, path, created_at, alert_match_patterns, risk_tag, dry_run_required, "
    "rate_limit_per_hour, cooldown_seconds, enabled, auto_trigger, content_hash"
    ") VALUES ("
    ":id, :path, :created_at, :alert_match_patterns, :risk_tag, :dry_run_required, "
    ":rate_limit_per_hour, :cooldown_seconds, :enabled, :auto_trigger, :content_hash"
    ")"
)

# Reconcile UPDATE: ONLY file-authoritative cached columns. enabled/auto_trigger
# are STRUCTURALLY absent from this statement so a refresh can never clobber them.
_UPDATE_CACHED_SQL = text(
    "UPDATE runbooks SET "
    "path = :path, "
    "alert_match_patterns = :alert_match_patterns, "
    "risk_tag = :risk_tag, "
    "dry_run_required = :dry_run_required, "
    "rate_limit_per_hour = :rate_limit_per_hour, "
    "cooldown_seconds = :cooldown_seconds, "
    "content_hash = :content_hash "
    "WHERE id = :id"
)

_UPDATE_GATES_SQL_TEMPLATE = "UPDATE runbooks SET {assignments} WHERE id = :id"

_COUNT_RUNS_FOR_RUNBOOK_SQL = text(
    "SELECT COUNT(*) AS run_count FROM runbook_runs WHERE runbook_id = :runbook_id"
)
_DELETE_RUNBOOK_SQL = text("DELETE FROM runbooks WHERE id = :id")

_MAX_RUNS_FOR_HARD_PRUNE = 0


def _row_to_runbook(row: Row[Any]) -> RunbookRecord:
    raw_patterns = row.alert_match_patterns
    patterns: list[dict[str, Any]] = [] if raw_patterns is None else json.loads(str(raw_patterns))
    return RunbookRecord(
        id=str(row.id),
        path=str(row.path),
        created_at=str(row.created_at),
        alert_match_patterns=patterns,
        risk_tag=str(row.risk_tag),
        dry_run_required=bool(row.dry_run_required),
        rate_limit_per_hour=(
            None if row.rate_limit_per_hour is None else int(row.rate_limit_per_hour)
        ),
        cooldown_seconds=(None if row.cooldown_seconds is None else int(row.cooldown_seconds)),
        enabled=bool(row.enabled),
        auto_trigger=bool(row.auto_trigger),
        content_hash=(None if row.content_hash is None else str(row.content_hash)),
    )


def _serialize_matchers(matchers: list[AlertMatcher]) -> str:
    return json.dumps([m.model_dump(mode="json") for m in matchers])


class RunbookRepo:
    """Registry persistence: scan reconcile, list, get, patch-gates."""

    def __init__(self, db: SqliteRepository) -> None:
        self._db = db

    # ---- reads ----

    async def list_runbooks(self) -> list[RunbookRecord]:
        rows = await self._db.fetch_all(_SELECT_ALL_SQL, {})
        return [_row_to_runbook(r) for r in rows]

    async def get_runbook(self, runbook_id: str) -> RunbookRecord | None:
        row = await self._db.fetch_one(_SELECT_BY_ID_SQL, {"id": runbook_id})
        return None if row is None else _row_to_runbook(row)

    # ---- reconcile ----

    async def reconcile(
        self,
        scan: ScanResult,
        *,
        who_principal: User | ApiToken,
        ip: str | None,
    ) -> RefreshOutcome:
        """Apply a loader ScanResult: INSERT new, UPDATE-cached changed, skip same.

        Each INSERT/UPDATE plus its audit row runs in one transaction per folder
        (atomic). enabled/auto_trigger are never written here.
        """
        who = principal_label(who_principal)
        registered: list[str] = []
        refreshed: list[str] = []
        skipped: list[str] = []
        hash_errors: list[LoadError] = []

        for loaded in scan.loaded:
            folder_str = str(loaded.folder)
            try:
                content_hash = compute_runbook_content_hash(loaded.folder)
            except RunbookHashError as exc:
                logger.warning("runbook_hash_error folder=%s error=%s", folder_str, exc)
                hash_errors.append(LoadError(path=folder_str, message=str(exc)))
                continue

            existing = await self._db.fetch_one(_SELECT_BY_PATH_SQL, {"path": folder_str})

            if existing is None:
                await self._insert(loaded.folder, loaded.config, content_hash, who, ip)
                registered.append(folder_str)
                continue

            record = _row_to_runbook(existing)
            if record.content_hash == content_hash:
                skipped.append(folder_str)
                continue

            await self._update_cached(
                record.id, loaded.folder, loaded.config, content_hash, who, ip, record
            )
            refreshed.append(folder_str)

        # Prune stale registry rows (see _run_prune_pass docstring for
        # C1 folder-missing rule + C2 mass-prune guard rationale).
        pruned, prune_skipped = await self._run_prune_pass(scan=scan, who=who, ip=ip)

        return RefreshOutcome(
            registered=registered,
            refreshed=refreshed,
            skipped=skipped,
            errors=[*scan.errors, *hash_errors],
            pruned=pruned,
            prune_skipped=prune_skipped,
        )

    async def _insert(
        self,
        folder: Path,
        config: RunbookConfig,
        content_hash: str,
        who: str,
        ip: str | None,
    ) -> None:
        now = utc_now_iso()
        new_id = uuid7()
        params: dict[str, Any] = {
            "id": new_id,
            "path": str(folder),
            "created_at": now,
            "alert_match_patterns": _serialize_matchers(config.match_patterns),
            "risk_tag": config.risk_tag.value,
            "dry_run_required": 1 if config.dry_run_required else 0,
            "rate_limit_per_hour": config.rate_limit_per_hour,
            "cooldown_seconds": config.cooldown_seconds,
            "enabled": 0,  # conservative default — operator must enable
            "auto_trigger": 0,  # conservative default
            "content_hash": content_hash,
        }
        after = {
            "id": new_id,
            "path": str(folder),
            "risk_tag": config.risk_tag.value,
            "dry_run_required": config.dry_run_required,
            "rate_limit_per_hour": config.rate_limit_per_hour,
            "cooldown_seconds": config.cooldown_seconds,
            "content_hash": content_hash,
        }
        async with self._db.transaction() as conn:
            await conn.execute(_INSERT_RUNBOOK_SQL, params)
            await insert_audit(
                conn,
                who=who,
                what="runbook_registered",
                before=None,
                after=after,
                ip=ip,
                when=now,
            )

    async def _update_cached(  # noqa: PLR0913
        self,
        runbook_id: str,
        folder: Path,
        config: RunbookConfig,
        content_hash: str,
        who: str,
        ip: str | None,
        old_record: RunbookRecord,
    ) -> None:
        now = utc_now_iso()
        params: dict[str, Any] = {
            "id": runbook_id,
            "path": str(folder),
            "alert_match_patterns": _serialize_matchers(config.match_patterns),
            "risk_tag": config.risk_tag.value,
            "dry_run_required": 1 if config.dry_run_required else 0,
            "rate_limit_per_hour": config.rate_limit_per_hour,
            "cooldown_seconds": config.cooldown_seconds,
            "content_hash": content_hash,
        }
        before = {
            "content_hash": old_record.content_hash,
            "risk_tag": old_record.risk_tag,
            "dry_run_required": old_record.dry_run_required,
            "rate_limit_per_hour": old_record.rate_limit_per_hour,
            "cooldown_seconds": old_record.cooldown_seconds,
            "path": old_record.path,
        }
        after = {
            "path": str(folder),
            "risk_tag": config.risk_tag.value,
            "dry_run_required": config.dry_run_required,
            "rate_limit_per_hour": config.rate_limit_per_hour,
            "cooldown_seconds": config.cooldown_seconds,
            "content_hash": content_hash,
        }
        async with self._db.transaction() as conn:
            await conn.execute(_UPDATE_CACHED_SQL, params)
            await insert_audit(
                conn,
                who=who,
                what="runbook_refreshed",
                before=before,
                after=after,
                ip=ip,
                when=now,
            )

    async def _prune(self, record: RunbookRecord, *, who: str, ip: str | None) -> None:
        """Hard-delete a registry row whose folder is gone and has no run
        history.

        Callers must have verified folder-absent + run-history-empty; this
        method does no checks.
        """
        before = {
            "id": record.id,
            "path": record.path,
            "content_hash": record.content_hash,
            "enabled": record.enabled,
            "auto_trigger": record.auto_trigger,
            "risk_tag": record.risk_tag,
        }
        async with self._db.transaction() as conn:
            await conn.execute(_DELETE_RUNBOOK_SQL, {"id": record.id})
            await insert_audit(
                conn,
                who=who,
                what="runbook_pruned",
                before=before,
                after=None,
                ip=ip,
            )

    async def _emit_prune_skipped(
        self, record: RunbookRecord, *, runs_count: int, who: str, ip: str | None
    ) -> None:
        """Audit-only: folder is gone but run history blocks deletion (FK
        safety).

        This audit is emitted per-refresh (not deduplicated) — every refresh
        call that hits a prune-blocked row emits a fresh audit row. This is
        intentional per non-negotiable #4 (every material decision, including
        a refusal-to-prune, is auditable).
        """
        # NOTE: per-refresh emission is intentional — see docstring.
        before = {
            "id": record.id,
            "path": record.path,
            "runs_count": runs_count,
        }
        async with self._db.transaction() as conn:
            await insert_audit(
                conn,
                who=who,
                what="runbook_prune_skipped",
                before=before,
                after=None,
                ip=ip,
            )

    async def _run_prune_pass(
        self,
        *,
        scan: ScanResult,
        who: str,
        ip: str | None,
    ) -> tuple[list[str], list[str]]:
        """Prune registry rows whose folders were deleted.

        Called after the scan-loop in reconcile(). Returns (pruned, prune_skipped)
        path lists. See the C1 (folder-missing-only) and C2 (mass-prune guard)
        rationale in the code comments below.

        C1: folders that still exist but were rejected by the loader/hasher
        are NEVER pruned (they surface via `errors` instead).

        C2 (mass-prune guard): if the runbooks root itself is missing/not-a-
        directory, scan_runbooks() returns loaded=[] and a single root-level
        LoadError whose path equals the root. In that case every DB row would
        look "missing from scan" and (if the parent dir is also gone) would
        also fail Path.exists(), causing every row to be hard-deleted. Skip
        the entire prune pass when the scan reports a root-level error so a
        misconfigured/transiently-unmounted HOMELAB_MONITOR_RUNBOOKS_DIR can
        never wipe the registry.
        """
        pruned: list[str] = []
        prune_skipped: list[str] = []

        runbooks_root_str = str(get_runbooks_dir())
        root_scan_failed = any(err.path == runbooks_root_str for err in scan.errors)

        if root_scan_failed:
            logger.warning(
                "runbooks_prune_skipped_root_missing root=%s errors=%s",
                runbooks_root_str,
                [err.message for err in scan.errors],
            )
            return pruned, prune_skipped

        scanned_paths: set[str] = {str(loaded.folder) for loaded in scan.loaded}
        all_rows = await self._db.fetch_all(_SELECT_ALL_SQL, {})
        for row in all_rows:
            record = _row_to_runbook(row)
            if record.path in scanned_paths:
                continue
            if Path(record.path).exists():
                continue
            count_row = await self._db.fetch_one(
                _COUNT_RUNS_FOR_RUNBOOK_SQL, {"runbook_id": record.id}
            )
            runs_count = int(count_row.run_count) if count_row is not None else 0
            if runs_count > _MAX_RUNS_FOR_HARD_PRUNE:
                await self._emit_prune_skipped(record, runs_count=runs_count, who=who, ip=ip)
                prune_skipped.append(record.path)
            else:
                try:
                    await self._prune(record, who=who, ip=ip)
                    pruned.append(record.path)
                except IntegrityError:
                    # TOCTOU: a runbook_runs row was inserted for this
                    # runbook_id between the COUNT above and this DELETE
                    # (e.g. an alert fired and triggered autofix mid-
                    # refresh). The FK constraint blocked the delete;
                    # treat this as prune-skipped instead of raising.
                    # The exact post-race run count is unknown here, so
                    # report a sentinel rather than a stale/misleading 0.
                    logger.warning(
                        "runbook_prune_race_detected runbook_id=%s path=%s",
                        record.id,
                        record.path,
                    )
                    await self._emit_prune_skipped(record, runs_count=-1, who=who, ip=ip)
                    prune_skipped.append(record.path)

        return pruned, prune_skipped

    # ---- operator gates ----

    async def patch_gates(
        self,
        runbook_id: str,
        *,
        enabled: bool | None,
        auto_trigger: bool | None,
        who_principal: User | ApiToken,
        ip: str | None,
    ) -> RunbookRecord:
        """Patch operator gates. Writes ONLY the provided gate(s); the other gate
        is preserved. Raises LookupError if the runbook does not exist. Audits
        ``runbook_gates_changed`` with before/after of the changed gate(s).
        """
        existing = await self._db.fetch_one(_SELECT_BY_ID_SQL, {"id": runbook_id})
        if existing is None:
            raise LookupError(f"runbook {runbook_id} not found")
        record = _row_to_runbook(existing)

        assignments: list[str] = []
        params: dict[str, Any] = {"id": runbook_id}
        before: dict[str, Any] = {}
        after: dict[str, Any] = {}

        if enabled is not None and enabled != record.enabled:
            assignments.append("enabled = :enabled")
            params["enabled"] = 1 if enabled else 0
            before["enabled"] = record.enabled
            after["enabled"] = enabled

        if auto_trigger is not None and auto_trigger != record.auto_trigger:
            assignments.append("auto_trigger = :auto_trigger")
            params["auto_trigger"] = 1 if auto_trigger else 0
            before["auto_trigger"] = record.auto_trigger
            after["auto_trigger"] = auto_trigger

        if not assignments:
            # No-op patch (both None, or values equal current): no write, no audit.
            return record

        now = utc_now_iso()
        update_sql = text(_UPDATE_GATES_SQL_TEMPLATE.format(assignments=", ".join(assignments)))
        async with self._db.transaction() as conn:
            await conn.execute(update_sql, params)
            await insert_audit(
                conn,
                who=principal_label(who_principal),
                what="runbook_gates_changed",
                before=before,
                after=after,
                ip=ip,
                when=now,
            )

        # Reconstruct in memory (avoids an uncoverable None branch on re-SELECT):
        return RunbookRecord(
            id=record.id,
            path=record.path,
            created_at=record.created_at,
            alert_match_patterns=record.alert_match_patterns,
            risk_tag=record.risk_tag,
            dry_run_required=record.dry_run_required,
            rate_limit_per_hour=record.rate_limit_per_hour,
            cooldown_seconds=record.cooldown_seconds,
            enabled=(enabled if enabled is not None else record.enabled),
            auto_trigger=(auto_trigger if auto_trigger is not None else record.auto_trigger),
            content_hash=record.content_hash,
        )


__all__ = ["RefreshOutcome", "RunbookRecord", "RunbookRepo"]
