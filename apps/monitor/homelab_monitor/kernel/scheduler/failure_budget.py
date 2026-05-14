"""Per-collector failure budget + quarantine state.

Tracks consecutive failures per collector. When a collector exceeds its
quarantine threshold (default 5; per-collector override via
``CollectorConfig.quarantine_after``), the FailureBudget records the
quarantine state in the ``collectors`` table and emits a structured log
+ ``audit_log`` entry.

In-memory state is authoritative; persisted state lives in three columns
on ``collectors``: ``consecutive_failures``, ``quarantined_at``,
``quarantine_reason``.

Persistence policy:

- On ``record_failure`` -> UPDATE counter (in-memory + DB).
- On quarantine entry -> UPDATE all 3 columns + audit (atomic transaction).
- On ``clear_quarantine`` -> UPDATE all 3 columns to NULL/0 + audit.
- On ``record_success`` -> reset in-memory counter to 0; do NOT persist.
  (Eliminates ~99% of steady-state DB writes; on restart, in-memory
  starts from persisted DB value, which is over-conservative but safe.)

Audit event names:

- ``collector.quarantine_entered``
- ``collector.quarantine_cleared``
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import text
from structlog.stdlib import BoundLogger

from homelab_monitor.kernel.alerts.events import AlertFiringEvent
from homelab_monitor.kernel.alerts.fingerprinting import quarantine_fingerprint
from homelab_monitor.kernel.alerts.repository import AlertRepository
from homelab_monitor.kernel.alerts.types import Alert, AlertStatus, Severity
from homelab_monitor.kernel.db.audit import insert_audit
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.db.time import utc_now_iso
from homelab_monitor.kernel.dispatch.dispatcher import AlertDispatcher


@dataclass(frozen=True, slots=True)
class QuarantineState:
    """Snapshot of a collector's quarantine state for audit before/after."""

    consecutive_failures: int
    quarantined_at: str | None
    quarantine_reason: str | None


