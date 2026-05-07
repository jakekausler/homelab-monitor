"""Async repository for alerts and alert outcomes.

Mirrors :class:`AuthRepository`: every state-changing method writes an
``audit_log`` row in the SAME transaction as the primary write. Reads use
``self._repo.fetch_one`` / ``fetch_all`` for one-shot queries.

Spec A delivers the foundation. Spec B wires it into ``/api/alerts/*`` and
the scheduler quarantine path.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Row

from homelab_monitor.kernel.alerts.types import (
    Alert,
    AlertOutcome,
    AlertStatus,
    Severity,
)
from homelab_monitor.kernel.db.audit import insert_audit
from homelab_monitor.kernel.db.ids import uuid7
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.db.time import utc_now_iso


class AlertRepository:
    """Async CRUD over ``alerts`` and ``alert_outcomes``.

    State-changing methods are atomic with their audit row. Listing supports
    cursor-based pagination on ``(opened_at DESC, id DESC)``.
    """

    def __init__(self, repo: SqliteRepository) -> None:
        self._repo = repo

    # ----- helpers -----

    @staticmethod
    def _row_to_alert(row: Row[Any]) -> Alert:
        """Hydrate a SELECT * row (named-tuple style) into an :class:`Alert`."""
        payload_text = str(row.payload_json) if row.payload_json is not None else "{}"
        payload = json.loads(payload_text)
        # Labels and annotations are stashed inside the payload JSON for
        # round-trip; the ingest path is responsible for placing them there.
        labels_obj = payload.get("labels", {})
        annotations_obj = payload.get("annotations", {})
        labels: dict[str, str] = {str(k): str(v) for k, v in labels_obj.items()}
        annotations: dict[str, str] = {str(k): str(v) for k, v in annotations_obj.items()}
        return Alert(
            id=str(row.id),
            fingerprint=str(row.fingerprint),
            source_tool=str(row.source_tool),
            severity=Severity(str(row.severity)),
            status=AlertStatus(str(row.status)),
            opened_at=str(row.opened_at),
            last_seen_at=str(row.last_seen_at),
            resolved_at=None if row.resolved_at is None else str(row.resolved_at),
            ack_at=None if row.ack_at is None else str(row.ack_at),
            ack_by=None if row.ack_by is None else int(row.ack_by),
            runbook_id=None if row.runbook_id is None else str(row.runbook_id),
            payload=payload,
            labels=labels,
            annotations=annotations,
        )

    # ----- queries -----

    async def find_active_by_fingerprint(self, fingerprint: str) -> Alert | None:
        """Return the most-recent unresolved alert for ``fingerprint``, if any."""
        row = await self._repo.fetch_one(
            text(
                "SELECT id, fingerprint, source_tool, severity, status, "
                "opened_at, last_seen_at, resolved_at, ack_at, ack_by, "
                "runbook_id, payload_json "
                "FROM alerts "
                "WHERE fingerprint = :fp AND resolved_at IS NULL "
                "ORDER BY opened_at DESC LIMIT 1"
            ),
            {"fp": fingerprint},
        )
        if row is None:
            return None
        return self._row_to_alert(row)

    async def get_alert_by_id(self, alert_id: str) -> Alert | None:
        """Return the alert with ``id == alert_id`` or ``None``."""
        row = await self._repo.fetch_one(
            text(
                "SELECT id, fingerprint, source_tool, severity, status, "
                "opened_at, last_seen_at, resolved_at, ack_at, ack_by, "
                "runbook_id, payload_json "
                "FROM alerts WHERE id = :id"
            ),
            {"id": alert_id},
        )
        if row is None:
            return None
        return self._row_to_alert(row)

    async def list_alerts(  # noqa: PLR0913
        self,
        *,
        status: AlertStatus | None = None,
        severity: Severity | None = None,
        source_tool: str | None = None,
        fingerprint: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> tuple[list[Alert], str | None]:
        """List alerts ordered by ``opened_at DESC, id DESC`` with cursor pagination.

        Cursor format: ``f"{opened_at}|{id}"`` of the last row from the
        previous page. The returned ``next_cursor`` is the cursor for the LAST
        row of the current page if a next page may exist (``len(rows) ==
        limit``), else ``None``.
        """
        clauses: list[str] = []
        params: dict[str, Any] = {"lim": limit}
        if status is not None:
            clauses.append("status = :status")
            params["status"] = status.value
        if severity is not None:
            clauses.append("severity = :severity")
            params["severity"] = severity.value
        if source_tool is not None:
            clauses.append("source_tool = :source_tool")
            params["source_tool"] = source_tool
        if fingerprint is not None:
            clauses.append("fingerprint = :fingerprint")
            params["fingerprint"] = fingerprint
        if cursor is not None:
            cur_opened, cur_id = cursor.split("|", 1)
            # Strict tuple comparison: rows older than the cursor row.
            clauses.append(
                "(opened_at < :cur_opened OR (opened_at = :cur_opened AND id < :cur_id))"
            )
            params["cur_opened"] = cur_opened
            params["cur_id"] = cur_id

        where_sql = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = (
            "SELECT id, fingerprint, source_tool, severity, status, "
            "opened_at, last_seen_at, resolved_at, ack_at, ack_by, "
            "runbook_id, payload_json "
            "FROM alerts" + where_sql + " "
            "ORDER BY opened_at DESC, id DESC LIMIT :lim"
        )
        rows = await self._repo.fetch_all(text(sql), params)
        items = [self._row_to_alert(r) for r in rows]

        next_cursor: str | None = None
        if len(items) == limit and items:
            last = items[-1]
            next_cursor = f"{last.opened_at}|{last.id}"
        return items, next_cursor

    # ----- mutations -----

    async def insert_firing(self, alert: Alert, payload_json: str) -> str:
        """Insert a new firing alert atomically with its audit row.

        Returns the newly-allocated ``id``. The supplied ``alert.id`` is
        ignored; this method always allocates a fresh ``uuid7()``. The row is
        stored with ``status='firing'``, ``opened_at = last_seen_at = now``,
        ``payload_json`` as supplied (the caller is responsible for encoding
        labels and annotations into that JSON so :meth:`_row_to_alert` can
        rehydrate them).
        """
        new_id = uuid7()
        now = utc_now_iso()
        async with self._repo.transaction() as conn:
            await conn.execute(
                text(
                    "INSERT INTO alerts (id, fingerprint, source_tool, severity, "
                    "status, opened_at, last_seen_at, payload_json, created_at) "
                    "VALUES (:id, :fp, :st, :sev, :status, :opened, :last_seen, :pj, :created)"
                ),
                {
                    "id": new_id,
                    "fp": alert.fingerprint,
                    "st": alert.source_tool,
                    "sev": alert.severity.value,
                    "status": AlertStatus.FIRING.value,
                    "opened": now,
                    "last_seen": now,
                    "pj": payload_json,
                    "created": now,
                },
            )
            await insert_audit(
                conn,
                who="system",
                what="alert.fire",
                after={
                    "alert_id": new_id,
                    "fingerprint": alert.fingerprint,
                    "source_tool": alert.source_tool,
                    "severity": alert.severity.value,
                },
            )
        return new_id

    async def update_last_seen(self, alert_id: str, ts: str) -> None:
        """Bump ``last_seen_at`` for an existing firing alert.

        Touches only ``last_seen_at``; ``opened_at`` MUST remain unchanged so
        downstream queries can still see the original opening time.
        """
        async with self._repo.transaction() as conn:
            await conn.execute(
                text("UPDATE alerts SET last_seen_at = :ts WHERE id = :id"),
                {"ts": ts, "id": alert_id},
            )
            await insert_audit(
                conn,
                who="system",
                what="alert.last_seen",
                after={"alert_id": alert_id, "last_seen_at": ts},
            )

    async def mark_resolved(self, alert_id: str, resolved_at: str) -> None:
        """Mark the alert resolved (status=resolved, resolved_at=ts)."""
        async with self._repo.transaction() as conn:
            await conn.execute(
                text("UPDATE alerts SET status = :status, resolved_at = :rt WHERE id = :id"),
                {
                    "status": AlertStatus.RESOLVED.value,
                    "rt": resolved_at,
                    "id": alert_id,
                },
            )
            await insert_audit(
                conn,
                who="system",
                what="alert.resolve",
                after={"alert_id": alert_id, "resolved_at": resolved_at},
            )

    # ----- outcomes -----

    async def insert_outcome(
        self,
        alert_id: str,
        outcome: AlertOutcome,
        decided_by: int | None,
    ) -> str:
        """Insert an ``alert_outcomes`` row + audit; returns the new id."""
        new_id = uuid7()
        now = utc_now_iso()
        async with self._repo.transaction() as conn:
            await conn.execute(
                text(
                    "INSERT INTO alert_outcomes "
                    "(id, alert_id, outcome, decided_at, decided_by, created_at) "
                    "VALUES (:id, :aid, :outcome, :dt, :db, :created)"
                ),
                {
                    "id": new_id,
                    "aid": alert_id,
                    "outcome": outcome.value,
                    "dt": now,
                    "db": decided_by,
                    "created": now,
                },
            )
            await insert_audit(
                conn,
                who=str(decided_by) if decided_by is not None else "system",
                what=f"alert.outcome.{outcome.value}",
                after={
                    "alert_id": alert_id,
                    "outcome_id": new_id,
                    "outcome": outcome.value,
                },
            )
        return new_id

    async def list_outcomes(self, alert_id: str) -> list[dict[str, Any]]:
        """Return outcomes for ``alert_id`` ordered by ``decided_at DESC``."""
        rows = await self._repo.fetch_all(
            text(
                "SELECT outcome, decided_at, decided_by FROM alert_outcomes "
                "WHERE alert_id = :aid ORDER BY decided_at DESC"
            ),
            {"aid": alert_id},
        )
        return [
            {
                "outcome": str(r[0]),
                "decided_at": str(r[1]),
                "decided_by": None if r[2] is None else int(r[2]),
            }
            for r in rows
        ]

    async def set_ack(self, alert_id: str, ack_at: str, ack_by: int) -> None:
        """Set ``ack_at`` and ``ack_by`` on an existing alert row.

        Spec B: called by the ``POST /api/alerts/{id}/ack`` endpoint AFTER
        ``insert_outcome`` has recorded the operator's decision. Atomic with
        an audit row recording the actor.

        Does NOT change ``status`` — an acked alert may still be firing; ack
        is a separate axis from lifecycle.
        """
        async with self._repo.transaction() as conn:
            await conn.execute(
                text("UPDATE alerts SET ack_at = :ack_at, ack_by = :ack_by WHERE id = :id"),
                {"ack_at": ack_at, "ack_by": ack_by, "id": alert_id},
            )
            await insert_audit(
                conn,
                who=str(ack_by),
                what="alert.ack",
                after={"alert_id": alert_id, "ack_at": ack_at, "ack_by": ack_by},
            )

    async def find_active_quarantine_alert(self, collector_name: str) -> Alert | None:
        """Return the active (firing, unresolved) scheduler-sourced quarantine alert
        for ``collector_name``, or ``None``.

        Used by ``Scheduler.clear_quarantine`` to find the row to mark resolved
        without recomputing a fingerprint from a possibly-stale reason string.
        Filters on ``source_tool='scheduler' AND status='firing' AND
        json_extract(payload_json, '$.collector_name') = :name``.
        """
        row = await self._repo.fetch_one(
            text(
                "SELECT id, fingerprint, source_tool, severity, status, "
                "opened_at, last_seen_at, resolved_at, ack_at, ack_by, "
                "runbook_id, payload_json "
                "FROM alerts "
                "WHERE source_tool = 'scheduler' "
                "  AND status = 'firing' "
                "  AND resolved_at IS NULL "
                "  AND json_extract(payload_json, '$.collector_name') = :name "
                "ORDER BY opened_at DESC LIMIT 1"
            ),
            {"name": collector_name},
        )
        if row is None:
            return None
        return self._row_to_alert(row)
