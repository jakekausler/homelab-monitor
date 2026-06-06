"""In-memory status store for manually-triggered drain cycles (STAGE-004-027).

A bounded, TTL-pruned map from cycle_id (uuid hex) to a status entry. Used by the
POST /logs/signatures/refresh + GET /logs/signatures/refresh/{cycle_id} endpoints
so a client can poll the outcome of a fire-and-forget drain cycle.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

# _now_ms is private but importable; reused intentionally (mirrors drain_consumer).
from homelab_monitor.kernel.logs.drain_engine import (
    _now_ms,  # pyright: ignore[reportPrivateUsage]
)

if TYPE_CHECKING:
    from homelab_monitor.kernel.logs.drain_consumer import DrainCycleResult

_DEFAULT_TTL_MS = 3_600_000  # 1 hour


@dataclass(frozen=True, slots=True)
class CycleStatusEntry:
    """One tracked cycle's status + outcome."""

    status: Literal["running", "done", "failed"]
    created_ms: int
    result: DrainCycleResult | None
    error: str | None


class CycleStatusStore:
    """Tracks manual drain-cycle status with TTL pruning on access."""

    def __init__(
        self,
        *,
        ttl_ms: int = _DEFAULT_TTL_MS,
        clock: Callable[[], int] = _now_ms,
    ) -> None:
        self._ttl_ms = ttl_ms
        self._clock = clock
        self._store: dict[str, CycleStatusEntry] = {}

    def begin(self, cycle_id: str) -> None:
        """Record a newly-started cycle as 'running'."""
        self._store[cycle_id] = CycleStatusEntry(
            status="running",
            created_ms=self._clock(),
            result=None,
            error=None,
        )

    def complete(self, cycle_id: str, result: DrainCycleResult) -> None:
        """Mark a cycle 'done' with its result (preserves created_ms)."""
        created = self._store[cycle_id].created_ms if cycle_id in self._store else self._clock()
        self._store[cycle_id] = CycleStatusEntry(
            status="done",
            created_ms=created,
            result=result,
            error=None,
        )

    def fail(self, cycle_id: str, error: str) -> None:
        """Mark a cycle 'failed' with an error string (preserves created_ms)."""
        created = self._store[cycle_id].created_ms if cycle_id in self._store else self._clock()
        self._store[cycle_id] = CycleStatusEntry(
            status="failed",
            created_ms=created,
            result=None,
            error=error,
        )

    def get(self, cycle_id: str) -> CycleStatusEntry | None:
        """Return the entry for cycle_id, pruning expired entries first.

        Drops every entry older than ttl_ms (by created_ms). Returns None for an
        unknown or just-expired cycle_id.
        """
        now = self._clock()
        expired = [cid for cid, e in self._store.items() if now - e.created_ms > self._ttl_ms]
        for cid in expired:
            del self._store[cid]
        return self._store.get(cycle_id)


__all__ = ["CycleStatusEntry", "CycleStatusStore"]
