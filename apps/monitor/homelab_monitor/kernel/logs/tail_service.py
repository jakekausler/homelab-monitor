"""Live-tail SSE service (STAGE-004-023).

Pure, directly-unit-testable building blocks for the /api/logs/tail endpoint:

- ``ForwardCursor`` — forward (newest-direction) same-ns dedup cursor; the
  inverse of pagination.paginate_older's backward cursor.
- ``TailRegistry`` — event-loop-atomic global connection counter (the gauge's
  single source of truth).
- ``TailEvent`` union (``LineEvent`` / ``DroppedEvent`` / ``ErrorEvent`` /
  ``KeepaliveEvent``) yielded by the session.
- ``TailSession`` — owns the 1s poll loop, cursor dedup, per-second
  backpressure, duration cap, and VL-error retry. Writes counters directly.

The handler in routers/logs.py is a thin formatter over ``TailSession.events()``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import datetime

from homelab_monitor.kernel.config import TailConfig
from homelab_monitor.kernel.logs.models import LogLine, from_victorialogs_line
from homelab_monitor.kernel.logs.pagination import (  # private but importable, reused intentionally
    _iso_to_ns,  # pyright: ignore[reportPrivateUsage]
    _ns_to_iso,  # pyright: ignore[reportPrivateUsage]
)
from homelab_monitor.kernel.logs.victorialogs_client import (
    VictoriaLogsClient,
    VictoriaLogsClientError,
    VlLogLine,
)
from homelab_monitor.kernel.plugins.io import MetricsWriter

_METRIC_LINES_STREAMED = "homelab_log_tail_lines_streamed_total"
_METRIC_LINES_DROPPED = "homelab_log_tail_lines_dropped_total"
_METRIC_ERRORS = "homelab_log_tail_errors_total"

_KEEPALIVE_IDLE_S = 30.0  # emit a keepalive after this much idle
_VL_ERROR_BACKOFF_S = 10.0  # sleep after a VL error before retrying
_MAX_CONSECUTIVE_FAILURES = 5  # close the stream after this many VL failures in a row


@dataclass(slots=True, frozen=True)
class ForwardCursor:
    """Forward (newest-direction) dedup cursor for live tail.

    ``last_seen_ns`` — _time (ns) of the newest line already emitted.
    ``n_at_boundary`` — count of lines at exactly ``last_seen_ns`` already
    emitted (so the next poll, whose inclusive window starts at last_seen_ns,
    can drop the duplicates it re-fetches).
    """

    last_seen_ns: int
    n_at_boundary: int

    def advance(self, lines: list[VlLogLine]) -> tuple[list[VlLogLine], ForwardCursor]:
        """Return (newly-emittable lines ASCENDING by ns, advanced cursor).

        Algorithm (deterministic; mirrors paginate_older's same-ns handling but
        FORWARD):
          1. If ``lines`` is empty -> return ([], self).
          2. Compute ns for each line via _iso_to_ns (unparseable -> 0).
          3. SORT ascending by (ns, message, stream) so order is fully
             deterministic regardless of VL response order (ties broken by
             message then stream).
          4. Drop every line with ns < last_seen_ns (already past).
          5. Among lines with ns == last_seen_ns, drop the FIRST
             ``n_at_boundary`` of them (boundary dedup — already emitted).
          6. ``emit`` = the surviving lines (ns > last_seen_ns, plus any
             beyond-the-boundary same-ns lines), in ascending order.
          7. If ``emit`` is empty -> return ([], self) (cursor unchanged).
          8. new_last_seen_ns = max ns across the FULL sorted set (= ns of the
             last sorted line). new_n_at_boundary = count of lines in the FULL
             sorted set whose ns == new_last_seen_ns. (Count across the full set,
             NOT just emit, so next-poll dedup is correct even when some
             boundary lines were already dropped.)
          9. Return (emit, ForwardCursor(new_last_seen_ns, new_n_at_boundary)).
        """
        if not lines:
            return [], self

        decorated: list[tuple[int, str, str, VlLogLine]] = []
        for ln in lines:
            ns = _iso_to_ns(ln.timestamp)
            decorated.append((ns, ln.message, ln.stream, ln))
        # Sort by (ns, message, stream) — a fully deterministic tie-break. The
        # boundary dedup below relies on consecutive polls reproducing the SAME
        # order over the overlapping [last_seen, now] window, so the "first
        # n_at_boundary lines at last_seen_ns" are stably the already-emitted ones.
        decorated.sort(key=lambda t: (t[0], t[1], t[2]))

        max_ns = decorated[-1][0]
        n_at_max = sum(1 for d in decorated if d[0] == max_ns)

        emit: list[VlLogLine] = []
        boundary_skipped = 0
        for ns, _msg, _stream, ln in decorated:
            if ns < self.last_seen_ns:
                continue
            if ns == self.last_seen_ns:
                if boundary_skipped < self.n_at_boundary:
                    boundary_skipped += 1
                    continue
                emit.append(ln)
                continue
            # ns > last_seen_ns
            emit.append(ln)

        if not emit:
            return [], self

        return emit, ForwardCursor(last_seen_ns=max_ns, n_at_boundary=n_at_max)


class TailRegistry:
    """Event-loop-atomic global counter of active tail connections.

    Single source of truth for the homelab_log_tail_active_connections gauge.
    All methods are synchronous and contain NO awaits, so they are atomic on a
    single event loop (no lock needed). NOT thread-safe — the kernel runs one
    event loop per worker.
    """

    def __init__(self, *, max_connections: int) -> None:
        self._max = max_connections
        self._active = 0

    @property
    def active_count(self) -> int:
        """Current number of active tail connections."""
        return self._active

    @property
    def max_connections(self) -> int:
        """Configured global cap."""
        return self._max

    def try_acquire(self) -> bool:
        """Reserve a slot. Return False (no reservation) if at/over the cap."""
        if self._active >= self._max:
            return False
        self._active += 1
        return True

    def release(self) -> None:
        """Release a slot. Floors at 0 (defensive against double-release)."""
        if self._active > 0:
            self._active -= 1


@dataclass(slots=True, frozen=True)
class LineEvent:
    """A new log line to stream."""

    line: LogLine


@dataclass(slots=True, frozen=True)
class DroppedEvent:
    """N lines were dropped this second due to backpressure."""

    count: int


@dataclass(slots=True, frozen=True)
class ErrorEvent:
    """A VL error occurred mid-stream (the stream may continue or close)."""

    code: str
    message: str


@dataclass(slots=True, frozen=True)
class KeepaliveEvent:
    """Idle keepalive sentinel (the handler emits an SSE comment line)."""


TailEvent = LineEvent | DroppedEvent | ErrorEvent | KeepaliveEvent


class TailSession:
    """Owns the live-tail poll loop for ONE connection.

    Construction does NOT acquire a registry slot — the handler manages the
    registry (acquire-before-probe, release-in-finally). The session only
    polls + emits.
    """

    def __init__(
        self,
        *,
        vl_client: VictoriaLogsClient,
        expr: str,
        config: TailConfig,
        metrics_writer: MetricsWriter,
        clock: Callable[[], datetime],
    ) -> None:
        self._vl = vl_client
        self._expr = expr
        self._config = config
        self._metrics = metrics_writer
        self._clock = clock

    async def events(self) -> AsyncIterator[TailEvent]:
        """Yield TailEvents until the duration cap or 5 consecutive VL failures."""
        poll_s = self._config.poll_ms / 1000.0
        started = self._clock()
        # Round-trip datetime -> iso -> ns so the connection anchor sits on the
        # exact same ns grid as the per-poll query-window bounds (which are built
        # via _now_iso_at / iso). Lossless at datetime's microsecond precision.
        cursor = ForwardCursor(last_seen_ns=_iso_to_ns(_now_iso(self._clock)), n_at_boundary=0)
        consecutive_failures = 0
        last_emit = self._clock()  # for keepalive cadence
        budget_window_start = self._clock()  # for per-second backpressure
        emitted_this_second = 0

        while True:
            # 1. Sleep one poll interval FIRST. This guarantees end > cursor on
            #    the very first query (start==now at init; after the sleep, now
            #    has advanced by >= poll_s, so start < end holds).
            await asyncio.sleep(poll_s)

            now = self._clock()

            # 2. Duration cap -> close.
            if (now - started).total_seconds() >= self._config.max_duration_s:
                return

            # 3. Build the inclusive window [last_seen, now].
            start_iso = _ns_to_iso(cursor.last_seen_ns)
            end_iso = _now_iso_at(now)

            # 4. Query VL.
            try:
                result = await self._vl.query(expr=self._expr, start=start_iso, end=end_iso)
            except VictoriaLogsClientError as exc:
                consecutive_failures += 1
                kind = "vl_status" if exc.status_code is not None else "vl_transport"
                self._metrics.write_counter(_METRIC_ERRORS, 1.0, {"kind": kind})
                yield ErrorEvent(code="vl_unavailable", message=str(exc))
                if consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
                    return
                # The duration cap is only re-checked at the top of the loop, so
                # on a persistent-error path the stream can overrun max_duration_s
                # by up to _VL_ERROR_BACKOFF_S. Bounded + benign (5 consecutive
                # failures hard-close anyway).
                await asyncio.sleep(_VL_ERROR_BACKOFF_S)
                last_emit = (
                    self._clock()
                )  # ErrorEvent just emitted counts as activity -> suppresses keepalive
                continue

            consecutive_failures = 0

            # 5. Advance cursor, get newly-emittable lines (ascending).
            emit_lines, cursor = cursor.advance(result.lines)

            # 6. Per-second backpressure window reset.
            if (now - budget_window_start).total_seconds() >= 1.0:
                budget_window_start = now
                emitted_this_second = 0

            # 7. Apply the cap: a live tail wants the NEWEST lines, so when over
            #    budget keep the newest `remaining` lines and DROP the oldest
            #    surplus. emit_lines is ascending (oldest..newest), so the
            #    surplus to drop is the FRONT (oldest) of the list.
            # NOTE: remaining can be 0 when the per-second budget is already
            # exhausted -> ALL of this poll's lines are dropped and only a
            # DroppedEvent is yielded. "keep newest `remaining`" includes the
            # keep-zero case.
            remaining = self._config.max_lines_per_sec - emitted_this_second
            remaining = max(remaining, 0)
            if len(emit_lines) > remaining:
                surplus = len(emit_lines) - remaining
                emit_lines = emit_lines[surplus:]  # keep newest `remaining`
                self._metrics.write_counter(_METRIC_LINES_DROPPED, float(surplus), {})
                yield DroppedEvent(count=surplus)
                last_emit = self._clock()

            # 8. Emit kept lines.
            for vl_line in emit_lines:
                self._metrics.write_counter(_METRIC_LINES_STREAMED, 1.0, {})
                yield LineEvent(line=from_victorialogs_line(vl_line))
                emitted_this_second += 1
                last_emit = self._clock()

            # 9. Keepalive when idle >= 30s.
            if (self._clock() - last_emit).total_seconds() >= _KEEPALIVE_IDLE_S:
                yield KeepaliveEvent()
                last_emit = self._clock()


def _now_iso(clock: Callable[[], datetime]) -> str:
    """ISO-8601 string for the clock's current time (UTC-normalized upstream)."""
    return _now_iso_at(clock())


def _now_iso_at(dt: datetime) -> str:
    """ISO-8601 string for a specific datetime; used for VL window bounds."""
    return dt.isoformat()


__all__ = [
    "DroppedEvent",
    "ErrorEvent",
    "ForwardCursor",
    "KeepaliveEvent",
    "LineEvent",
    "TailEvent",
    "TailRegistry",
    "TailSession",
]
