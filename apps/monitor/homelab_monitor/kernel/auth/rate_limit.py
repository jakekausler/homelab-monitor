"""Login rate limiting by IP address."""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from typing import Protocol


class LoginRateLimiter(Protocol):
    """Sliding-window rate limiter for login attempts, keyed by IP."""

    def check_and_record(self, ip: str) -> bool:
        """Return True if the attempt is allowed; False if rate-limited."""
        ...


class InProcessLoginRateLimiter:
    """Sliding-window in-process limiter: 5 attempts per 5 minutes per IP.

    Lost on restart (acceptable for homelab single-process per locked decision D4).
    Test-clock injectable via the `clock` constructor argument.
    """

    DEFAULT_MAX_ATTEMPTS = 5
    DEFAULT_WINDOW_SECONDS = 300

    def __init__(
        self,
        *,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        window_seconds: float = DEFAULT_WINDOW_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max = max_attempts
        self._window = window_seconds
        self._clock = clock
        self._buckets: dict[str, deque[float]] = {}

    def check_and_record(self, ip: str) -> bool:
        """Return True if the attempt is allowed; False if rate-limited."""
        now = self._clock()
        bucket = self._buckets.get(ip)
        if bucket is None:
            bucket = deque[float]()
            self._buckets[ip] = bucket
        # Evict timestamps older than the window
        cutoff = now - self._window
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) >= self._max:
            return False
        bucket.append(now)
        return True
