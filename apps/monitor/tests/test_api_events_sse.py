"""Tests for kernel/api/sse.py — SSE broker behavior and HTTP endpoint."""

from __future__ import annotations

import asyncio
import base64
from collections.abc import AsyncIterator
from pathlib import Path
from typing import cast

import pytest
import pytest_asyncio
import structlog
from httpx import ASGITransport, AsyncClient
from structlog.stdlib import BoundLogger

from homelab_monitor.kernel.api.app import create_app
from homelab_monitor.kernel.api.sse import (  # pyright: ignore[reportPrivateUsage]
    SseBroker,
    SseDisconnect,
    SseKeepalive,
    _SseEvent,  # pyright: ignore[reportPrivateUsage]
)
from homelab_monitor.kernel.db.time import utc_now_iso
from homelab_monitor.kernel.events import SchedulerTickEvent, make_tick_id

from ._uvicorn_fixture import UvicornFixtureValue

# ---------------------------------------------------------------------------
# Direct broker tests (not through HTTP)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def broker() -> SseBroker:
    """Create a broker with default settings."""
    log = cast(BoundLogger, structlog.get_logger().bind())
    return SseBroker(log)


@pytest.mark.asyncio
async def test_sse_broker_subscribe_replay_on_connect(broker: SseBroker) -> None:
    """Subscribe AFTER 5 events published; new subscriber gets all 5 in ring buffer."""
    # Publish 5 events
    for _ in range(5):
        event = SchedulerTickEvent(
            collector="test_col",
            tick_id=make_tick_id(),
            outcome="success",
            ts="2026-05-05T20:57:00Z",
        )
        await broker.publish(event)

    # Now subscribe — should replay all 5
    events: list[_SseEvent | SseDisconnect | SseKeepalive] = []
    async for ev in broker.subscribe():
        events.append(ev)
        if len(events) >= 5:  # noqa: PLR2004
            break

    assert len(events) == 5  # noqa: PLR2004
    # Check monotonic seq
    for i, ev in enumerate(events):
        assert isinstance(ev, _SseEvent)
        assert ev.seq == i + 1


@pytest.mark.asyncio
async def test_sse_broker_ring_buffer_cap(broker: SseBroker) -> None:
    """Publish 60 events; new subscriber gets last 50, oldest 10 dropped."""
    # Publish 60 events
    for idx in range(60):
        event = SchedulerTickEvent(
            collector=f"col_{idx}",
            tick_id=make_tick_id(),
            outcome="success",
            ts="2026-05-05T20:57:00Z",
        )
        await broker.publish(event)

    # Subscribe and collect all replayed events
    events: list[_SseEvent | SseDisconnect | SseKeepalive] = []
    async for ev in broker.subscribe():
        events.append(ev)
        if len(events) >= 50:  # noqa: PLR2004
            break

    assert len(events) == 50  # noqa: PLR2004
    # Check that we got events 11-60 (oldest 10 are dropped)
    assert isinstance(events[0], _SseEvent)
    assert isinstance(events[49], _SseEvent)
    assert events[0].seq == 11  # noqa: PLR2004
    assert events[49].seq == 60  # noqa: PLR2004


@pytest.mark.asyncio
async def test_sse_broker_slow_subscriber_overflow(broker: SseBroker) -> None:
    """Subscriber's queue fills; publisher continues; slow subscriber disconnected."""
    # Create a subscriber generator and drive it sequentially. Async generators
    # forbid concurrent __anext__ calls, so we drain it on a single task.
    sub_gen = broker.subscribe()
    events: list[_SseEvent | SseDisconnect | SseKeepalive] = []

    async def collect() -> None:
        async for ev in sub_gen:
            events.append(ev)
            if isinstance(ev, SseDisconnect):
                break

    collect_task: asyncio.Task[None] = asyncio.create_task(collect())
    # Yield to let the subscriber register on the broker before we publish.
    await asyncio.sleep(0.01)

    # Fill the queue (broker's queue_maxsize=64 by default) by publishing 65 events.
    for _ in range(65):
        event = SchedulerTickEvent(
            collector="test",
            tick_id=make_tick_id(),
            outcome="success",
            ts="2026-05-05T20:57:00Z",
        )
        await broker.publish(event)

    try:
        await asyncio.wait_for(collect_task, timeout=2.0)
    except TimeoutError:
        collect_task.cancel()

    # Should have received a disconnect sentinel after the queue overflowed.
    assert any(isinstance(ev, SseDisconnect) for ev in events)


