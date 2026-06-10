"""Home Assistant WebSocket client (STAGE-005-002).

D-HA-REST-FIRST: REST (``kernel/ha/client.py``) covers config / states /
error-log / service calls. This WebSocket client adds the live channel: a
long-lived, supervised connection that performs the HA auth handshake, runs
one-shot RPC commands, manages event subscriptions, and reconnects with
exponential backoff while replaying active subscriptions onto fresh ids.

Construction (lifespan, once at startup, AFTER metrics_writer is built)::

    HomeAssistantWebsocketClient(
        base_url=ha_config.base_url,
        token_provider=lambda: ttl_resolver.current().get("ha_token"),
        metrics_writer=metrics_writer,
        log=log.bind(component="ha_websocket"),
    )

Lifecycle mirrors ``BuildSourcesLoader``: ``start_task()`` (idempotent) launches
``_run_loop`` as an asyncio task; ``stop_task()`` cancels + suppresses
``CancelledError`` + awaits + clears the handle.

SECURITY: the bearer token never appears in any returned ``HaError.message``,
in any log line, nor in any exception. The auth frame (which carries the token)
is NEVER logged. Error messages are built from the WS URL + reason only.

D-HA-CLIENT-RETURN-NOT-RAISE: ``send_command`` / ``subscribe`` return a success
value OR an :class:`HaError`; they never raise for protocol / transport failures.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import TYPE_CHECKING, Final, cast

from homelab_monitor.kernel.ha.errors import HaError, HaErrorReason

if TYPE_CHECKING:
    from typing import Protocol

    from structlog.stdlib import BoundLogger

    from homelab_monitor.kernel.plugins.io import MetricsWriter

    class _WsConnection(Protocol):
        """Minimal surface of a live websockets connection we depend on.

        Kept deliberately small so the test fake is trivial AND pyright-strict
        clean without importing the awkward ``websockets`` type stubs.
        """

        async def send(self, message: str) -> None:
            """Send one text frame."""
            ...

        async def recv(self) -> str:
            """Receive one text frame (str; we never request binary)."""
            ...

        async def close(self) -> None:
            """Close the connection."""
            ...

        def __aiter__(self) -> AsyncIterator[str]:
            """Async-iterate inbound text frames until the socket closes."""
            ...

    # A connect callable: ws_url -> awaitable yielding a live connection.
    _ConnectCallable = Callable[[str], Awaitable["_WsConnection"]]


# ---- metric names (exact) ----
_METRIC_CONNECTED: Final[str] = "homelab_ha_websocket_connected"
_METRIC_LAST_MESSAGE: Final[str] = "homelab_ha_websocket_last_message_timestamp_seconds"
_METRIC_RECONNECT: Final[str] = "homelab_ha_websocket_reconnect_total"
_METRIC_ERROR: Final[str] = "homelab_ha_websocket_error_total"

_MAX_BACKOFF: Final[float] = 60.0
_INITIAL_BACKOFF: Final[float] = 1.0
_COMMAND_TIMEOUT_SECONDS: Final[float] = 30.0
_QUEUE_MAXSIZE: Final[int] = 256
_WS_PATH: Final[str] = "/api/websocket"


async def _default_connect(ws_url: str) -> _WsConnection:
    """Default ``connect`` seam: open a real websockets connection.

    Imported lazily so ``websockets`` is only touched at runtime (keeps module
    import cheap + keeps the awkward stubs out of the strict-typed surface).
    The single ``cast`` here is the sole place we bridge the real library type
    to our minimal :class:`_WsConnection` Protocol.
    """
    from websockets.asyncio.client import connect  # noqa: PLC0415

    conn = await connect(ws_url)
    return cast("_WsConnection", conn)


class Subscription:
    """A live HA event subscription: an async-iterable of event dicts.

    A future collector does ``async for event in sub:``. Backed by a bounded
    queue; the WS client's receive loop enqueues ``event`` frames for this
    subscription's current id. Across a reconnect the SAME ``Subscription``
    object is kept; only its bound ``id`` and queue contents change (the client
    rebinds it via ``_replay_intents``).

    ``_closed`` is set by ``unsubscribe`` so an in-flight ``async for`` ends
    cleanly (``__anext__`` raises ``StopAsyncIteration`` when closed AND drained).
    """

    def __init__(self, type_: str, fields: dict[str, object]) -> None:
        self.type_: str = type_
        self.fields: dict[str, object] = fields
        # Per-connection binding; reset on each (re)subscribe.
        self.id: int | None = None
        self.queue: asyncio.Queue[dict[str, object]] = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
        self._closed: bool = False

    def _rebind(self, new_id: int) -> None:
        """Point this subscription at a fresh per-connection id + empty queue."""
        self.id = new_id
        self.queue = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)

    def __aiter__(self) -> AsyncIterator[dict[str, object]]:
        return self

    async def __anext__(self) -> dict[str, object]:
        while True:
            if self._closed and self.queue.empty():
                raise StopAsyncIteration
            try:
                # Short timeout so a close that happens WHILE we wait is observed.
                return await asyncio.wait_for(self.queue.get(), timeout=1.0)
            except TimeoutError:
                continue


class HomeAssistantWebsocketClient:
    """Long-lived, supervised HA WebSocket client.

    Demuxes inbound frames into one-shot command futures (``_pending``) and
    per-subscription bounded queues (``_subscriptions``). Reconnects with
    exponential backoff and replays active subscription intents on a fresh
    connection (HA ids are monotonic WITHIN a connection, so ids reset per
    connection).
    """

    def __init__(
        self,
        *,
        base_url: str,
        token_provider: Callable[[], str | None],
        metrics_writer: MetricsWriter,
        log: BoundLogger,
        connect: _ConnectCallable = _default_connect,
    ) -> None:
        """Initialize the client (does NOT connect; call ``start_task``).

        Args:
            base_url: HA base URL (e.g. ``http://192.168.2.148:8123``). Trailing
                slashes are stripped; ``http``->``ws`` / ``https``->``wss`` and
                ``/api/websocket`` is appended to derive the WS URL.
            token_provider: zero-arg callable returning the current bearer token
                or ``None``. Called at each (re)connect; never stored.
            metrics_writer: shared writer for the ``homelab_ha_websocket_*`` series.
            log: bound structlog logger (caller binds ``component="ha_websocket"``).
            connect: INJECTABLE connection factory (default = real websockets).
        """
        self._base_url: str = base_url.rstrip("/")
        self._ws_url: str = self._derive_ws_url(self._base_url)
        self._token_provider: Callable[[], str | None] = token_provider
        self._metrics: MetricsWriter = metrics_writer
        self._log: BoundLogger = log
        self._connect: _ConnectCallable = connect

        self._task: asyncio.Task[None] | None = None
        self._connected: bool = False
        self._id_counter: int = 0
        self._backoff: float = _INITIAL_BACKOFF
        self._conn: _WsConnection | None = None

        self._pending: dict[int, asyncio.Future[dict[str, object] | HaError]] = {}
        self._subscriptions: dict[int, Subscription] = {}
        self._intents: list[Subscription] = []
        # Transition-only logging guard: last (kind) we logged, e.g. "auth_invalid".
        self._last_logged_state: str | None = None
        # Reconnect-metric guard: True after the first successful connect.
        self._has_connected_once: bool = False

    # ---- derivation ----

    @staticmethod
    def _derive_ws_url(base_url: str) -> str:
        """``http(s)://host`` (no trailing slash) -> ``ws(s)://host/api/websocket``."""
        if base_url.startswith("https://"):
            scheme_body = "wss://" + base_url[len("https://") :]
        elif base_url.startswith("http://"):
            scheme_body = "ws://" + base_url[len("http://") :]
        else:
            # No recognized scheme: leave host as-is, prefix ws://.
            scheme_body = "ws://" + base_url
        return scheme_body + _WS_PATH

    # ---- public API ----

    @property
    def connected(self) -> bool:
        """True iff the auth handshake has completed and the socket is open."""
        return self._connected

    def start_task(self) -> None:
        """Launch the supervised run loop. Idempotent."""
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._run_loop(), name="ha_websocket")

    async def stop_task(self) -> None:
        """Cancel + await the run loop; clear the handle."""
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    async def send_command(self, type_: str, **fields: object) -> dict[str, object] | HaError:
        """One-shot RPC: send ``{"id", "type", **fields}``, await the result frame.

        Returns the result payload dict on ``success: true``, or an HaError:
          - not connected -> ``unreachable``
          - command timeout -> ``timeout``
          - ``success: false`` -> ``http_error`` if HA gave a numeric error code,
            else ``bad_response``
          - disconnect while awaiting -> ``unreachable`` (set by ``_fail_all_pending``)
        """
        if not self._connected or self._conn is None:
            return HaError(reason="unreachable", message="websocket not connected")
        msg_id = self._next_id()
        future: asyncio.Future[dict[str, object] | HaError] = (
            asyncio.get_running_loop().create_future()
        )
        self._pending[msg_id] = future
        frame = {"id": msg_id, "type": type_, **fields}
        try:
            await self._conn.send(json.dumps(frame))
        except Exception:
            self._pending.pop(msg_id, None)
            return HaError(reason="unreachable", message="websocket send failed")
        try:
            return await asyncio.wait_for(future, timeout=_COMMAND_TIMEOUT_SECONDS)
        except TimeoutError:
            self._pending.pop(msg_id, None)
            return HaError(reason="timeout", message=f"command '{type_}' timed out")

    async def subscribe(self, type_: str, **fields: object) -> Subscription | HaError:
        """Register a subscription intent + send the subscribe command.

        On a confirming ``success: true`` result, binds a fresh id->queue and
        returns the :class:`Subscription` handle. On ``success: false`` or any
        send/timeout failure, returns the HaError and registers NO intent.
        """
        sub = Subscription(type_=type_, fields=dict(fields))
        result = await self._do_subscribe(sub)
        if isinstance(result, HaError):
            return result
        self._intents.append(sub)
        return sub

    async def unsubscribe(self, sub: Subscription) -> None:
        """Send ``unsubscribe_events`` for ``sub`` and drop its intent + binding."""
        sub._closed = True  # pyright: ignore[reportPrivateUsage]
        if sub in self._intents:
            self._intents.remove(sub)
        if sub.id is not None:
            self._subscriptions.pop(sub.id, None)
            # Best-effort unsubscribe frame; HA ignores unknown unsubscribe ids
            # so id-staleness across a reconnect is tolerated.
            await self.send_command("unsubscribe_events", subscription=sub.id)
            sub.id = None  # clear binding after drop for hygiene

    # ---- internals: subscribe helper ----

    async def _do_subscribe(self, sub: Subscription) -> Subscription | HaError:
        """Allocate an id, register the queue, send the subscribe command, confirm."""
        if not self._connected or self._conn is None:
            return HaError(reason="unreachable", message="websocket not connected")
        msg_id = self._next_id()
        sub._rebind(msg_id)  # pyright: ignore[reportPrivateUsage]
        self._subscriptions[msg_id] = sub
        future: asyncio.Future[dict[str, object] | HaError] = (
            asyncio.get_running_loop().create_future()
        )
        self._pending[msg_id] = future
        frame: dict[str, object] = {"id": msg_id, "type": sub.type_, **sub.fields}
        try:
            await self._conn.send(json.dumps(frame))
        except Exception:
            self._pending.pop(msg_id, None)
            self._subscriptions.pop(msg_id, None)
            return HaError(reason="unreachable", message="websocket send failed")
        try:
            result = await asyncio.wait_for(future, timeout=_COMMAND_TIMEOUT_SECONDS)
        except TimeoutError:
            self._pending.pop(msg_id, None)
            self._subscriptions.pop(msg_id, None)
            return HaError(reason="timeout", message=f"subscribe '{sub.type_}' timed out")
        if isinstance(result, HaError):
            self._subscriptions.pop(msg_id, None)
            return result
        return sub

    # ---- internals: id + metrics ----

    def _next_id(self) -> int:
        """Monotonic per-connection id. Reset to 0 on each new connection."""
        self._id_counter += 1
        return self._id_counter

    def _emit_error(self, reason: HaErrorReason) -> None:
        self._metrics.write_counter(_METRIC_ERROR, 1.0, {"reason": reason})

    def _set_connected(self, value: bool) -> None:
        """Update connected state; emit the gauge ONLY on a transition."""
        if value == self._connected:
            return
        self._connected = value
        self._metrics.write_gauge(_METRIC_CONNECTED, 1.0 if value else 0.0, {})

    # ---- internals: the supervised loop ----

    async def _run_loop(self) -> None:
        """Connect, auth, replay subscriptions, receive — reconnect on failure.

        Re-raises CancelledError so ``stop_task`` observes cancellation. Each
        failure backs off (1s -> 60s, doubling); a successful connect resets it.
        """
        self._backoff = _INITIAL_BACKOFF
        while True:
            conn: _WsConnection | None = None
            try:
                conn = await self._connect_and_auth()
                self._conn = conn
                self._set_connected(True)
                if self._has_connected_once:
                    self._metrics.write_counter(_METRIC_RECONNECT, 1.0, {})
                self._has_connected_once = True
                self._backoff = _INITIAL_BACKOFF
                replay_task = asyncio.create_task(self._replay_intents(conn))
                try:
                    await self._receive_loop(conn)
                finally:
                    replay_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await replay_task
                # Receive loop returned (socket closed cleanly) -> treat as failure.
                raise _WsClosed(HaError(reason="unreachable", message="websocket closed"))
            except asyncio.CancelledError:
                self._set_connected(False)
                self._fail_all_pending(
                    HaError(reason="unreachable", message="websocket shutting down")
                )
                await self._close(conn)
                raise
            except _AuthFailure as exc:
                self._set_connected(False)
                self._fail_all_pending(
                    HaError(reason="unreachable", message="websocket disconnected")
                )
                await self._close(conn)
                self._conn = None
                self._emit_error("auth")
                self._log_transition("auth_failure", "ha_websocket.auth_failed", reason=exc.detail)
            except Exception as exc:
                reason: HaErrorReason = exc.reason if isinstance(exc, _WsClosed) else "unreachable"
                self._set_connected(False)
                self._fail_all_pending(
                    HaError(reason="unreachable", message="websocket disconnected")
                )
                await self._close(conn)
                self._conn = None
                self._emit_error(reason)
                self._log_transition(
                    f"error:{reason}", "ha_websocket.connection_error", error=str(exc)
                )
            # Backoff before reconnecting.
            try:
                await asyncio.sleep(self._backoff)
            except asyncio.CancelledError:
                raise
            self._backoff = min(self._backoff * 2, _MAX_BACKOFF)

    async def _connect_and_auth(self) -> _WsConnection:
        """Open the socket + perform the HA auth handshake. Returns the live conn.

        Raises :class:`_AuthFailure` for no-token / auth_invalid (the run loop
        maps it to error_total{reason="auth"} + backoff). Other connect/recv
        failures propagate to the loop's generic handler.
        """
        conn = await self._connect(self._ws_url)
        self._id_counter = 0  # ids are monotonic WITHIN a connection.
        # Clear stale id->Subscription bindings; _replay_intents re-populates
        # with fresh ids. _intents (replay source) and _pending are NOT cleared.
        self._subscriptions.clear()
        first: object = json.loads(await conn.recv())
        if not isinstance(first, dict):
            await self._close(conn)
            raise _WsClosed(HaError(reason="bad_response", message="expected auth_required"))
        first_msg = cast("dict[str, object]", first)
        if first_msg.get("type") != "auth_required":
            await self._close(conn)
            raise _WsClosed(HaError(reason="bad_response", message="expected auth_required"))
        token = self._token_provider()
        if token is None:
            await self._close(conn)
            # NEVER send an auth frame with a None token. NEVER log the token.
            raise _AuthFailure("no token configured")
        # SECURITY: this frame carries the token — do NOT log it.
        await conn.send(json.dumps({"type": "auth", "access_token": token}))
        second: object = json.loads(await conn.recv())
        if isinstance(second, dict):
            second_msg = cast("dict[str, object]", second)
            if second_msg.get("type") == "auth_ok":
                return conn
        await self._close(conn)
        raise _AuthFailure("auth_invalid")

    async def _replay_intents(self, conn: _WsConnection) -> None:
        """Re-subscribe every active intent on the fresh connection."""
        del conn  # _do_subscribe uses self._conn / self._connected, already set.
        # Iterate a snapshot: _do_subscribe does not mutate _intents.
        for sub in list(self._intents):
            result = await self._do_subscribe(sub)
            if isinstance(result, HaError):
                # Intent remains in _intents and retries on the next reconnect.
                self._log.warning(
                    "ha_websocket.replay_subscribe_failed",
                    subscription_type=sub.type_,
                    reason=result.reason,
                )

    async def _receive_loop(self, conn: _WsConnection) -> None:
        """Demux inbound frames until the socket closes/raises."""
        async for raw in conn:
            try:
                msg = json.loads(raw)
            except (ValueError, TypeError):
                self._log.debug("ha_websocket.bad_frame")
                continue
            if not isinstance(msg, dict):
                self._log.debug("ha_websocket.non_object_frame")
                continue
            frame = cast("dict[str, object]", msg)
            self._metrics.write_gauge(_METRIC_LAST_MESSAGE, time.time(), {})
            self._dispatch_frame(frame)

    def _dispatch_frame(self, frame: dict[str, object]) -> None:
        """Route one parsed frame to a pending future or a subscription queue."""
        ftype = frame.get("type")
        raw_id = frame.get("id")
        mid = raw_id if isinstance(raw_id, int) else None
        if ftype == "result" and mid is not None:
            fut = self._pending.pop(mid, None)
            if fut is not None and not fut.done():
                fut.set_result(self._result_payload(frame))
        elif ftype == "event" and mid is not None:
            sub = self._subscriptions.get(mid)
            if sub is not None:
                self._enqueue_event(sub, frame)
        else:
            # auth_required / auth_ok / pong / unknown post-handshake -> ignore.
            self._log.debug("ha_websocket.unhandled_frame", frame_type=str(ftype))

    @staticmethod
    def _result_payload(frame: dict[str, object]) -> dict[str, object] | HaError:
        """Map a ``result`` frame to its payload dict or an HaError."""
        if frame.get("success") is True:
            result = frame.get("result")
            if isinstance(result, dict):
                return cast("dict[str, object]", result)
            return {}  # success with null/non-dict result (e.g. subscribe confirms).
        # success: false -> error. HA error shape: {"error": {"code", "message"}}.
        err = frame.get("error")
        code: int | None = None
        if isinstance(err, dict):
            raw_code = cast("dict[str, object]", err).get("code")
            if isinstance(raw_code, int):
                code = raw_code
        if code is not None:
            return HaError(reason="http_error", message="command failed", status=code)
        return HaError(reason="bad_response", message="command failed")

    def _enqueue_event(self, sub: Subscription, frame: dict[str, object]) -> None:
        """Enqueue the ``event`` payload; drop-oldest + log on a full queue."""
        event = frame.get("event")
        payload = cast("dict[str, object]", event) if isinstance(event, dict) else frame
        if sub.queue.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                sub.queue.get_nowait()  # drop oldest
            self._log.warning("ha_websocket.event_queue_full", subscription_id=sub.id)
        sub.queue.put_nowait(payload)

    # ---- internals: teardown helpers ----

    def _fail_all_pending(self, error: HaError) -> None:
        """Resolve every outstanding command future with an HaError (never hang)."""
        for fut in self._pending.values():
            if not fut.done():
                fut.set_result(error)
        self._pending.clear()

    async def _close(self, conn: _WsConnection | None) -> None:
        """Close a connection, ignoring any close-time error."""
        if conn is None:
            return
        with contextlib.suppress(Exception):
            await conn.close()

    def _log_transition(self, state_key: str, event: str, **kw: object) -> None:
        """Log ``event`` only when the failure state KEY changed (avoids spam)."""
        if self._last_logged_state == state_key:
            return
        self._last_logged_state = state_key
        self._log.warning(event, **kw)


class _AuthFailure(Exception):
    """Internal signal: no-token / auth_invalid. Maps to error_total{reason=auth}."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail: str = detail


class _WsClosed(Exception):
    """Internal signal carrying the HaError reason for a closed/bad socket."""

    def __init__(self, error: HaError) -> None:
        super().__init__(error.message)
        self.reason: HaErrorReason = error.reason


__all__: Final = ["HomeAssistantWebsocketClient", "Subscription"]