class FailureBudget:
    """Per-collector consecutive-failure tracker with quarantine on threshold.

    State machine::

        clean --failure--> ... --failure (count == threshold)--> quarantined
        quarantined --clear_quarantine()--> clean
        clean --success--> clean (counter resets in-memory only)

    Successful runs reset the in-memory counter but do NOT auto-clear
    quarantine -- only ``clear_quarantine`` does. Successful runs while
    quarantined cannot occur because the scheduler gates ticks on
    ``is_quarantined``.
    """

    def __init__(  # noqa: PLR0913
        self,
        repo: SqliteRepository,
        log: BoundLogger,
        clock: Callable[[], str] | None = None,
        default_threshold: int = 5,
        *,
        alert_repo: AlertRepository | None = None,
        dispatcher: AlertDispatcher | None = None,
    ) -> None:
        """Stash dependencies; do NOT touch the DB here.

        Args:
            repo: Async SQLite repository for persistence.
            log: Structured logger for quarantine warnings.
            clock: Returns ISO-8601 UTC timestamp. Defaults to ``utc_now_iso``.
                Injected for deterministic testing.
            default_threshold: Consecutive failures before quarantine.
                Per-collector override via ``CollectorConfig.quarantine_after``.
            alert_repo: Optional ``AlertRepository`` for writing quarantine alert
                rows on entry. When ``None``, quarantine emits no alert.
            dispatcher: Optional ``AlertDispatcher`` for fanning out
                ``AlertFiringEvent`` on quarantine entry. When ``None``, no
                event is dispatched.
        """
        self._repo = repo
        self._log = log
        self._clock: Callable[[], str] = clock if clock is not None else utc_now_iso
        self._default_threshold = default_threshold
        self._alert_repo = alert_repo
        self._dispatcher = dispatcher
        self._consecutive_failures: dict[str, int] = {}
        self._quarantined: dict[str, QuarantineState] = {}
        self._loaded: bool = False

    async def load_state(self) -> None:
        """Read persisted counters and quarantine state into memory.

        Called from ``Scheduler.start()``. Idempotent: returns early if
        already loaded.
        """
        if self._loaded:
            return

        async with self._repo.transaction() as conn:
            result = await conn.execute(
                text(
                    "SELECT name, consecutive_failures, quarantined_at, quarantine_reason "
                    "FROM collectors "
                    "WHERE consecutive_failures > 0 OR quarantined_at IS NOT NULL"
                )
            )
            rows = result.fetchall()

        for row in rows:
            name = row[0]
            count = row[1] or 0
            quarantined_at = row[2]
            quarantine_reason = row[3]

            if count > 0:
                self._consecutive_failures[name] = count
            if quarantined_at is not None:
                self._quarantined[name] = QuarantineState(
                    consecutive_failures=count,
                    quarantined_at=quarantined_at,
                    quarantine_reason=quarantine_reason,
                )

        self._loaded = True

    def is_quarantined(self, name: str) -> bool:
        """In-memory check; safe to call outside an async context."""
        return name in self._quarantined

    def quarantined_names(self) -> list[str]:
        """Return sorted list of currently quarantined collector names."""
        return sorted(self._quarantined.keys())

    def consecutive_failures(self, name: str) -> int:
        """Return in-memory counter; 0 if collector has never failed."""
        return self._consecutive_failures.get(name, 0)

    def degraded_names(self) -> list[str]:
        """Return collector names with elevated consecutive failures but not yet quarantined."""
        return sorted(
            name
            for name, count in self._consecutive_failures.items()
            if count > 0 and name not in self._quarantined
        )

    def quarantine_state(self, name: str) -> QuarantineState | None:
        """Return the QuarantineState for a quarantined collector, or None."""
        return self._quarantined.get(name)

    async def record_success(self, name: str) -> None:
        """Reset in-memory counter to 0.

        Does NOT persist and does NOT auto-clear quarantine. If the
        collector is somehow already quarantined (shouldn't happen due
        to scheduler gating), no state change occurs.
        """
        if name in self._quarantined:
            # Defensive: scheduler gates ticks on is_quarantined, so this
            # branch is not expected during normal operation. Leave state
            # alone -- only clear_quarantine() removes quarantine.
            return
        self._consecutive_failures[name] = 0

    async def record_failure(
        self,
        name: str,
        reason: str,
        threshold: int | None = None,
    ) -> None:
        """Increment counter; persist; transition to quarantine if threshold met.

        Args:
            name: Collector name.
            reason: Failure reason (``"timeout"``, ``"exception"``, or
                ``"result_error"``).
            threshold: Per-collector ``quarantine_after`` override; falls back
                to ``default_threshold`` when ``None``.
        """
        if name in self._quarantined:
            # Defensive: scheduler gates ticks on is_quarantined, so this branch
            # is not expected during normal operation. Guards against double
            # quarantine_entered audit rows if a future code path bypasses the gate.
            return

        prev = self._consecutive_failures.get(name, 0)
        new_count = prev + 1

        effective_threshold = threshold if threshold is not None else self._default_threshold

        if new_count < effective_threshold:
            # Persist counter only (no quarantine yet).
            async with self._repo.transaction() as conn:
                await conn.execute(
                    text("UPDATE collectors SET consecutive_failures = :n WHERE name = :name"),
                    {"n": new_count, "name": name},
                )
            # Only set in-memory state after the transaction succeeds.
            self._consecutive_failures[name] = new_count
            return

        # Threshold met -> enter quarantine.
        quarantined_at = self._clock()
        quarantine_reason = f"consecutive failures: {new_count} (last reason: {reason})"

        before = QuarantineState(
            consecutive_failures=prev,
            quarantined_at=None,
            quarantine_reason=None,
        )
        after = QuarantineState(
            consecutive_failures=new_count,
            quarantined_at=quarantined_at,
            quarantine_reason=quarantine_reason,
        )

        async with self._repo.transaction() as conn:
            await conn.execute(
                text(
                    "UPDATE collectors "
                    "SET consecutive_failures = :n, "
                    "    quarantined_at = :at, "
                    "    quarantine_reason = :reason "
                    "WHERE name = :name"
                ),
                {
                    "n": new_count,
                    "at": quarantined_at,
                    "reason": quarantine_reason,
                    "name": name,
                },
            )
            await insert_audit(
                conn,
                who="scheduler",
                what="collector.quarantine_entered",
                before={
                    "name": name,
                    "consecutive_failures": before.consecutive_failures,
                    "quarantined_at": before.quarantined_at,
                    "quarantine_reason": before.quarantine_reason,
                },
                after={
                    "name": name,
                    "consecutive_failures": after.consecutive_failures,
                    "quarantined_at": after.quarantined_at,
                    "quarantine_reason": after.quarantine_reason,
                },
            )

        # Only set in-memory state after the transaction succeeds.
        self._consecutive_failures[name] = new_count
        self._quarantined[name] = after

        self._log.warning(
            "collector_quarantined",
            name=name,
            consecutive_failures=new_count,
            last_reason=reason,
            quarantined_at=quarantined_at,
        )

        # STAGE-001-013 Spec B: dispatch an AlertFiringEvent through the alert
        # subsystem so the dashboard sees a real alert row + SSE event for
        # collector quarantines. Skipped silently when alert_repo or dispatcher
        # was not wired (unit tests that exercise quarantine logic without the
        # alert subsystem still pass).
        if self._alert_repo is not None and self._dispatcher is not None:
            await self._emit_quarantine_alert(
                name=name,
                reason=reason,
                consecutive_failures=new_count,
                ts=quarantined_at,
            )

    async def _emit_quarantine_alert(
        self,
        *,
        name: str,
        reason: str,
        consecutive_failures: int,
        ts: str,
    ) -> None:
        """Persist + dispatch a collector-quarantine alert.

        - Computes ``quarantine_fingerprint(name, reason)``.
        - Looks up the active row by fingerprint:
          - found: bump ``last_seen_at``; reuse opened_at + alert_id.
          - not found: insert a new firing row. ``insert_firing`` is
            race-safe: a concurrent insert of the same fingerprint is
            collapsed to ``last_seen`` bump via the
            ``ux_alerts_fingerprint_firing`` unique partial index (F1).

        Note (F2): the dispatched ``AlertFiringEvent``'s severity, labels,
        and annotations are pinned at FIRST fire of the fingerprint.
        Re-fires from a different originating call site (e.g., escalation
        from warning to critical on the same alertname+labels) will NOT be
        reflected. Operators tracking severity changes should ensure
        upstream produces distinct fingerprints per severity tier.

        Both ``self._alert_repo`` and ``self._dispatcher`` MUST be non-None
        when this method is called (caller's responsibility).
        """
        assert self._alert_repo is not None
        assert self._dispatcher is not None

        fp = quarantine_fingerprint(name, reason)
        labels = {
            "alertname": "collector_quarantined",
            "collector_name": name,
            "reason": reason,
        }
        annotations: dict[str, str] = {}

        existing = await self._alert_repo.find_active_by_fingerprint(fp)
        if existing is not None:
            await self._alert_repo.update_last_seen(existing.id, ts)
            alert_id = existing.id
            opened_at = existing.opened_at
        else:
            payload: dict[str, object] = {
                "labels": labels,
                "annotations": annotations,
                "collector_name": name,
                "reason": reason,
                "consecutive_failures": consecutive_failures,
            }
            new_alert = Alert(
                id="",  # repo allocates uuid7
                fingerprint=fp,
                source_tool="scheduler",
                severity=Severity.WARNING,
                status=AlertStatus.FIRING,
                opened_at=ts,
                last_seen_at=ts,
                payload=payload,
                labels=labels,
                annotations=annotations,
            )
            # F8: payload_json derived from alert.payload by the repo.
            alert_id = await self._alert_repo.insert_firing(new_alert)
            opened_at = ts

        event = AlertFiringEvent(
            alert_id=alert_id,
            fingerprint=fp,
            source_tool="scheduler",
            severity=Severity.WARNING,
            opened_at=opened_at,
            last_seen_at=ts,
            labels=labels,
            annotations=annotations,
            ts=ts,
        )
        await self._dispatcher.dispatch(event)

    async def clear_quarantine(self, name: str, by: str = "operator") -> None:
        """Reset counter, clear DB quarantine state, write audit row.

        Idempotent: if not quarantined, no-op.

        Args:
            name: Collector name.
            by: Actor identifier for audit ``who`` field. Default
                ``"operator"``; STAGE-001-010 supplies real users from the
                authenticated request context.
        """
        if not self._loaded:
            await self.load_state()

        if name not in self._quarantined:
            return  # idempotent

        before = self._quarantined[name]
        after = QuarantineState(
            consecutive_failures=0,
            quarantined_at=None,
            quarantine_reason=None,
        )

        async with self._repo.transaction() as conn:
            await conn.execute(
                text(
                    "UPDATE collectors "
                    "SET consecutive_failures = 0, "
                    "    quarantined_at = NULL, "
                    "    quarantine_reason = NULL "
                    "WHERE name = :name"
                ),
                {"name": name},
            )
            await insert_audit(
                conn,
                who=by,
                what="collector.quarantine_cleared",
                before={
                    "name": name,
                    "consecutive_failures": before.consecutive_failures,
                    "quarantined_at": before.quarantined_at,
                    "quarantine_reason": before.quarantine_reason,
                },
                after={
                    "name": name,
                    "consecutive_failures": after.consecutive_failures,
                    "quarantined_at": after.quarantined_at,
                    "quarantine_reason": after.quarantine_reason,
                },
            )

        del self._quarantined[name]
        self._consecutive_failures[name] = 0

        self._log.info(
            "collector_quarantine_cleared",
            name=name,
            cleared_by=by,
        )

    async def clear_all_quarantine(self, by: str = "system") -> int:
        """Clear all quarantined collectors on startup.

        Idempotent: if no collectors are quarantined, returns 0.

        Args:
            by: Actor identifier for audit ``who`` field. Default
                ``"system"`` for startup clearing.

        Returns:
            Count of collectors that were quarantined and cleared.
        """
        if not self._loaded:
            await self.load_state()

        cleared_names = list(self._quarantined.keys())
        if not cleared_names:
            return 0

        for name in cleared_names:
            await self.clear_quarantine(name, by=by)

        return len(cleared_names)