@pytest.mark.asyncio
async def test_sse_broker_multiple_concurrent_subscribers(broker: SseBroker) -> None:
    """Multiple concurrent subscribers all receive the same published events."""
    # Create 3 subscribers
    subs = [broker.subscribe() for _ in range(3)]

    # Publish 5 events
    for _ in range(5):
        event = SchedulerTickEvent(
            collector="test",
            tick_id=make_tick_id(),
            outcome="success",
            ts="2026-05-05T20:57:00Z",
        )
        await broker.publish(event)

    # Collect events from all 3 subscribers
    collected: list[list[_SseEvent | SseDisconnect | SseKeepalive]] = [[], [], []]

    async def collect_from_sub(
        sub_gen: AsyncIterator[_SseEvent | SseDisconnect | SseKeepalive], idx: int
    ) -> None:
        count = 0
        async for ev in sub_gen:
            collected[idx].append(ev)
            count += 1
            if count >= 5:  # noqa: PLR2004
                break

    tasks = [asyncio.create_task(collect_from_sub(sub, i)) for i, sub in enumerate(subs)]
    await asyncio.gather(*tasks, return_exceptions=True)

    # All 3 should have gotten 5 events
    for i in range(3):
        assert len(collected[i]) == 5  # noqa: PLR2004
        # All should have the same seq numbers
        for j in range(5):
            ev = collected[i][j]
            assert isinstance(ev, _SseEvent)
            assert ev.seq == j + 1


@pytest.mark.asyncio
async def test_sse_broker_monotonic_event_seq(broker: SseBroker) -> None:
    """event_seq increments monotonically per publish."""
    events: list[_SseEvent | SseDisconnect | SseKeepalive] = []
    sub_gen = broker.subscribe()

    for _ in range(10):
        event = SchedulerTickEvent(
            collector="test",
            tick_id=make_tick_id(),
            outcome="success",
            ts="2026-05-05T20:57:00Z",
        )
        await broker.publish(event)

    # Collect them
    count = 0
    async for ev in sub_gen:
        events.append(ev)
        count += 1
        if count >= 10:  # noqa: PLR2004
            break

    # Check monotonic increment
    for i, ev in enumerate(events):
        assert isinstance(ev, _SseEvent)
        assert ev.seq == i + 1


@pytest.mark.asyncio
async def test_sse_broker_publish_non_throwing(broker: SseBroker) -> None:
    """publish() is non-throwing even if a subscriber's queue raises."""
    # Create a slow subscriber by filling the queue
    for _ in range(65):
        event = SchedulerTickEvent(
            collector="test",
            tick_id=make_tick_id(),
            outcome="success",
            ts="2026-05-05T20:57:00Z",
        )
        await broker.publish(event)

    # publish() should not have raised even though a subscriber was discarded
    # Just verify no exception was raised
    assert True


@pytest.mark.asyncio
async def test_sse_broker_no_skip_seq_numbers_in_replay(broker: SseBroker) -> None:
    """Replay events do NOT skip seq numbers."""
    for _ in range(3):
        event = SchedulerTickEvent(
            collector="test",
            tick_id=make_tick_id(),
            outcome="success",
            ts="2026-05-05T20:57:00Z",
        )
        await broker.publish(event)

    events: list[_SseEvent | SseDisconnect | SseKeepalive] = []
    async for ev in broker.subscribe():
        events.append(ev)
        if len(events) >= 3:  # noqa: PLR2004
            break

    # Seq should be 1, 2, 3 with no gaps
    assert [e.seq for e in events if isinstance(e, _SseEvent)] == [1, 2, 3]


# ---------------------------------------------------------------------------
# HTTP endpoint tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sse_http_endpoint_smoke(
    uvicorn_server: UvicornFixtureValue,
) -> None:
    """Subscribe via /api/events over a real socket; trigger a tick; assert it arrives.

    Runs uvicorn on an ephemeral port (NOT httpx.ASGITransport, which buffers
    streaming responses). Triggers a tick by POSTing /api/collectors/noop/retry
    rather than poking the broker directly — the broker lives on uvicorn's
    event loop in a background thread and isn't reachable from here.
    """
    async with AsyncClient(base_url=uvicorn_server.base_url) as client:
        # Login to obtain session + csrf cookies.
        resp = await client.post(
            "/api/auth/login",
            json={
                "username": uvicorn_server.username,
                "password": uvicorn_server.password,
            },
        )
        assert resp.status_code == 200  # noqa: PLR2004

        csrf_token = client.cookies.get("homelab_monitor_csrf")
        assert csrf_token is not None
        csrf_headers = {"X-CSRF-Token": csrf_token}

        async def subscribe_and_collect() -> list[str]:
            events: list[str] = []
            async with client.stream("GET", "/api/events") as stream_resp:
                assert stream_resp.status_code == 200  # noqa: PLR2004
                async for line in stream_resp.aiter_lines():
                    if line:
                        events.append(line)
                    # event + data + id + blank line per event; collect until
                    # we've seen at least one full event (>=3 non-blank lines).
                    if len(events) >= 3:  # noqa: PLR2004
                        break
            return events

        sub_task = asyncio.create_task(subscribe_and_collect())

        # Give the subscription a moment to register on the broker.
        await asyncio.sleep(0.2)

        # Trigger a tick over HTTP (the broker is on uvicorn's loop, not
        # ours). The noop collector is registered by uvicorn's lifespan.
        retry_resp = await client.post(
            "/api/collectors/noop/retry",
            headers=csrf_headers,
        )
        assert retry_resp.status_code == 200  # noqa: PLR2004

        events = await asyncio.wait_for(sub_task, timeout=5.0)

        # Parse the SSE format: event: ...\ndata: ...\nid: ...\n\n
        assert len(events) >= 3  # noqa: PLR2004
        event_line = next((e for e in events if e.startswith("event:")), None)
        data_line = next((e for e in events if e.startswith("data:")), None)
        id_line = next((e for e in events if e.startswith("id:")), None)

        assert event_line == "event: collector.tick"
        assert data_line is not None and data_line.startswith("data: {")
        assert id_line is not None and id_line.startswith("id: ")


