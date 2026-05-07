"""Server-Sent Events streaming endpoint."""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends
from starlette.requests import Request
from starlette.responses import StreamingResponse

from homelab_monitor.kernel.api.dependencies import get_broker, require_session
from homelab_monitor.kernel.api.sse import SseDisconnect, SseKeepalive
from homelab_monitor.kernel.auth.models import User

if TYPE_CHECKING:
    from homelab_monitor.kernel.api.sse import SseBroker

router = APIRouter()

KEEPALIVE_INTERVAL_S = 15.0


@router.get("/events")
async def stream_events(
    request: Request,
    _user: User = Depends(require_session()),  # noqa: B008
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
        # iteration. Starlette will cancel this generator when the client
        # really disconnects (uvicorn delivers http.disconnect on the receive
        # channel; Starlette's StreamingResponse listen_for_disconnect task
        # then cancels the task group), raising CancelledError/GeneratorExit
        # and triggering broker.subscribe()'s finally block to remove the
        # subscriber. After STAGE-001-014 (pure-ASGI middleware), the previous
        # spurious-disconnect issue under BaseHTTPMiddleware no longer applies,
        # but polling is still unnecessary because Starlette handles the
        # cancellation correctly on real disconnect.
        # Keepalive is implemented INSIDE broker.subscribe() (yielding an
        # SseKeepalive sentinel on idle timeout) rather than wrapping
        # __anext__ in asyncio.wait_for here. wait_for cancels the awaited
        # coroutine on timeout, and that cancellation unwinds the
        # subscribe() async-generator's `finally` block, which discards the
        # queue from broker._subscribers. The result was that every 15s of
        # idle silently terminated the subscription. Letting subscribe()
        # own its own timeout keeps the generator frame alive across
        # keepalives.
        async for ev in broker.subscribe(keepalive_interval=KEEPALIVE_INTERVAL_S):
            if isinstance(ev, SseKeepalive):
                # Emit an SSE comment line. Comments start with ':' and are ignored
                # by EventSource clients but flush bytes through reverse proxies
                # (nginx, Cloudflare) preventing idle-close before the next real
                # event.
                yield b": keepalive\n\n"
                continue
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
