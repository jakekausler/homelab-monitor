"""TTL-caching wrapper around AsyncSecretsRepository.

Maintains an in-memory SyncSecretsResolver snapshot with a 60-second TTL.
The refresh task wakes every ttl_seconds and swaps in a fresh snapshot.
Collectors read via current() which is synchronous (no await).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from homelab_monitor.kernel.secrets.resolver import SyncSecretsResolver

if TYPE_CHECKING:
    from structlog.stdlib import BoundLogger

    from homelab_monitor.kernel.secrets.repository import AsyncSecretsRepository


class TtlCachingSecretsResolver:
    """In-memory snapshot with TTL refresh.

    current() is sync — collectors call this every tick from ctx_factory
    without awaiting. The refresh task keeps the snapshot warm by waking
    every ttl_seconds and calling refresh_now().
    """

    def __init__(
        self,
        repo: AsyncSecretsRepository,
        *,
        ttl_seconds: float = 60.0,
        clock: Callable[[], float] | None = None,
        log: BoundLogger | None = None,
    ) -> None:
        """Initialize the resolver.

        Args:
            repo: AsyncSecretsRepository to snapshot from.
            ttl_seconds: How long to cache before refreshing (default 60).
            clock: Clock function for testing (default: asyncio.get_event_loop().time).
            log: Logger for refresh failures (optional).
        """
        self._repo = repo
        self._ttl_seconds = ttl_seconds
        self._log = log
        self._clock = clock if clock is not None else self._default_clock
        self._snapshot: SyncSecretsResolver | None = None
        self._cached_at: float = 0.0

    @staticmethod
    def _default_clock() -> float:
        """Default clock implementation using event loop time."""
        try:
            loop = asyncio.get_running_loop()
            return loop.time()
        except RuntimeError:  # pragma: no cover -- requires non-async context
            # Not in async context (e.g., in tests); use time.monotonic
            return time.monotonic()

    async def refresh_now(self) -> None:
        """Fetch a fresh snapshot from the repo and swap it in.

        If the fetch fails, the previous snapshot is retained. Called once
        at lifespan startup and then repeatedly by refresh_loop().
        """
        try:
            snapshot = await self._repo.snapshot()
            self._snapshot = snapshot
            self._cached_at = self._clock()
        except Exception:  # pragma: no cover -- defensive; snapshot never fails in tests
            if self._log is not None:  # pragma: no cover -- defensive check when log is None
                self._log.exception("ttl_resolver.refresh_failed")
            raise

    def current(self) -> SyncSecretsResolver:
        """Fetch the current snapshot (sync).

        May be stale by up to ttl_seconds; the refresh task keeps it warm.
        Raises RuntimeError if called before refresh_now() has been called at
        least once.
        """
        if self._snapshot is None:
            msg = "ttl_resolver not initialized; call refresh_now() first"
            raise RuntimeError(msg)
        return self._snapshot

    async def refresh_loop(self) -> None:
        """Run forever, refreshing every ttl_seconds.

        Re-raises ``asyncio.CancelledError`` so callers awaiting the task
        (e.g., lifespan shutdown) observe cancellation. Wrap the await in
        ``contextlib.suppress(asyncio.CancelledError)`` if you want to
        tolerate it silently.
        """
        while True:
            await asyncio.sleep(self._ttl_seconds)
            try:
                await self.refresh_now()
            except Exception:  # pragma: no cover
                if self._log is not None:
                    self._log.exception("ttl_resolver.refresh_failed")
