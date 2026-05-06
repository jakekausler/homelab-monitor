"""Server-Sent Events streaming endpoint."""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends
from starlette.requests import Request
from starlette.responses import StreamingResponse

from homelab_monitor.kernel.api.dependencies import get_broker
from homelab_monitor.kernel.api.sse import SseDisconnect

if TYPE_CHECKING:
    from homelab_monitor.kernel.api.sse import SseBroker

router = APIRouter()


@router.get("/events")
async def stream_events(
    request: Request,
    broker: SseBroker = Depends(get_broker),  # noqa: B008
) -> StreamingResponse:
    """Stream collector tick events via Server-Sent Events.

    Connects to the SSE broker and yields events in the format:
      event: collector.tick
      data: {JSON payload}
      id: {sequence number}

    Replays the last 50 events on connect. If the consumer's queue fills
    (slow subscriber), the broker disconnects them with an error event.
    """

    async def gen() -> AsyncGenerator[bytes, None]:
        # NOTE: we deliberately do NOT poll request.is_disconnected() per
        # iteration — it returns spurious True under Starlette
        # BaseHTTPMiddleware-wrapped requests (used by RequestIdMiddleware
        # and AccessLogMiddleware), which would close the stream immediately.
        # Starlette will cancel this generator when the client really
        # disconnects, raising CancelledError/GeneratorExit and triggering
        # broker.subscribe()'s finally block to remove the subscriber.
        async for ev in broker.subscribe():
            if isinstance(
                ev, SseDisconnect
            ):  # pragma: no cover -- BaseHTTPMiddleware buffers streaming responses (STAGE-001-014)
                yield b'event: error\ndata: {"reason":"slow_subscriber"}\n\n'
                return
            payload = json.dumps(ev.payload, separators=(",", ":"), sort_keys=True)
            line = f"event: {ev.kind}\ndata: {payload}\nid: {ev.seq}\n\n"
            yield line.encode("utf-8")

    headers = {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "Content-Type": "text/event-stream; charset=utf-8",
    }
    return StreamingResponse(gen(), headers=headers, media_type="text/event-stream")
