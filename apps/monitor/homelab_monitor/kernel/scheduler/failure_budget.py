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

from homelab_monitor.kernel.db.audit import insert_audit
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.db.time import utc_now_iso


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

    def __init__(
        self,
        repo: SqliteRepository,
        log: BoundLogger,
        clock: Callable[[], str] | None = None,
        default_threshold: int = 5,
    ) -> None:
        """Stash dependencies; do NOT touch the DB here.

        Args:
            repo: Async SQLite repository for persistence.
            log: Structured logger for quarantine warnings.
            clock: Returns ISO-8601 UTC timestamp. Defaults to ``utc_now_iso``.
                Injected for deterministic testing.
            default_threshold: Consecutive failures before quarantine.
                Per-collector override via ``CollectorConfig.quarantine_after``.
        """
        self._repo = repo
        self._log = log
        self._clock: Callable[[], str] = clock if clock is not None else utc_now_iso
        self._default_threshold = default_threshold
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

    def consecutive_failures(self, name: str) -> int:
        """Return in-memory counter; 0 if collector has never failed."""
        return self._consecutive_failures.get(name, 0)

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

        # SCAFFOLDING: STAGE-001-013 will dispatch a real alert here via the
        # alert ingestor. For now, the WARNING log + audit_log row are the
        # only signal.

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
