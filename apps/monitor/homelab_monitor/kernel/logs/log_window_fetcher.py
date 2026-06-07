"""LogWindowFetcher — TTL+LRU cache for anchor-centered VictoriaLogs windows.

STAGE-004-031. Fetches a [anchor - before, anchor + after] log window from
VictoriaLogs via stream_query, caches results by (expr, anchor_utc_iso,
window_before_s, window_after_s, limit) with a TTL+LRU eviction policy, and
degrades gracefully on VL error (degraded=True, empty lines, no cache).
"""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import cast

import structlog
from pydantic import BaseModel
from structlog.stdlib import BoundLogger

from homelab_monitor.kernel.logs.models import LogLine, from_victorialogs_line
from homelab_monitor.kernel.logs.pagination import _iso_to_ns  # pyright: ignore[reportPrivateUsage]
from homelab_monitor.kernel.logs.victorialogs_client import (
    VictoriaLogsClient,
    VictoriaLogsClientError,
    VlLogLine,
)

_MAX_WINDOW_SECONDS = 3600
_MAX_LIMIT = 1000

_log = structlog.get_logger(__name__)


def _default_wall_clock() -> datetime:
    return datetime.now(UTC)


class LogWindowResult(BaseModel):
    """Result of a log-window fetch."""

    lines: list[LogLine]
    truncated: bool
    degraded: bool = False
    window_start: datetime
    window_end: datetime
    queried_at: datetime


# Cache key: raw API args (clamping is deterministic from them)
_CacheKey = tuple[str, str, int, int, int]


@dataclass(slots=True)
class _CacheEntry:
    expires_at: float
    result: LogWindowResult


class _WindowCache:
    """OrderedDict-backed TTL + LRU cache for LogWindowResult.

    - ``clock``: monotonic float seconds, drives TTL expiry (lazy, on get).
    - ``max_entries``: LRU capacity; over-capacity evicts the least-recently-used
      entry via popitem(last=False).
    """

    def __init__(
        self,
        *,
        ttl_s: int,
        max_entries: int,
        clock: Callable[[], float],
    ) -> None:
        self._ttl = ttl_s
        self._max = max_entries
        self._clock = clock
        self._store: OrderedDict[_CacheKey, _CacheEntry] = OrderedDict()

    def get(self, key: _CacheKey) -> LogWindowResult | None:
        entry = self._store.get(key)
        if entry is None:
            return None
        if self._clock() >= entry.expires_at:
            del self._store[key]
            return None
        self._store.move_to_end(key)
        return entry.result

    def put(self, key: _CacheKey, result: LogWindowResult) -> None:
        self._store[key] = _CacheEntry(
            expires_at=self._clock() + self._ttl,
            result=result,
        )
        self._store.move_to_end(key)
        while len(self._store) > self._max:
            self._store.popitem(last=False)