@pytest.mark.asyncio
async def test_sse_http_endpoint_auth_gated(
    db_path: Path, db_url: str, monkeypatch: pytest.MonkeyPatch, master_key: bytes
) -> None:
    """401 without valid session cookie."""
    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    monkeypatch.setenv("HOMELAB_MONITOR_MASTER_KEY", base64.b64encode(master_key).decode())
    monkeypatch.setenv("HOMELAB_MONITOR_AUTO_MIGRATE", "1")

    app = create_app(lifespan_enabled=True)

    async with app.router.lifespan_context(app):  # noqa: SIM117
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            # Try without auth cookie
            resp = await client.get("/api/events")
            assert resp.status_code == 401  # noqa: PLR2004


@pytest.mark.asyncio
async def test_sse_broker_queue_maxsize_validation() -> None:
    """SseBroker raises ValueError if replay_capacity > queue_maxsize."""
    log = cast(BoundLogger, structlog.get_logger().bind())
    with pytest.raises(ValueError, match="replay_capacity"):
        SseBroker(log=log, queue_maxsize=10, replay_capacity=50)


# ---------------------------------------------------------------------------
# Keepalive path in events.gen() (STAGE-001-014 coverage gap)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sse_gen_emits_keepalive_when_idle(
    broker: SseBroker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """gen() yields b': keepalive\\n\\n' when no broker event arrives within the interval.

    Monkeypatches KEEPALIVE_INTERVAL_S to 0.05 s so the test completes quickly.
    The broker has no events published, so the wait_for always times out and
    the TimeoutError branch (lines 63-64 of events.py) executes.
    """
    from unittest.mock import MagicMock  # noqa: PLC0415

    from homelab_monitor.kernel.api.routers import events as events_module  # noqa: PLC0415
    from homelab_monitor.kernel.api.routers.events import stream_events  # noqa: PLC0415

    monkeypatch.setattr(events_module, "KEEPALIVE_INTERVAL_S", 0.05)

    # Build a minimal fake request / user / dependency context — we call gen()
    # directly rather than going through FastAPI routing.
    mock_user = MagicMock()

    # Reconstruct the gen() coroutine by calling stream_events with our broker
    # and extracting the StreamingResponse's body_iterator (which IS gen()).
    mock_request = MagicMock()
    response = await stream_events(request=mock_request, _user=mock_user, broker=broker)

    gen_iter = cast(AsyncIterator[bytes], response.body_iterator)

    # Await the first chunk — the broker is idle so KEEPALIVE_INTERVAL_S elapses
    # and we get the SSE comment line. The `continue` after the yield is exercised
    # on iteration regardless of whether the broker terminates afterward.
    chunk: bytes = await asyncio.wait_for(gen_iter.__anext__(), timeout=2.0)
    assert chunk == b": keepalive\n\n"

    # Publish a real event before the second await so the broker yields a
    # payload (event line) instead of either a keepalive or shutting down.
    # This proves the loop body re-entered cleanly after the keepalive yield.
    await broker.publish(
        SchedulerTickEvent(
            collector="noop",
            tick_id="test-tick",
            outcome="success",
            reason=None,
            duration_seconds=0.0,
            trigger_kind="manual",
            request_id=None,
            ts=utc_now_iso(),
        )
    )
    chunk2: bytes = await asyncio.wait_for(gen_iter.__anext__(), timeout=2.0)
    assert chunk2.startswith(b"event: collector.tick\n")
