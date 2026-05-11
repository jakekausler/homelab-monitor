"""In-memory per-cron token-bucket rate limiter for the heartbeat receiver.

Single-process, single-event-loop scope. Buckets reset on process restart;
that is acceptable per Decision 3a (the limiter exists to defend the
audit_log + heartbeats_state table from runaway cron loops, not to enforce a
strict global SLA — vmalert rules + intrusion detection cover the latter).

Concurrency note: the kernel runs single-threaded asyncio per process. The
bucket dict mutations in ``try_acquire`` are atomic from asyncio's perspective
because there is no ``await`` between read and write — only synchronous code.
No ``asyncio.Lock`` is required.

Configuration:
- ``capacity`` (default 60): max burst per cron.
- ``refill_per_second`` (default 1.0): steady-state ceiling = 60 pings/min/cron.

The clock is injectable for tests (``clock=lambda: ...``).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(slots=True)
class _Bucket:
    """Internal per-cron bucket state."""

    tokens: float
    last_refill_at: float


class CronRateLimiter:
    """Token-bucket per cron_id, single-process scope.

    A ``try_acquire(cron_id)`` either consumes one token and returns
    ``(True, 0.0)``, or returns ``(False, retry_after)`` where ``retry_after``
    is the seconds until at least one token would be available.
    """

    def __init__(
        self,
        *,
        capacity: int = 60,
        refill_per_second: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if capacity <= 0:
            msg = f"capacity must be positive, got {capacity}"
            raise ValueError(msg)
        if refill_per_second <= 0:
            msg = f"refill_per_second must be positive, got {refill_per_second}"
            raise ValueError(msg)
        self._capacity = float(capacity)
        self._refill_per_second = refill_per_second
        self._clock = clock
        self._buckets: dict[str, _Bucket] = {}

    def try_acquire(self, cron_id: str) -> tuple[bool, float]:
        """Attempt to consume one token for ``cron_id``.

        Returns:
            (True, 0.0) on success.
            (False, retry_after_seconds) when the bucket is empty.
        """
        now = self._clock()
        bucket = self._buckets.get(cron_id)
        if bucket is None:
            bucket = _Bucket(tokens=self._capacity, last_refill_at=now)
            self._buckets[cron_id] = bucket
        else:
            elapsed = max(0.0, now - bucket.last_refill_at)
            bucket.tokens = min(
                self._capacity,
                bucket.tokens + elapsed * self._refill_per_second,
            )
            bucket.last_refill_at = now

        if bucket.tokens >= 1.0:
            bucket.tokens -= 1.0
            return True, 0.0

        # Not enough tokens: compute when at least one will be available.
        deficit = 1.0 - bucket.tokens
        retry_after = deficit / self._refill_per_second
        return False, retry_after

    def reset(self) -> None:
        """Wipe all bucket state. Test helper; not used by production code."""
        self._buckets.clear()


# Module-level singleton used by the router. Tests may import this directly,
# call ``cron_rate_limiter.reset()`` in setup, or monkeypatch the binding.
cron_rate_limiter = CronRateLimiter()
