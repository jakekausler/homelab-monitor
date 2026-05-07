"""SSE broker implementing EventSink Protocol."""

from __future__ import annotations

import asyncio
import collections
import contextlib
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from structlog.stdlib import BoundLogger

from homelab_monitor.kernel.events import BaseEvent

# Public type alias documenting the SSE event payload shape; today this is a
# JSON dict produced by SchedulerTickEvent.model_dump(mode="json").
type SseEventPayload = dict[str, Any]


@dataclass(frozen=True, slots=True)
class _SseEvent:
    """Internal SSE event wrapper."""

    seq: int
    kind: str
    payload: SseEventPayload


class SseDisconnect:
    """Sentinel value pushed to a subscriber queue to signal disconnection.

    Public so downstream consumers (SSE router) can isinstance-check it
    without crossing a private-name boundary.
    """


_DISCONNECT = SseDisconnect()


class SseBroker:
    """SSE broker implementing EventSink.

    Maintains a ring buffer of recent events for replay-on-connect and
    distributes new events to active subscribers. Subscribers with full
    queues are disconnected with a sentinel event.

    Per locked decision D2:
    - queue_maxsize: 64 (backpressure threshold)
    - replay_capacity: 50 (events replayed on connect)
    """

    def __init__(
        self,
        log: BoundLogger,
        *,
        queue_maxsize: int = 64,
        replay_capacity: int = 50,
    ) -> None:
        """Initialize the broker.

        Args:
            log: Logger for errors and debug info.
            queue_maxsize: Max items per subscriber queue (default 64).
            replay_capacity: Size of ring buffer for replay (default 50).
        """
        if replay_capacity > queue_maxsize:
            msg = f"replay_capacity ({replay_capacity}) must be <= queue_maxsize ({queue_maxsize})"
            raise ValueError(msg)
        self._log = log
        self._subscribers: set[asyncio.Queue[_SseEvent | SseDisconnect]] = set()
        self._ring: collections.deque[_SseEvent] = collections.deque(maxlen=replay_capacity)
        self._seq: int = 0
        self._queue_maxsize = queue_maxsize
        self._lock = asyncio.Lock()

    async def publish(self, event: BaseEvent) -> None:
        """Publish an event (scheduler tick or alert) to all subscribers.

        The event's ``kind`` discriminator is propagated to subscribers so the
        SSE consumer can route by event type.

        Non-throwing per Protocol contract. Exceptions are caught and logged.

        NOTE (F9): each ``publish()`` call grabs ``self._lock`` for fan-out.
        Under burst load (50+ alerts in one Alertmanager batch), the ingest
        loop serialises on this lock. Acceptable for v1; revisit if alerts/sec
        ever exceeds ~100. TODO(future): batch publish() into a producer
        queue and drain on a single fan-out task.
        """
        try:
            async with self._lock:
                self._seq += 1
                wrapped = _SseEvent(
                    seq=self._seq,
                    kind=event.kind,
                    payload=event.model_dump(mode="json"),
                )
                self._ring.append(wrapped)
                dead: list[asyncio.Queue[_SseEvent | SseDisconnect]] = []
                for q in self._subscribers:
                    try:
                        q.put_nowait(wrapped)
                    except asyncio.QueueFull:
                        dead.append(q)
                # Drop dead subscribers first; then attempt to push the disconnect
                # sentinel best-effort. The queue is full by definition so a normal
                # put_nowait would raise QueueFull again; instead, drain one item
                # to make room for the sentinel so the consumer learns it was kicked.
                for q in dead:
                    self._subscribers.discard(q)
                    # Make room for the sentinel by dropping the oldest queued event.
                    with contextlib.suppress(asyncio.QueueEmpty):
                        q.get_nowait()
                    # Subscriber consumer is gone; sentinel is best-effort.
                    with contextlib.suppress(
                        asyncio.QueueFull
                    ):  # pragma: no cover -- broker fail-safe; QueueFull re-raised by put_nowait
                        q.put_nowait(_DISCONNECT)
        except Exception:  # pragma: no cover -- broker fail-safe; tested via 7 unit tests
            self._log.exception("sse_broker.publish_failed")

    async def subscribe(self) -> AsyncIterator[_SseEvent | SseDisconnect]:
        """Subscribe to the event stream.

        Yields recent events from the ring buffer first (replay), then
        waits for new events. If the consumer's queue fills, the broker
        disconnects them with a sentinel.
        """
        q: asyncio.Queue[_SseEvent | SseDisconnect] = asyncio.Queue(maxsize=self._queue_maxsize)
        # Replay on connect. Ring buffer length is bounded by replay_capacity,
        # which the constructor MUST keep <= queue_maxsize so put_nowait can't
        # raise QueueFull during replay. A defensive assertion catches any
        # future config mistake in tests.
        assert self._ring.maxlen is not None and self._ring.maxlen <= self._queue_maxsize, (
            "replay_capacity must be <= queue_maxsize"
        )
        async with self._lock:
            for ev in list(self._ring):
                q.put_nowait(ev)
            self._subscribers.add(q)
        try:
            while True:
                ev = await q.get()
                yield ev
        finally:
            async with self._lock:
                self._subscribers.discard(q)
