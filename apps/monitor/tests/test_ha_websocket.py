"""Tests for HomeAssistantWebsocketClient + Subscription (STAGE-005-002).

A scripted fake WS connection (injected via the ``connect`` param) drives the
demux / handshake / reconnect / intent-replay paths without real network or real
sleeps. Covers the full HaError mapping, the bounded-queue drop-oldest path, the
token-never-logged discipline, and clean shutdown.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import cast

import pytest
import structlog
from structlog.stdlib import BoundLogger
from structlog.testing import capture_logs

from homelab_monitor.kernel.ha import HomeAssistantWebsocketClient, Subscription
from homelab_monitor.kernel.ha.errors import HaError
from homelab_monitor.kernel.ha.websocket import (
    _default_connect,  # pyright: ignore[reportPrivateUsage]
)
from homelab_monitor.kernel.plugins.io import InMemoryMetricsWriter

_HTTP_BAD_GATEWAY = 502
_COMMAND_TIMEOUT_SECONDS = 30.0
_EXPECTED_RECONNECT_COUNT = 1
_ANEXT_WAIT_FOR_CALL_COUNT = 2
_AUTH_ERROR_COUNT_EXPECTED = 2
_UNMAPPED_SUBSCRIPTION_ID = 999
_TOKEN = "super-secret-ha-token-xyz"


def _log() -> BoundLogger:
    return cast(BoundLogger, structlog.get_logger().bind(component="test"))


class FakeWsConnection:
    """A scriptable fake websockets connection.

    ``inbound`` is an asyncio.Queue of raw str frames the client will ``recv`` /
    iterate. ``sent`` records every frame the client sent (as parsed dicts).
    ``closed`` flips True on ``close``. Test helpers push frames; a ``None``
    sentinel pushed to ``inbound`` ends the async-iteration (simulates the
    socket closing). ``recv_error`` (if set) is raised by the NEXT recv/iter.
    """

    def __init__(self) -> None:
        self.inbound: asyncio.Queue[str | None] = asyncio.Queue()
        self.sent: list[dict[str, object]] = []
        self.closed: bool = False
        self.recv_error: Exception | None = None

    async def send(self, message: str) -> None:
        self.sent.append(cast("dict[str, object]", json.loads(message)))

    async def recv(self) -> str:
        if self.recv_error is not None:
            err, self.recv_error = self.recv_error, None
            raise err
        item = await self.inbound.get()
        if item is None:
            raise ConnectionError("socket closed")
        return item

    async def __anext__(self) -> str:
        if self.recv_error is not None:
            err, self.recv_error = self.recv_error, None
            raise err
        item = await self.inbound.get()
        if item is None:
            raise StopAsyncIteration
        return item

    def __aiter__(self) -> AsyncIterator[str]:
        return self

    async def close(self) -> None:
        self.closed = True

    # ---- test push helpers ----
    def push(self, frame: dict[str, object]) -> None:
        self.inbound.put_nowait(json.dumps(frame))

    def push_raw(self, raw: str) -> None:
        self.inbound.put_nowait(raw)

    def push_close(self) -> None:
        self.inbound.put_nowait(None)


class ScriptedConnector:
    """Injectable ``connect`` that hands out queued FakeWsConnection objects.

    Each ``connect`` call pops the next pre-seeded connection (so a test can
    script connection #1, force a drop, and script connection #2 for reconnect).
    Records ws_urls it was called with.
    """

    def __init__(self, *conns: FakeWsConnection) -> None:
        self._conns: list[FakeWsConnection] = list(conns)
        self.urls: list[str] = []
        self.connect_attempts: int = 0

    async def __call__(self, ws_url: str) -> FakeWsConnection:
        self.urls.append(ws_url)
        self.connect_attempts += 1
        if not self._conns:
            # No more scripted conns: block forever (test cancels via stop_task).
            await asyncio.Event().wait()
        return self._conns.pop(0)


def _seed_auth_ok(conn: FakeWsConnection) -> None:
    """Pre-seed the handshake frames for a successful auth."""
    conn.push({"type": "auth_required"})
    conn.push({"type": "auth_ok"})


def _client(
    connector: ScriptedConnector,
    *,
    token: str | None = _TOKEN,
    base_url: str = "http://ha.local:8123",
) -> tuple[HomeAssistantWebsocketClient, InMemoryMetricsWriter]:
    metrics = InMemoryMetricsWriter()
    client = HomeAssistantWebsocketClient(
        base_url=base_url,
        token_provider=lambda: token,
        metrics_writer=metrics,
        log=_log(),
        connect=connector,
    )
    return client, metrics


async def _wait_connected(client: HomeAssistantWebsocketClient, *, timeout: float = 2.0) -> None:
    """Spin the event loop until the client reports connected."""
    async with asyncio.timeout(timeout):
        while not client.connected:
            await asyncio.sleep(0)


async def _wait_until(predicate: Callable[[], bool], *, timeout: float = 2.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0)


def _gauge_value(metrics: InMemoryMetricsWriter, name: str) -> float | None:
    for entry in reversed(metrics.recorded):
        if entry.name == name:
            return entry.value
    return None


def _counter_count(metrics: InMemoryMetricsWriter, name: str, **labels: str) -> int:
    n = 0
    for entry in metrics.recorded:
        if (
            entry.name == name
            and entry.kind == "counter"
            and all(entry.labels.get(k) == v for k, v in labels.items())
        ):
            n += 1
    return n


# ---- ws_url derivation ----


def test_ws_url_http_to_ws() -> None:
    client, _ = _client(ScriptedConnector(), base_url="http://ha.local:8123")
    assert client._ws_url == "ws://ha.local:8123/api/websocket"  # pyright: ignore[reportPrivateUsage]


def test_ws_url_https_to_wss() -> None:
    client, _ = _client(ScriptedConnector(), base_url="https://ha.local:8123")
    assert client._ws_url == "wss://ha.local:8123/api/websocket"  # pyright: ignore[reportPrivateUsage]


def test_ws_url_trailing_slash_stripped() -> None:
    client, _ = _client(ScriptedConnector(), base_url="http://ha.local:8123/")
    assert client._ws_url == "ws://ha.local:8123/api/websocket"  # pyright: ignore[reportPrivateUsage]


def test_ws_url_no_scheme() -> None:
    client, _ = _client(ScriptedConnector(), base_url="ha.local:8123")
    assert client._ws_url == "ws://ha.local:8123/api/websocket"  # pyright: ignore[reportPrivateUsage]


# ---- handshake ----


@pytest.mark.asyncio
async def test_auth_handshake_success_sets_connected() -> None:
    conn = FakeWsConnection()
    _seed_auth_ok(conn)
    client, metrics = _client(ScriptedConnector(conn))
    client.start_task()
    await _wait_connected(client)
    # Auth frame sent with the token, and connected gauge = 1.
    assert conn.sent == [{"type": "auth", "access_token": _TOKEN}]
    assert _gauge_value(metrics, "homelab_ha_websocket_connected") == 1.0
    assert _counter_count(metrics, "homelab_ha_websocket_reconnect_total") == 0
    await client.stop_task()


@pytest.mark.asyncio
async def test_auth_invalid_loops_to_backoff_and_emits_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn1 = FakeWsConnection()
    conn1.push({"type": "auth_required"})
    conn1.push({"type": "auth_invalid"})
    conn2 = (
        FakeWsConnection()
    )  # second connect blocks (ScriptedConnector exhausted? no, 2nd present)
    _seed_auth_ok(conn2)
    # Patch sleep so backoff is instant.
    monkeypatch.setattr(asyncio, "sleep", _instant_sleep())
    client, metrics = _client(ScriptedConnector(conn1, conn2))
    with capture_logs() as captured:
        client.start_task()
        await _wait_connected(client)  # recovers on conn2
    assert _counter_count(metrics, "homelab_ha_websocket_error_total", reason="auth") == 1
    # Transition log emitted once, and token NEVER appears in any log.
    assert any(e.get("event") == "ha_websocket.auth_failed" for e in captured)
    assert not any(_TOKEN in json.dumps(e) for e in captured)
    await client.stop_task()


@pytest.mark.asyncio
async def test_no_token_no_auth_frame_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    conn1 = FakeWsConnection()
    conn1.push({"type": "auth_required"})
    conn2 = FakeWsConnection()
    _seed_auth_ok(conn2)
    monkeypatch.setattr(asyncio, "sleep", _instant_sleep())
    # First connect has no token; flip token to present after first attempt.
    state: dict[str, str | None] = {"token": None}
    metrics = InMemoryMetricsWriter()
    client = HomeAssistantWebsocketClient(
        base_url="http://ha.local:8123",
        token_provider=lambda: state["token"],
        metrics_writer=metrics,
        log=_log(),
        connect=ScriptedConnector(conn1, conn2),
    )
    with capture_logs() as captured:
        client.start_task()
        await _wait_until(lambda: conn1.closed)  # first attempt aborted (no token)
        state["token"] = _TOKEN  # now allow auth on reconnect
        await _wait_connected(client)
    # No auth frame was ever sent on conn1.
    assert conn1.sent == []
    assert _counter_count(metrics, "homelab_ha_websocket_error_total", reason="auth") >= 1
    assert not any(_TOKEN in json.dumps(e) for e in captured)
    await client.stop_task()


@pytest.mark.asyncio
async def test_unexpected_first_frame_is_bad_response(monkeypatch: pytest.MonkeyPatch) -> None:
    conn1 = FakeWsConnection()
    conn1.push({"type": "not_auth_required"})  # protocol violation
    conn2 = FakeWsConnection()
    _seed_auth_ok(conn2)
    monkeypatch.setattr(asyncio, "sleep", _instant_sleep())
    client, metrics = _client(ScriptedConnector(conn1, conn2))
    client.start_task()
    await _wait_connected(client)
    assert _counter_count(metrics, "homelab_ha_websocket_error_total", reason="bad_response") == 1
    await client.stop_task()


# ---- send_command ----


@pytest.mark.asyncio
async def test_send_command_success_returns_payload() -> None:
    conn = FakeWsConnection()
    _seed_auth_ok(conn)
    client, _ = _client(ScriptedConnector(conn))
    client.start_task()
    await _wait_connected(client)
    # Drive the command in a task; resolve it by pushing a result frame.
    cmd = asyncio.ensure_future(client.send_command("get_states"))
    await _wait_until(lambda: any(s.get("type") == "get_states" for s in conn.sent))
    sent_id = next(s["id"] for s in conn.sent if s.get("type") == "get_states")
    conn.push({"id": sent_id, "type": "result", "success": True, "result": {"k": "v"}})
    result = await cmd
    assert result == {"k": "v"}
    await client.stop_task()


@pytest.mark.asyncio
async def test_send_command_success_non_dict_result_returns_empty() -> None:
    conn = FakeWsConnection()
    _seed_auth_ok(conn)
    client, _ = _client(ScriptedConnector(conn))
    client.start_task()
    await _wait_connected(client)
    cmd = asyncio.ensure_future(client.send_command("ping"))
    await _wait_until(lambda: any(s.get("type") == "ping" for s in conn.sent))
    sent_id = next(s["id"] for s in conn.sent if s.get("type") == "ping")
    conn.push({"id": sent_id, "type": "result", "success": True, "result": None})
    assert await cmd == {}
    await client.stop_task()


@pytest.mark.asyncio
async def test_send_command_success_list_result_returns_list() -> None:
    """config_entries/get returns a top-level JSON array; send_command passes the list through."""
    conn = FakeWsConnection()
    _seed_auth_ok(conn)
    client, _ = _client(ScriptedConnector(conn))
    client.start_task()
    await _wait_connected(client)
    cmd = asyncio.ensure_future(client.send_command("config_entries/get"))
    await _wait_until(lambda: any(s.get("type") == "config_entries/get" for s in conn.sent))
    sent_id = next(s["id"] for s in conn.sent if s.get("type") == "config_entries/get")
    conn.push({"id": sent_id, "type": "result", "success": True, "result": [{"a": 1}, {"b": 2}]})
    result = await cmd
    assert result == [{"a": 1}, {"b": 2}]
    await client.stop_task()


@pytest.mark.asyncio
async def test_send_command_failure_with_code_is_http_error() -> None:
    conn = FakeWsConnection()
    _seed_auth_ok(conn)
    client, _ = _client(ScriptedConnector(conn))
    client.start_task()
    await _wait_connected(client)
    cmd = asyncio.ensure_future(client.send_command("bad"))
    await _wait_until(lambda: any(s.get("type") == "bad" for s in conn.sent))
    sent_id = next(s["id"] for s in conn.sent if s.get("type") == "bad")
    conn.push(
        {"id": sent_id, "type": "result", "success": False, "error": {"code": 502, "message": "x"}}
    )
    result = await cmd
    assert isinstance(result, HaError)
    assert result.reason == "http_error"
    assert result.status == _HTTP_BAD_GATEWAY
    await client.stop_task()


@pytest.mark.asyncio
async def test_send_command_failure_no_code_is_bad_response() -> None:
    conn = FakeWsConnection()
    _seed_auth_ok(conn)
    client, _ = _client(ScriptedConnector(conn))
    client.start_task()
    await _wait_connected(client)
    cmd = asyncio.ensure_future(client.send_command("bad"))
    await _wait_until(lambda: any(s.get("type") == "bad" for s in conn.sent))
    sent_id = next(s["id"] for s in conn.sent if s.get("type") == "bad")
    conn.push({"id": sent_id, "type": "result", "success": False})
    result = await cmd
    assert isinstance(result, HaError)
    assert result.reason == "bad_response"
    await client.stop_task()


@pytest.mark.asyncio
async def test_send_command_not_connected_is_unreachable() -> None:
    client, _ = _client(ScriptedConnector())  # never started
    result = await client.send_command("get_states")
    assert isinstance(result, HaError)
    assert result.reason == "unreachable"


@pytest.mark.asyncio
async def test_send_command_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = FakeWsConnection()
    _seed_auth_ok(conn)
    client, _ = _client(ScriptedConnector(conn))
    client.start_task()
    await _wait_connected(client)

    # Make wait_for time out immediately for the command future.
    real_wait_for = asyncio.wait_for

    async def fake_wait_for(awaitable: Awaitable[object], timeout: float) -> object:
        if timeout == _COMMAND_TIMEOUT_SECONDS:
            # Cancel the awaitable and raise as a real timeout would.
            fut = asyncio.ensure_future(awaitable)
            fut.cancel()
            raise TimeoutError
        return await real_wait_for(awaitable, timeout)

    monkeypatch.setattr(asyncio, "wait_for", fake_wait_for)
    result = await client.send_command("slow")
    assert isinstance(result, HaError)
    assert result.reason == "timeout"
    await client.stop_task()


@pytest.mark.asyncio
async def test_send_command_send_failure_is_unreachable() -> None:
    conn = FakeWsConnection()
    _seed_auth_ok(conn)
    client, _ = _client(ScriptedConnector(conn))
    client.start_task()
    await _wait_connected(client)

    async def boom(_message: str) -> None:
        raise ConnectionError("send failed")

    conn.send = boom  # type: ignore[method-assign]
    result = await client.send_command("get_states")
    assert isinstance(result, HaError)
    assert result.reason == "unreachable"
    await client.stop_task()


# ---- subscribe / event flow ----


@pytest.mark.asyncio
async def test_subscribe_then_event_reaches_async_for() -> None:
    conn = FakeWsConnection()
    _seed_auth_ok(conn)
    client, _ = _client(ScriptedConnector(conn))
    client.start_task()
    await _wait_connected(client)
    sub_task = asyncio.ensure_future(client.subscribe("subscribe_events"))
    await _wait_until(lambda: any(s.get("type") == "subscribe_events" for s in conn.sent))
    sub_id = next(s["id"] for s in conn.sent if s.get("type") == "subscribe_events")
    conn.push({"id": sub_id, "type": "result", "success": True})
    sub = await sub_task
    assert isinstance(sub, Subscription)
    conn.push({"id": sub_id, "type": "event", "event": {"e": 1}})
    received = await anext(aiter(sub))
    assert received == {"e": 1}
    await client.stop_task()


@pytest.mark.asyncio
async def test_subscribe_failure_returns_haerror_no_intent() -> None:
    conn = FakeWsConnection()
    _seed_auth_ok(conn)
    client, _ = _client(ScriptedConnector(conn))
    client.start_task()
    await _wait_connected(client)
    sub_task = asyncio.ensure_future(client.subscribe("subscribe_events"))
    await _wait_until(lambda: any(s.get("type") == "subscribe_events" for s in conn.sent))
    sub_id = next(s["id"] for s in conn.sent if s.get("type") == "subscribe_events")
    conn.push({"id": sub_id, "type": "result", "success": False})
    result = await sub_task
    assert isinstance(result, HaError)
    assert client._intents == []  # pyright: ignore[reportPrivateUsage]
    await client.stop_task()


@pytest.mark.asyncio
async def test_subscribe_not_connected_is_unreachable() -> None:
    client, _ = _client(ScriptedConnector())
    result = await client.subscribe("subscribe_events")
    assert isinstance(result, HaError)
    assert result.reason == "unreachable"


@pytest.mark.asyncio
async def test_unsubscribe_drops_intent_and_sends_frame() -> None:
    conn = FakeWsConnection()
    _seed_auth_ok(conn)
    client, _ = _client(ScriptedConnector(conn))
    client.start_task()
    await _wait_connected(client)
    sub_task = asyncio.ensure_future(client.subscribe("subscribe_events"))
    await _wait_until(lambda: any(s.get("type") == "subscribe_events" for s in conn.sent))
    sub_id = next(s["id"] for s in conn.sent if s.get("type") == "subscribe_events")
    conn.push({"id": sub_id, "type": "result", "success": True})
    sub = await sub_task
    assert isinstance(sub, Subscription)
    unsub_task = asyncio.ensure_future(client.unsubscribe(sub))
    await _wait_until(lambda: any(s.get("type") == "unsubscribe_events" for s in conn.sent))
    unsub_id = next(s["id"] for s in conn.sent if s.get("type") == "unsubscribe_events")
    conn.push({"id": unsub_id, "type": "result", "success": True})
    await unsub_task
    assert client._intents == []  # pyright: ignore[reportPrivateUsage]
    await client.stop_task()


# ---- reconnect + intent replay ----


@pytest.mark.asyncio
async def test_reconnect_replays_subscription_same_handle(monkeypatch: pytest.MonkeyPatch) -> None:
    conn1 = FakeWsConnection()
    _seed_auth_ok(conn1)
    conn2 = FakeWsConnection()
    _seed_auth_ok(conn2)
    monkeypatch.setattr(asyncio, "sleep", _instant_sleep())
    client, metrics = _client(ScriptedConnector(conn1, conn2))
    client.start_task()
    await _wait_connected(client)
    sub_task = asyncio.ensure_future(client.subscribe("subscribe_events"))
    await _wait_until(lambda: any(s.get("type") == "subscribe_events" for s in conn1.sent))
    sub_id1 = next(s["id"] for s in conn1.sent if s.get("type") == "subscribe_events")
    conn1.push({"id": sub_id1, "type": "result", "success": True})
    sub = await sub_task
    assert isinstance(sub, Subscription)
    # Force a drop: end conn1's iteration -> run loop reconnects to conn2.
    conn1.push_close()
    await _wait_until(lambda: any(s.get("type") == "subscribe_events" for s in conn2.sent))
    # Replay re-subscribed on conn2 (fresh id). Confirm it, push an event, same handle gets it.
    sub_id2 = next(s["id"] for s in conn2.sent if s.get("type") == "subscribe_events")
    conn2.push({"id": sub_id2, "type": "result", "success": True})
    await _wait_until(lambda: sub.id == sub_id2)
    conn2.push({"id": sub_id2, "type": "event", "event": {"after": "reconnect"}})
    received = await anext(aiter(sub))
    assert received == {"after": "reconnect"}
    assert (
        _counter_count(metrics, "homelab_ha_websocket_reconnect_total") == _EXPECTED_RECONNECT_COUNT
    )
    await client.stop_task()


@pytest.mark.asyncio
async def test_reconnect_clears_stale_subscription_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per-connection _subscriptions.clear() prevents stale-entry accumulation.

    After reconnect with ONE active intent, len(_subscriptions) must be 1 (not 2).
    """
    conn1 = FakeWsConnection()
    _seed_auth_ok(conn1)
    conn2 = FakeWsConnection()
    _seed_auth_ok(conn2)
    monkeypatch.setattr(asyncio, "sleep", _instant_sleep())
    client, _ = _client(ScriptedConnector(conn1, conn2))
    client.start_task()
    await _wait_connected(client)
    # Subscribe one intent on conn1.
    sub_task = asyncio.ensure_future(client.subscribe("subscribe_events"))
    await _wait_until(lambda: any(s.get("type") == "subscribe_events" for s in conn1.sent))
    sub_id1 = next(s["id"] for s in conn1.sent if s.get("type") == "subscribe_events")
    conn1.push({"id": sub_id1, "type": "result", "success": True})
    sub = await sub_task
    assert isinstance(sub, Subscription)
    # Force reconnect.
    conn1.push_close()
    await _wait_until(lambda: any(s.get("type") == "subscribe_events" for s in conn2.sent))
    sub_id2 = next(s["id"] for s in conn2.sent if s.get("type") == "subscribe_events")
    conn2.push({"id": sub_id2, "type": "result", "success": True})
    await _wait_until(lambda: sub.id == sub_id2)
    # After replay, _subscriptions must contain exactly 1 entry (not 2).
    assert len(client._subscriptions) == 1  # pyright: ignore[reportPrivateUsage]
    assert client._subscriptions[cast(int, sub_id2)] is sub  # pyright: ignore[reportPrivateUsage]
    await client.stop_task()


@pytest.mark.asyncio
async def test_stale_event_id_after_reconnect_is_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Events carrying an unmapped id are not routed to any subscription.

    After _subscriptions.clear() on reconnect, only replayed subscription ids
    are registered. An event with an id that was never issued on the new
    connection (e.g. _UNMAPPED_SUBSCRIPTION_ID) must be silently dropped.
    """
    conn1 = FakeWsConnection()
    _seed_auth_ok(conn1)
    conn2 = FakeWsConnection()
    _seed_auth_ok(conn2)
    monkeypatch.setattr(asyncio, "sleep", _instant_sleep())
    client, _ = _client(ScriptedConnector(conn1, conn2))
    client.start_task()
    await _wait_connected(client)
    # Subscribe on conn1.
    sub_task = asyncio.ensure_future(client.subscribe("subscribe_events"))
    await _wait_until(lambda: any(s.get("type") == "subscribe_events" for s in conn1.sent))
    sub_id1 = next(s["id"] for s in conn1.sent if s.get("type") == "subscribe_events")
    conn1.push({"id": sub_id1, "type": "result", "success": True})
    sub = await sub_task
    assert isinstance(sub, Subscription)
    # Force reconnect.
    conn1.push_close()
    await _wait_until(lambda: any(s.get("type") == "subscribe_events" for s in conn2.sent))
    sub_id2 = next(s["id"] for s in conn2.sent if s.get("type") == "subscribe_events")
    conn2.push({"id": sub_id2, "type": "result", "success": True})
    await _wait_until(lambda: sub.id == sub_id2)
    # Push an event on conn2 using an id that was never issued on conn2 (unmapped
    # after _subscriptions.clear()). _dispatch_frame must drop it silently.
    conn2.push({"id": _UNMAPPED_SUBSCRIPTION_ID, "type": "event", "event": {"stale": True}})
    # Push a valid ping so we know conn2 has processed frames past the unmapped one.
    cmd = asyncio.ensure_future(client.send_command("ping"))
    await _wait_until(lambda: any(s.get("type") == "ping" for s in conn2.sent))
    ping_id = next(s["id"] for s in conn2.sent if s.get("type") == "ping")
    conn2.push({"id": ping_id, "type": "result", "success": True, "result": {}})
    await cmd
    # The unmapped-id event must NOT have reached the subscription queue.
    assert sub.queue.empty()
    await client.stop_task()


@pytest.mark.asyncio
async def test_disconnect_resolves_pending_with_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn1 = FakeWsConnection()
    _seed_auth_ok(conn1)
    conn2 = FakeWsConnection()
    _seed_auth_ok(conn2)
    monkeypatch.setattr(asyncio, "sleep", _instant_sleep())
    client, _ = _client(ScriptedConnector(conn1, conn2))
    client.start_task()
    await _wait_connected(client)
    # Issue a command but never answer it; then drop the socket.
    cmd = asyncio.ensure_future(client.send_command("get_states"))
    await _wait_until(lambda: any(s.get("type") == "get_states" for s in conn1.sent))
    conn1.push_close()  # disconnect mid-flight
    result = await cmd
    assert isinstance(result, HaError)
    assert result.reason == "unreachable"
    await client.stop_task()


# ---- bounded queue drop-oldest ----


@pytest.mark.asyncio
async def test_event_queue_full_drops_oldest_and_logs() -> None:
    conn = FakeWsConnection()
    _seed_auth_ok(conn)
    client, _ = _client(ScriptedConnector(conn))
    client.start_task()
    await _wait_connected(client)
    sub_task = asyncio.ensure_future(client.subscribe("subscribe_events"))
    await _wait_until(lambda: any(s.get("type") == "subscribe_events" for s in conn.sent))
    sub_id = next(s["id"] for s in conn.sent if s.get("type") == "subscribe_events")
    conn.push({"id": sub_id, "type": "result", "success": True})
    sub = await sub_task
    assert isinstance(sub, Subscription)
    with capture_logs() as captured:
        # Push 257 events (queue maxsize 256) -> one drop-oldest.
        for i in range(257):
            conn.push({"id": sub_id, "type": "event", "event": {"n": i}})
        await _wait_until(sub.queue.full)
        # Wait for the 257th to be processed (full -> drop -> put).
        await _wait_until(
            lambda: any(e.get("event") == "ha_websocket.event_queue_full" for e in captured)
        )
    assert sub.queue.full()
    await client.stop_task()


# ---- malformed frames ----


@pytest.mark.asyncio
async def test_non_json_frame_is_ignored() -> None:
    conn = FakeWsConnection()
    _seed_auth_ok(conn)
    client, _ = _client(ScriptedConnector(conn))
    client.start_task()
    await _wait_connected(client)
    conn.push_raw("not json {{{")  # bad frame -> logged + skipped
    conn.push_raw("123")  # valid JSON but not an object -> skipped
    # Client stays connected after junk.
    cmd = asyncio.ensure_future(client.send_command("ping"))
    await _wait_until(lambda: any(s.get("type") == "ping" for s in conn.sent))
    pid = next(s["id"] for s in conn.sent if s.get("type") == "ping")
    conn.push({"id": pid, "type": "result", "success": True, "result": {}})
    assert await cmd == {}
    await client.stop_task()


@pytest.mark.asyncio
async def test_unhandled_frame_type_is_ignored() -> None:
    conn = FakeWsConnection()
    _seed_auth_ok(conn)
    client, _ = _client(ScriptedConnector(conn))
    client.start_task()
    await _wait_connected(client)
    conn.push({"type": "pong"})  # no id, unknown type -> debug log, ignored
    conn.push({"type": "result"})  # result without id -> ignored
    conn.push({"type": "event"})  # event without id -> ignored
    cmd = asyncio.ensure_future(client.send_command("ping"))
    await _wait_until(lambda: any(s.get("type") == "ping" for s in conn.sent))
    pid = next(s["id"] for s in conn.sent if s.get("type") == "ping")
    conn.push({"id": pid, "type": "result", "success": True, "result": {}})
    assert await cmd == {}
    await client.stop_task()


@pytest.mark.asyncio
async def test_event_for_unknown_subscription_id_is_ignored() -> None:
    conn = FakeWsConnection()
    _seed_auth_ok(conn)
    client, _ = _client(ScriptedConnector(conn))
    client.start_task()
    await _wait_connected(client)
    conn.push({"id": 9999, "type": "event", "event": {"x": 1}})  # no such sub
    cmd = asyncio.ensure_future(client.send_command("ping"))
    await _wait_until(lambda: any(s.get("type") == "ping" for s in conn.sent))
    pid = next(s["id"] for s in conn.sent if s.get("type") == "ping")
    conn.push({"id": pid, "type": "result", "success": True, "result": {}})
    assert await cmd == {}
    await client.stop_task()


@pytest.mark.asyncio
async def test_event_without_event_key_falls_back_to_frame() -> None:
    conn = FakeWsConnection()
    _seed_auth_ok(conn)
    client, _ = _client(ScriptedConnector(conn))
    client.start_task()
    await _wait_connected(client)
    sub_task = asyncio.ensure_future(client.subscribe("subscribe_events"))
    await _wait_until(lambda: any(s.get("type") == "subscribe_events" for s in conn.sent))
    sub_id = next(s["id"] for s in conn.sent if s.get("type") == "subscribe_events")
    conn.push({"id": sub_id, "type": "result", "success": True})
    sub = await sub_task
    assert isinstance(sub, Subscription)
    conn.push({"id": sub_id, "type": "event"})  # no "event" key -> whole frame enqueued
    received = await anext(aiter(sub))
    assert received["type"] == "event"
    await client.stop_task()


# ---- backoff increment + reset ----


@pytest.mark.asyncio
async def test_backoff_increments_then_resets_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    # conn1 fails the handshake (bad first frame). conn2 succeeds.
    conn1 = FakeWsConnection()
    conn1.push({"type": "wrong"})  # triggers _WsClosed(bad_response)
    conn2 = FakeWsConnection()
    _seed_auth_ok(conn2)
    sleeps: list[float] = []

    async def record_sleep(d: float) -> None:
        # Record only real backoff sleeps; the 0-delay yields from the test
        # helpers (_wait_connected) must not pollute the recorded list.
        if d:
            sleeps.append(d)
        # MUST yield to the loop so the background _run_loop task progresses
        # and asyncio.timeout(...) deadlines can fire. A bare append never
        # suspends, which deadlocks _wait_connected's `await asyncio.sleep(0)`.
        await _yield_once()

    monkeypatch.setattr(asyncio, "sleep", record_sleep)
    client, _ = _client(ScriptedConnector(conn1, conn2))
    client.start_task()
    await _wait_connected(client)
    # First failure backed off at 1.0; after success backoff reset to 1.0.
    assert sleeps and sleeps[0] == 1.0
    assert client._backoff == 1.0  # pyright: ignore[reportPrivateUsage]
    await client.stop_task()


# ---- clean shutdown ----


@pytest.mark.asyncio
async def test_stop_task_cancels_cleanly() -> None:
    conn = FakeWsConnection()
    _seed_auth_ok(conn)
    client, metrics = _client(ScriptedConnector(conn))
    client.start_task()
    await _wait_connected(client)
    await client.stop_task()  # must not raise
    assert client._task is None  # pyright: ignore[reportPrivateUsage]
    assert _gauge_value(metrics, "homelab_ha_websocket_connected") == 0.0


@pytest.mark.asyncio
async def test_stop_task_idempotent_when_never_started() -> None:
    client, _ = _client(ScriptedConnector())
    await client.stop_task()  # no task -> no-op, no raise
    assert client._task is None  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_start_task_idempotent() -> None:
    conn = FakeWsConnection()
    _seed_auth_ok(conn)
    client, _ = _client(ScriptedConnector(conn))
    client.start_task()
    first = client._task  # pyright: ignore[reportPrivateUsage]
    client.start_task()  # second call -> same task, no new one
    assert client._task is first  # pyright: ignore[reportPrivateUsage]
    await client.stop_task()


# ---- exports ----


def test_exports_present() -> None:
    from homelab_monitor.kernel import ha  # noqa: PLC0415

    assert "HomeAssistantWebsocketClient" in ha.__all__
    assert "Subscription" in ha.__all__


# ---- Subscription.__anext__ coverage ----

_ANEXT_TIMEOUT_SECONDS = 1.0


@pytest.mark.asyncio
async def test_anext_raises_stop_when_closed_and_empty() -> None:
    """Line 133-134: closed + empty queue -> StopAsyncIteration ends async-for."""
    sub = Subscription(type_="subscribe_events", fields={})
    sub._closed = True  # pyright: ignore[reportPrivateUsage]
    # Queue is empty and closed: __anext__ must raise StopAsyncIteration.
    with pytest.raises(StopAsyncIteration):
        await sub.__anext__()


@pytest.mark.asyncio
async def test_anext_timeout_continues_then_returns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Lines 138-139: wait_for TimeoutError -> continue; next iteration returns item."""
    sub = Subscription(type_="subscribe_events", fields={})
    real_wait_for = asyncio.wait_for
    call_count = 0

    async def fake_wait_for(awaitable: Awaitable[object], timeout: float) -> object:
        nonlocal call_count
        if timeout == _ANEXT_TIMEOUT_SECONDS:
            call_count += 1
            if call_count == 1:
                # First call: cancel the awaitable and raise TimeoutError.
                fut = asyncio.ensure_future(awaitable)
                fut.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await fut
                raise TimeoutError
            # Second call: deliver the item by putting it in the queue first.
            sub.queue.put_nowait({"x": 2})
            return await real_wait_for(sub.queue.get(), timeout=timeout)
        return await real_wait_for(awaitable, timeout)

    monkeypatch.setattr(asyncio, "wait_for", fake_wait_for)
    result = await sub.__anext__()
    assert result == {"x": 2}
    assert call_count == _ANEXT_WAIT_FOR_CALL_COUNT  # exactly 2 wait_for calls


# ---- unsubscribe never-registered path ----


@pytest.mark.asyncio
async def test_unsubscribe_never_registered_sub_closes_cleanly() -> None:
    """Lines 274→276: sub not in _intents and sub.id is None -> just sets _closed."""
    conn = FakeWsConnection()
    _seed_auth_ok(conn)
    client, _ = _client(ScriptedConnector(conn))
    client.start_task()
    await _wait_connected(client)
    # A Subscription that was never registered via subscribe().
    sub = Subscription(type_="subscribe_events", fields={})
    assert sub.id is None
    assert sub not in client._intents  # pyright: ignore[reportPrivateUsage]
    await client.unsubscribe(sub)
    # Should have set _closed, sent nothing extra beyond auth.
    assert sub._closed is True  # pyright: ignore[reportPrivateUsage]
    unsubscribe_frames = [s for s in conn.sent if s.get("type") == "unsubscribe_events"]
    assert unsubscribe_frames == []
    await client.stop_task()


# ---- _do_subscribe send failure ----


@pytest.mark.asyncio
async def test_subscribe_send_failure_returns_unreachable() -> None:
    """Lines 297-300: send raises during _do_subscribe -> HaError(unreachable)."""
    conn = FakeWsConnection()
    _seed_auth_ok(conn)
    client, _ = _client(ScriptedConnector(conn))
    client.start_task()
    await _wait_connected(client)

    async def boom(_message: str) -> None:
        raise ConnectionError("send failed")

    conn.send = boom  # type: ignore[method-assign]
    result = await client.subscribe("subscribe_events")
    assert isinstance(result, HaError)
    assert result.reason == "unreachable"
    await client.stop_task()


# ---- _do_subscribe timeout ----


@pytest.mark.asyncio
async def test_subscribe_timeout_returns_timeout_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Lines 303-306: wait_for times out waiting for subscribe confirm -> HaError(timeout)."""
    conn = FakeWsConnection()
    _seed_auth_ok(conn)
    client, _ = _client(ScriptedConnector(conn))
    client.start_task()
    await _wait_connected(client)

    real_wait_for = asyncio.wait_for

    async def fake_wait_for(awaitable: Awaitable[object], timeout: float) -> object:
        if timeout == _COMMAND_TIMEOUT_SECONDS:
            fut = asyncio.ensure_future(awaitable)
            fut.cancel()
            raise TimeoutError
        return await real_wait_for(awaitable, timeout)

    monkeypatch.setattr(asyncio, "wait_for", fake_wait_for)
    result = await client.subscribe("subscribe_events")
    assert isinstance(result, HaError)
    assert result.reason == "timeout"
    await client.stop_task()


# ---- first frame not a dict ----


@pytest.mark.asyncio
async def test_non_dict_first_frame_is_bad_response(monkeypatch: pytest.MonkeyPatch) -> None:
    """Lines 401-402: first frame decodes to non-dict -> _WsClosed(bad_response)."""
    conn1 = FakeWsConnection()
    conn1.push_raw("[1, 2, 3]")  # valid JSON but a list, not a dict
    conn2 = FakeWsConnection()
    _seed_auth_ok(conn2)
    monkeypatch.setattr(asyncio, "sleep", _instant_sleep())
    client, metrics = _client(ScriptedConnector(conn1, conn2))
    client.start_task()
    await _wait_connected(client)
    assert _counter_count(metrics, "homelab_ha_websocket_error_total", reason="bad_response") == 1
    await client.stop_task()


# ---- second frame non-dict -> auth failure ----


@pytest.mark.asyncio
async def test_non_dict_second_frame_triggers_auth_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Lines 415→419: second frame not a dict -> isinstance check False -> _AuthFailure."""
    conn1 = FakeWsConnection()
    conn1.push({"type": "auth_required"})
    conn1.push_raw('"not_a_dict"')  # JSON string, not a dict
    conn2 = FakeWsConnection()
    _seed_auth_ok(conn2)
    monkeypatch.setattr(asyncio, "sleep", _instant_sleep())
    client, metrics = _client(ScriptedConnector(conn1, conn2))
    client.start_task()
    await _wait_connected(client)
    assert _counter_count(metrics, "homelab_ha_websocket_error_total", reason="auth") == 1
    await client.stop_task()


# ---- result frame with already-done future ----


@pytest.mark.asyncio
async def test_result_frame_for_already_done_future_is_ignored() -> None:
    """Line 451→ branch: result frame dispatched when pending future is already done."""
    conn = FakeWsConnection()
    _seed_auth_ok(conn)
    client, _ = _client(ScriptedConnector(conn))
    client.start_task()
    await _wait_connected(client)

    # Issue a command to get a real pending future registered.
    cmd = asyncio.ensure_future(client.send_command("ping"))
    await _wait_until(lambda: any(s.get("type") == "ping" for s in conn.sent))
    sent_id = next(s["id"] for s in conn.sent if s.get("type") == "ping")

    # Resolve it once.
    conn.push({"id": sent_id, "type": "result", "success": True, "result": {}})
    assert await cmd == {}

    # Push a second result frame for the same id (future already resolved + popped).
    # _dispatch_frame: fut = _pending.pop(mid) -> None (already popped); no crash.
    conn.push({"id": sent_id, "type": "result", "success": True, "result": {"late": True}})
    # Client stays healthy: can still process a new command.
    cmd2 = asyncio.ensure_future(client.send_command("ping2"))
    await _wait_until(lambda: any(s.get("type") == "ping2" for s in conn.sent))
    sent_id2 = next(s["id"] for s in conn.sent if s.get("type") == "ping2")
    conn.push({"id": sent_id2, "type": "result", "success": True, "result": {"ok": True}})
    assert await cmd2 == {"ok": True}
    await client.stop_task()


# ---- _result_payload: error present but not a dict ----


@pytest.mark.asyncio
async def test_send_command_failure_error_not_dict_is_bad_response() -> None:
    """Lines 474→476: error key present but not a dict -> code stays None -> bad_response."""
    conn = FakeWsConnection()
    _seed_auth_ok(conn)
    client, _ = _client(ScriptedConnector(conn))
    client.start_task()
    await _wait_connected(client)
    cmd = asyncio.ensure_future(client.send_command("bad"))
    await _wait_until(lambda: any(s.get("type") == "bad" for s in conn.sent))
    sent_id = next(s["id"] for s in conn.sent if s.get("type") == "bad")
    # error is a string, not a dict -> isinstance(err, dict) is False -> code=None -> bad_response.
    conn.push({"id": sent_id, "type": "result", "success": False, "error": "boom"})
    result = await cmd
    assert isinstance(result, HaError)
    assert result.reason == "bad_response"
    await client.stop_task()


@pytest.mark.asyncio
async def test_send_command_failure_error_dict_non_int_code_is_bad_response() -> None:
    """Lines 474→476: error is dict but code is not int -> code stays None -> bad_response."""
    conn = FakeWsConnection()
    _seed_auth_ok(conn)
    client, _ = _client(ScriptedConnector(conn))
    client.start_task()
    await _wait_connected(client)
    cmd = asyncio.ensure_future(client.send_command("bad"))
    await _wait_until(lambda: any(s.get("type") == "bad" for s in conn.sent))
    sent_id = next(s["id"] for s in conn.sent if s.get("type") == "bad")
    # error is a dict but code is a string, not int -> isinstance(raw_code, int) False.
    conn.push({"id": sent_id, "type": "result", "success": False, "error": {"code": "not_an_int"}})
    result = await cmd
    assert isinstance(result, HaError)
    assert result.reason == "bad_response"
    await client.stop_task()


# ---- _enqueue_event: event key present but not a dict ----


@pytest.mark.asyncio
async def test_event_key_non_dict_falls_back_to_frame() -> None:
    """Lines 495→494: event key present but not a dict -> payload = frame."""
    conn = FakeWsConnection()
    _seed_auth_ok(conn)
    client, _ = _client(ScriptedConnector(conn))
    client.start_task()
    await _wait_connected(client)
    sub_task = asyncio.ensure_future(client.subscribe("subscribe_events"))
    await _wait_until(lambda: any(s.get("type") == "subscribe_events" for s in conn.sent))
    sub_id = next(s["id"] for s in conn.sent if s.get("type") == "subscribe_events")
    conn.push({"id": sub_id, "type": "result", "success": True})
    sub = await sub_task
    assert isinstance(sub, Subscription)
    # event key present but value is a string, not a dict -> fallback to whole frame.
    conn.push({"id": sub_id, "type": "event", "event": "not_a_dict"})
    received = await anext(aiter(sub))
    assert received["type"] == "event"
    assert received["event"] == "not_a_dict"
    await client.stop_task()


# ---- _default_connect: real library seam ----

_DEFAULT_CONNECT_URL = "ws://example.local/api/websocket"
_FAKE_WS_SENTINEL = object()


@pytest.mark.asyncio
async def test_default_connect_uses_websockets_connect(monkeypatch: pytest.MonkeyPatch) -> None:
    """Lines 96-99: _default_connect calls websockets.asyncio.client.connect and returns its result.

    The import is function-local, so we patch the source attribute directly:
    ``websockets.asyncio.client.connect``.  The cast on line 99 is a no-op at
    runtime, so the returned object IS the sentinel that ``fake_connect`` returned.
    """

    async def fake_connect(url: str) -> object:
        assert url == _DEFAULT_CONNECT_URL
        return _FAKE_WS_SENTINEL

    import websockets.asyncio.client  # noqa: PLC0415

    monkeypatch.setattr(websockets.asyncio.client, "connect", fake_connect)
    result = await _default_connect(_DEFAULT_CONNECT_URL)
    assert result is _FAKE_WS_SENTINEL


# ---- _log_transition: early-return when same state logged twice ----

_AUTH_FAILURE_EVENT = "ha_websocket.auth_failed"
_AUTH_LOGGED_COUNT_EXPECTED = 1


@pytest.mark.asyncio
async def test_log_transition_skips_duplicate_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Line 509: _log_transition early-return when the same failure state repeats.

    Script: conn1 -> auth_invalid, conn2 -> auth_invalid again (identical state
    key), conn3 -> auth_ok (so _wait_connected can return). The warning for
    ``ha_websocket.auth_failed`` must appear exactly once in captured logs even
    though two consecutive auth failures occurred.
    """
    conn1 = FakeWsConnection()
    conn1.push({"type": "auth_required"})
    conn1.push({"type": "auth_invalid"})

    conn2 = FakeWsConnection()
    conn2.push({"type": "auth_required"})
    conn2.push({"type": "auth_invalid"})

    conn3 = FakeWsConnection()
    _seed_auth_ok(conn3)

    monkeypatch.setattr(asyncio, "sleep", _instant_sleep())
    client, metrics = _client(ScriptedConnector(conn1, conn2, conn3))
    with capture_logs() as captured:
        client.start_task()
        await _wait_connected(client)

    auth_failed_entries = [e for e in captured if e.get("event") == _AUTH_FAILURE_EVENT]
    assert len(auth_failed_entries) == _AUTH_LOGGED_COUNT_EXPECTED
    assert (
        _counter_count(metrics, "homelab_ha_websocket_error_total", reason="auth")
        == _AUTH_ERROR_COUNT_EXPECTED
    )
    await client.stop_task()


# ---- helpers ----


def _instant_sleep():  # noqa: ANN202
    """An asyncio.sleep replacement that yields control once but never waits."""

    async def _sleep(_delay: float) -> None:
        # Yield to the loop so other tasks progress, but do not actually wait.
        await _yield_once()

    return _sleep


async def _yield_once() -> None:
    fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    asyncio.get_running_loop().call_soon(fut.set_result, None)
    await fut


# ---- _fail_all_pending: already-done future is skipped ----

_ALREADY_DONE_RESULT: dict[str, object] = {"status": "already_done"}
_PENDING_CMD_ID = 99


@pytest.mark.asyncio
async def test_replay_subscribe_failure_is_logged(monkeypatch: pytest.MonkeyPatch) -> None:
    """_replay_intents HaError branch (websocket.py:437-443): warning fires on success:false replay.

    On reconnect, the replayed subscribe returns HaError (success:false confirm) so
    the warning at line 439 fires.  The intent must REMAIN in _intents for the next
    reconnect attempt.
    """
    conn1 = FakeWsConnection()
    _seed_auth_ok(conn1)
    conn2 = FakeWsConnection()
    _seed_auth_ok(conn2)
    monkeypatch.setattr(asyncio, "sleep", _instant_sleep())
    client, _ = _client(ScriptedConnector(conn1, conn2))
    client.start_task()
    await _wait_connected(client)

    # Subscribe successfully on conn1 so the intent is registered in _intents.
    sub_task = asyncio.ensure_future(client.subscribe("subscribe_events"))
    await _wait_until(lambda: any(s.get("type") == "subscribe_events" for s in conn1.sent))
    sub_id1 = next(s["id"] for s in conn1.sent if s.get("type") == "subscribe_events")
    conn1.push({"id": sub_id1, "type": "result", "success": True})
    sub = await sub_task
    assert isinstance(sub, Subscription)
    assert len(client._intents) == 1  # pyright: ignore[reportPrivateUsage]

    # Force reconnect to conn2.
    conn1.push_close()

    # Wait for conn2 to receive the replayed subscribe command.
    await _wait_until(lambda: any(s.get("type") == "subscribe_events" for s in conn2.sent))
    sub_id2 = next(s["id"] for s in conn2.sent if s.get("type") == "subscribe_events")

    # Push a success:false result for the replayed subscribe -> _do_subscribe returns HaError
    # -> _replay_intents logs the warning at line 439.
    with capture_logs() as captured:
        conn2.push({"id": sub_id2, "type": "result", "success": False})
        await _wait_until(
            lambda: any(e.get("event") == "ha_websocket.replay_subscribe_failed" for e in captured)
        )

    # Warning must have been emitted with the right fields.
    warning = next(e for e in captured if e.get("event") == "ha_websocket.replay_subscribe_failed")
    assert warning["subscription_type"] == "subscribe_events"
    assert "reason" in warning

    # Intent must remain in _intents (retries on next reconnect).
    assert len(client._intents) == 1  # pyright: ignore[reportPrivateUsage]

    # Connection is still up after failed replay (success:false doesn't close socket).
    assert client.connected
    await client.stop_task()


@pytest.mark.asyncio
async def test_fail_all_pending_skips_already_done_future() -> None:
    """_fail_all_pending must skip futures that are already done (branch 495->494).

    Inserts one pre-resolved future into _pending, calls _fail_all_pending, and
    asserts: (1) no exception is raised, (2) the future's result is unchanged (not
    overwritten with the error), and (3) _pending is cleared afterward.
    """
    connector = ScriptedConnector()
    client, _ = _client(connector)
    loop = asyncio.get_running_loop()

    # Create a future that is already done before _fail_all_pending is called.
    already_done: asyncio.Future[dict[str, object] | list[object] | HaError] = loop.create_future()
    already_done.set_result(_ALREADY_DONE_RESULT)

    client._pending[_PENDING_CMD_ID] = already_done  # pyright: ignore[reportPrivateUsage]

    error = HaError(reason="unreachable", message="should not overwrite")
    client._fail_all_pending(error)  # pyright: ignore[reportPrivateUsage]

    # The pre-done future must not have been overwritten.
    assert already_done.result() == _ALREADY_DONE_RESULT
    # _pending must be cleared regardless.
    assert not client._pending  # pyright: ignore[reportPrivateUsage]