class LogWindowFetcher:
    """Fetch and cache anchor-centered log windows from VictoriaLogs.

    Parameters
    ----------
    vl_client:
        VictoriaLogsClient to query.
    cache_ttl_s:
        Cache TTL in seconds (default 300).
    max_cache_entries:
        LRU eviction threshold (default 1000).
    clock:
        Monotonic float clock for TTL (default time.monotonic).
    wall_clock:
        datetime clock for queried_at + window math (default datetime.now(UTC)).
    log:
        Bound structlog logger; defaults to module logger.
    """

    def __init__(  # noqa: PLR0913
        self,
        vl_client: VictoriaLogsClient,
        *,
        cache_ttl_s: int = 300,
        max_cache_entries: int = 1000,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] = _default_wall_clock,
        log: BoundLogger | None = None,
    ) -> None:
        self._vl = vl_client
        self._wall_clock = wall_clock
        self._log: BoundLogger = log if log is not None else cast(BoundLogger, _log)
        self._cache = _WindowCache(
            ttl_s=cache_ttl_s,
            max_entries=max_cache_entries,
            clock=clock,
        )

    async def fetch(
        self,
        logs_ql: str,
        anchor_ts: datetime,
        window_before_s: int = 60,
        window_after_s: int = 60,
        limit: int = 200,
    ) -> LogWindowResult:
        """Fetch log lines centered on ``anchor_ts``.

        Returns a cached result (with original queried_at) on a cache HIT.
        Returns a degraded result (lines=[], degraded=True) on VL error — not cached.
        """
        # Normalize anchor to UTC
        if anchor_ts.tzinfo is None:
            anchor_utc = anchor_ts.replace(tzinfo=UTC)
        else:
            anchor_utc = anchor_ts.astimezone(UTC)

        # Cache key uses raw args (clamping is deterministic)
        cache_key: _CacheKey = (
            logs_ql,
            anchor_utc.isoformat(),
            window_before_s,
            window_after_s,
            limit,
        )
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        # Clamp window proportionally to 3600 s total
        before = window_before_s
        after = window_after_s
        total = before + after
        if total > _MAX_WINDOW_SECONDS:
            scale = _MAX_WINDOW_SECONDS / total
            before = int(before * scale)
            after = _MAX_WINDOW_SECONDS - before

        window_start = anchor_utc - timedelta(seconds=before)
        window_end = anchor_utc + timedelta(seconds=after)
        effective_limit = min(limit, _MAX_LIMIT)

        queried_at = self._wall_clock()

        try:
            # OVERSAMPLE: stream_query keeps the first N lines in VL's NEWEST-FIRST
            # order. For a one-sided window the lines nearest the anchor are NOT the
            # newest, so fetching only effective_limit+1 would miss them when the
            # window is dense. Fetch up to _MAX_LIMIT+1 so the anchor-adjacent lines
            # are within the fetched set WHEN the window holds <= _MAX_LIMIT+1 lines,
            # then sort + slice the correct side. (For a one-sided window denser than
            # _MAX_LIMIT+1, VL returns the newest cap and the AFTER-side's oldest
            # anchor-adjacent lines may fall outside it; `truncated=True` signals this.)
            # (No LogsQL `sort` — it OOMs; see pagination.py.)
            fetch_cap = _MAX_LIMIT + 1
            raw: list[VlLogLine] = []
            async for vl_line in self._vl.stream_query(
                expr=logs_ql,
                start=window_start.isoformat(),
                end=window_end.isoformat(),
                limit=fetch_cap,
            ):
                raw.append(vl_line)

            # Sort ascending by _time (ns). Memoize the parse per timestamp string
            # (pagination.paginate_older precedent): same-ts lines share one parse.
            _ns_cache: dict[str, int] = {}

            def _ns(line: VlLogLine) -> int:
                ts = line.timestamp
                cached_ns = _ns_cache.get(ts)
                if cached_ns is None:
                    cached_ns = _iso_to_ns(ts)
                    _ns_cache[ts] = cached_ns
                return cached_ns

            ordered = sorted(raw, key=_ns)

            # Side-aware selection: keep the effective_limit lines NEAREST the anchor.
            # AFTER-side  (before==0, after>0): anchor at window START → keep OLDEST
            #   effective_limit  (the FIRST after ascending sort).
            # BEFORE-side (after==0, before>0): anchor at window END → keep NEWEST
            #   effective_limit  (the LAST after ascending sort).
            # SYMMETRIC (both>0 or both==0): keep the FIRST effective_limit (legacy).
            truncated = len(ordered) > effective_limit
            if window_before_s > 0 and window_after_s == 0:
                kept = ordered[-effective_limit:] if effective_limit > 0 else []
            else:
                kept = ordered[:effective_limit]

            collected: list[LogLine] = [from_victorialogs_line(vl_line) for vl_line in kept]

            result = LogWindowResult(
                lines=collected,
                truncated=truncated,
                degraded=False,
                window_start=window_start,
                window_end=window_end,
                queried_at=queried_at,
            )
            self._cache.put(cache_key, result)
            return result

        except VictoriaLogsClientError as exc:
            self._log.warning(
                "log_window_fetcher.degraded",
                error=str(exc),
            )
            return LogWindowResult(
                lines=[],
                truncated=False,
                degraded=True,
                window_start=window_start,
                window_end=window_end,
                queried_at=queried_at,
            )


__all__ = ["LogWindowFetcher", "LogWindowResult"]
