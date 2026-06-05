"""Handler and integration tests for /logs/tail SSE endpoint (STAGE-004-023)."""

from __future__ import annotations

import re
from typing import cast
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from pytest_httpx import HTTPXMock

from homelab_monitor.kernel.api.errors import ServiceUnavailableProblem
from homelab_monitor.kernel.logs.tail_service import TailRegistry


class TestLogsHandlerTailRequiresSession:
    """GET /logs/tail requires session auth."""

    @pytest.mark.asyncio
    async def test_tail_requires_session(self, unauthenticated_client: AsyncClient) -> None:
        """Anon client → 401 unauthorized."""
        resp = await unauthenticated_client.get(
            "/api/logs/tail?expr=test",
            follow_redirects=False,
        )
        assert resp.status_code == 401  # noqa: PLR2004
        assert resp.json()["error"]["code"] == "unauthenticated"


class TestLogsHandlerTailValidation:
    """GET /logs/tail input validation."""

    @pytest.mark.asyncio
    async def test_tail_bad_expr_length(
        self, authenticated_client: AsyncClient, _shared_app: FastAPI
    ) -> None:
        """Expression > 4096 chars → 400 invalid_expr; slot NOT acquired."""
        # Acquire all slots manually
        registry = cast(TailRegistry, _shared_app.state.tail_registry)
        for _ in range(registry.max_connections):
            registry.try_acquire()
        try:
            long_expr = "a" * 4097
            resp = await authenticated_client.get(
                f"/api/logs/tail?expr={long_expr}",
            )
            assert resp.status_code == 400  # noqa: PLR2004
            assert resp.json()["error"]["code"] == "invalid_expr"
            # Slot should NOT be acquired (count unchanged)
            assert registry.active_count == registry.max_connections
        finally:
            # Release manually acquired slots
            for _ in range(registry.max_connections):
                registry.release()


class TestLogsHandlerTailCap:
    """GET /logs/tail global connection cap."""

    @pytest.mark.asyncio
    async def test_tail_503_over_cap(
        self, authenticated_client: AsyncClient, _shared_app: FastAPI
    ) -> None:
        """Global cap reached → 503 tail_capacity with Retry-After: 60."""
        registry = cast(TailRegistry, _shared_app.state.tail_registry)
        # Pre-acquire all slots
        for _ in range(registry.max_connections):
            registry.try_acquire()

        try:
            resp = await authenticated_client.get(
                "/api/logs/tail?expr=test",
            )
            assert resp.status_code == 503  # noqa: PLR2004
            assert resp.json()["error"]["code"] == "tail_capacity"
            assert resp.headers.get("Retry-After") == "60"
        finally:
            # Release manually acquired slots
            for _ in range(registry.max_connections):
                registry.release()


class TestLogsHandlerTailProbe:
    """GET /logs/tail pre-flight probe."""

    @pytest.mark.asyncio
    async def test_tail_422_on_vl_4xx_probe(
        self, authenticated_client: AsyncClient, _shared_app: FastAPI, httpx_mock: HTTPXMock
    ) -> None:
        """VL 4xx on probe → 422 invalid_logsql; slot released."""
        registry = cast(TailRegistry, _shared_app.state.tail_registry)
        httpx_mock.add_response(
            method="GET",
            url=re.compile(r"http://.*:9428/select/logsql/query.*"),
            status_code=400,
            text="bad query",
        )

        resp = await authenticated_client.get(
            "/api/logs/tail?expr=test",
        )
        assert resp.status_code == 422  # noqa: PLR2004
        assert resp.json()["error"]["code"] == "invalid_logsql"
        # Slot released after probe failure
        assert registry.active_count == 0

    @pytest.mark.asyncio
    async def test_tail_502_on_vl_5xx_probe(
        self, authenticated_client: AsyncClient, _shared_app: FastAPI, httpx_mock: HTTPXMock
    ) -> None:
        """VL 5xx on probe → 502 upstream_unavailable; slot released."""
        registry = cast(TailRegistry, _shared_app.state.tail_registry)
        httpx_mock.add_response(
            method="GET",
            url=re.compile(r"http://.*:9428/select/logsql/query.*"),
            status_code=500,
            text="internal error",
        )

        resp = await authenticated_client.get(
            "/api/logs/tail?expr=test",
        )
        assert resp.status_code == 502  # noqa: PLR2004
        assert resp.json()["error"]["code"] == "upstream_unavailable"
        # Slot released after probe failure
        assert registry.active_count == 0


class TestLogsHandlerTailHappyPath:
    """GET /logs/tail happy path streaming."""

    @pytest.mark.asyncio
    async def test_tail_happy_path_streams_lines(
        self,
        authenticated_client: AsyncClient,
        _shared_app: FastAPI,
        httpx_mock: HTTPXMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Probe succeeds → stream returns lines as SSE."""
        # Speed up for test: small poll + short duration
        monkeypatch.setenv("HOMELAB_MONITOR_TAIL_POLL_MS", "10")
        monkeypatch.setenv("HOMELAB_MONITOR_TAIL_MAX_DURATION_S", "1")

        registry = cast(TailRegistry, _shared_app.state.tail_registry)

        # Mock VL: probe + each poll return 2 lines. Timestamps are in the far
        # future so they are NEWER than the session's connection-time cursor
        # anchor (handler clock = real datetime.now(UTC)); otherwise advance()
        # drops them as already-past. is_reusable: the poll loop re-queries each
        # tick; is_optional: exact poll count before the reader breaks is timing-
        # dependent.
        httpx_mock.add_response(
            method="GET",
            url=re.compile(r"http://.*:9428/select/logsql/query.*"),
            status_code=200,
            text='{"_time":"2099-01-01T00:00:00.000100Z","_msg":"line1","_stream_id":"s1"}\n'
            '{"_time":"2099-01-01T00:00:00.000200Z","_msg":"line2","_stream_id":"s1"}\n',
            is_optional=True,
            is_reusable=True,
        )

        with patch("asyncio.sleep", new_callable=AsyncMock):
            async with authenticated_client.stream(
                "GET",
                "/api/logs/tail?expr=test",
            ) as resp:
                assert resp.status_code == 200  # noqa: PLR2004
                assert resp.headers["content-type"] == "text/event-stream; charset=utf-8"

                lines: list[str] = []
                async for line in resp.aiter_lines():
                    lines.append(line)
                    # Stop after reading 2 SSE event blocks
                    if len([ln for ln in lines if ln.startswith("event:")]) >= 2:  # noqa: PLR2004
                        break

                # Should have at least 2 'event: line' events
                event_lines = [ln for ln in lines if ln.startswith("event: line")]
                assert len(event_lines) >= 2  # noqa: PLR2004

        # Slot released after stream ends
        assert registry.active_count == 0

    @pytest.mark.asyncio
    async def test_tail_slot_released_on_disconnect(
        self,
        authenticated_client: AsyncClient,
        _shared_app: FastAPI,
        httpx_mock: HTTPXMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Closing stream early → slot released."""
        # Speed up + duration backstop so the server-side poll loop terminates
        # even if the client-side break races (mirrors happy_path).
        monkeypatch.setenv("HOMELAB_MONITOR_TAIL_POLL_MS", "10")
        monkeypatch.setenv("HOMELAB_MONITOR_TAIL_MAX_DURATION_S", "1")

        registry = cast(TailRegistry, _shared_app.state.tail_registry)
        httpx_mock.add_response(
            method="GET",
            url=re.compile(r"http://.*:9428/select/logsql/query.*"),
            status_code=200,
            text='{"_time":"2099-01-01T00:00:00.000100Z","_msg":"test","_stream_id":"s1"}\n',
            is_optional=True,
            is_reusable=True,
        )

        with patch("asyncio.sleep", new_callable=AsyncMock):
            async with authenticated_client.stream(
                "GET",
                "/api/logs/tail?expr=test",
            ) as resp:
                assert resp.status_code == 200  # noqa: PLR2004
                # Read one line then break
                async for line in resp.aiter_lines():
                    if line.startswith("data:"):
                        break

        # Slot released after context exit
        assert registry.active_count == 0


class TestServiceUnavailableProblemHandler:
    """_handle_service_unavailable branches without Retry-After (lines 238->242, 240->242)."""

    @pytest.mark.asyncio
    async def test_503_no_retry_after_when_details_none(
        self, authenticated_client: AsyncClient, _shared_app: FastAPI
    ) -> None:
        """ServiceUnavailableProblem with details=None → 503, no Retry-After header."""
        from fastapi import FastAPI as _FastAPI  # noqa: PLC0415
        from starlette.testclient import TestClient  # noqa: PLC0415

        from homelab_monitor.kernel.api.errors import register_error_handlers  # noqa: PLC0415

        # Build a minimal app that raises ServiceUnavailableProblem(details=None)
        # and has _handle_service_unavailable registered.
        mini_app = _FastAPI()

        @mini_app.get("/test-503-no-details")
        async def _route_no_details() -> None:  # pyright: ignore[reportUnusedFunction]
            raise ServiceUnavailableProblem(message="down", details=None)

        register_error_handlers(mini_app)

        client = TestClient(mini_app, raise_server_exceptions=False)
        resp = client.get("/test-503-no-details")
        assert resp.status_code == 503  # noqa: PLR2004
        assert "Retry-After" not in resp.headers
        assert resp.json()["error"]["code"] == "service_unavailable"

    @pytest.mark.asyncio
    async def test_503_no_retry_after_when_details_missing_key(
        self, authenticated_client: AsyncClient, _shared_app: FastAPI
    ) -> None:
        """details={} → 503, no Retry-After header (branch 240->242)."""
        from fastapi import FastAPI as _FastAPI  # noqa: PLC0415
        from starlette.testclient import TestClient  # noqa: PLC0415

        from homelab_monitor.kernel.api.errors import register_error_handlers  # noqa: PLC0415

        mini_app = _FastAPI()

        @mini_app.get("/test-503-no-key")
        async def _route_no_key() -> None:  # pyright: ignore[reportUnusedFunction]
            raise ServiceUnavailableProblem(message="down", details={"other": "x"})

        register_error_handlers(mini_app)

        client = TestClient(mini_app, raise_server_exceptions=False)
        resp = client.get("/test-503-no-key")
        assert resp.status_code == 503  # noqa: PLR2004
        assert "Retry-After" not in resp.headers
        assert resp.json()["error"]["code"] == "service_unavailable"


class TestLogsHandlerTailProbeBaseException:
    """GET /logs/tail BaseException in probe → slot released (lines 399-401)."""

    @pytest.mark.asyncio
    async def test_tail_probe_unexpected_exception_releases_slot(
        self,
        authenticated_client: AsyncClient,
        _shared_app: FastAPI,
    ) -> None:
        """Unexpected non-VL exception during probe → slot released (lines 399-401).

        httpx ASGITransport re-raises unhandled exceptions from the app rather
        than surfacing them as 500 responses when raise_server_exceptions is
        True (the httpx default).  We therefore expect a RuntimeError to
        propagate to the test, which is the same behavior users would observe
        as a 500 (the FastAPI generic handler would normally catch this, but
        ASGITransport's transport layer propagates it).  The CRITICAL assertion
        is that the slot counter returns to 0 — the BaseException branch ran.
        """
        registry = cast(TailRegistry, _shared_app.state.tail_registry)

        with (
            patch(
                "homelab_monitor.kernel.logs.victorialogs_client.VictoriaLogsClient.query",
                new_callable=AsyncMock,
                side_effect=RuntimeError("unexpected boom"),
            ),
            pytest.raises(RuntimeError, match="unexpected boom"),
        ):
            await authenticated_client.get("/api/logs/tail?expr=test")

        # Slot must be released by the BaseException branch (lines 399-401).
        assert registry.active_count == 0


class TestLogsHandlerTailSSEEventTypes:
    """GET /logs/tail gen() SSE formatting for DroppedEvent and ErrorEvent."""

    @pytest.mark.asyncio
    async def test_tail_emits_dropped_event(
        self,
        authenticated_client: AsyncClient,
        _shared_app: FastAPI,
        httpx_mock: HTTPXMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Backpressure cap exceeded → gen() emits 'event: dropped' SSE frame."""
        # Set max_lines_per_sec to 2 so 5 lines in one poll causes 3 dropped.
        monkeypatch.setenv("HOMELAB_MONITOR_TAIL_POLL_MS", "10")
        monkeypatch.setenv("HOMELAB_MONITOR_TAIL_MAX_DURATION_S", "1")
        monkeypatch.setenv("HOMELAB_MONITOR_TAIL_MAX_LINES_PER_SEC", "2")

        # Probe + poll both return 5 future-dated lines. max_lines_per_sec=2
        # means 3 are dropped → DroppedEvent.
        lines_payload = "".join(
            f'{{"_time":"2099-01-01T00:00:00.000{100 + i:03d}Z",'
            f'"_msg":"line{i}","_stream_id":"s1"}}\n'
            for i in range(5)
        )
        httpx_mock.add_response(
            method="GET",
            url=re.compile(r"http://.*:9428/select/logsql/query.*"),
            status_code=200,
            text=lines_payload,
            is_optional=True,
            is_reusable=True,
        )

        with patch("asyncio.sleep", new_callable=AsyncMock):
            async with authenticated_client.stream(
                "GET",
                "/api/logs/tail?expr=test",
            ) as resp:
                assert resp.status_code == 200  # noqa: PLR2004

                lines: list[str] = []
                async for line in resp.aiter_lines():
                    lines.append(line)
                    # Collect until we have both the event: dropped line AND its
                    # data: line (they arrive on consecutive lines).
                    has_dropped = any(ln == "event: dropped" for ln in lines)
                    has_data = any("count" in ln for ln in lines)
                    if has_dropped and has_data:
                        break

        assert any(ln == "event: dropped" for ln in lines)
        assert any("count" in ln for ln in lines)

    @pytest.mark.asyncio
    async def test_tail_emits_error_event(
        self,
        authenticated_client: AsyncClient,
        _shared_app: FastAPI,
        httpx_mock: HTTPXMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """VL poll failure mid-stream → gen() emits 'event: error' SSE frame."""
        monkeypatch.setenv("HOMELAB_MONITOR_TAIL_POLL_MS", "10")
        monkeypatch.setenv("HOMELAB_MONITOR_TAIL_MAX_DURATION_S", "1")

        # First response (probe): 200 success.  Subsequent responses: 500 → ErrorEvent.
        httpx_mock.add_response(
            method="GET",
            url=re.compile(r"http://.*:9428/select/logsql/query.*"),
            status_code=200,
            text="",
        )
        httpx_mock.add_response(
            method="GET",
            url=re.compile(r"http://.*:9428/select/logsql/query.*"),
            status_code=500,
            text="internal error",
            is_optional=True,
            is_reusable=True,
        )

        with patch("asyncio.sleep", new_callable=AsyncMock):
            async with authenticated_client.stream(
                "GET",
                "/api/logs/tail?expr=test",
            ) as resp:
                assert resp.status_code == 200  # noqa: PLR2004

                lines: list[str] = []
                async for line in resp.aiter_lines():
                    lines.append(line)
                    if any(ln == "event: error" for ln in lines):
                        break

        assert any(ln == "event: error" for ln in lines)


class TestLogsHandlerTailKeepalive:
    """GET /logs/tail gen() emits keepalive SSE comment when idle >= 30s."""

    @pytest.mark.asyncio
    async def test_tail_emits_keepalive(
        self,
        authenticated_client: AsyncClient,
        _shared_app: FastAPI,
        httpx_mock: HTTPXMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Idle stream for >= 30s → gen() emits ': keepalive' SSE comment (lines 430-431)."""
        monkeypatch.setenv("HOMELAB_MONITOR_TAIL_POLL_MS", "10")
        # Duration cap set to 30s; the clock advances to T+31 starting from call #6
        # which is AFTER the keepalive fires on iteration 1. Iteration 2 sees
        # now=T+31 ≥ 30 and the duration cap closes the generator normally (no
        # CancelledError, so the ASGI transport sees a clean generator return).
        monkeypatch.setenv("HOMELAB_MONITOR_TAIL_MAX_DURATION_S", "30")

        # All polls return empty → no lines emitted → idle clock advances.
        httpx_mock.add_response(
            method="GET",
            url=re.compile(r"http://.*:9428/select/logsql/query.*"),
            status_code=200,
            text="",
            is_optional=True,
            is_reusable=True,
        )

        # Patch routers.logs datetime so the handler's inline clock
        # (lambda: datetime.now(UTC)) returns a time 31s ahead of `last_emit`
        # starting from call #6, making the keepalive fire on iteration 1 and
        # the duration cap fire on iteration 2 (clean generator return).
        #
        # Clock call sequence in logs_tail + TailSession.events():
        #   call #0: probe `now = datetime.now(UTC)` → T+0
        #   call #1: `started = clock()` → T+0
        #   call #2: `_iso_to_ns(_now_iso(clock))` internal clock call → T+0
        #   call #3: `last_emit = clock()` → T+0
        #   call #4: `budget_window_start = clock()` → T+0
        #   -- iteration 1 (after sleep #1) --
        #   call #5: `now = clock()` → T+0  (duration 0 < 30: ok)
        #   (empty poll, no lines)
        #   call #6: keepalive check `self._clock()` → T+31 (31 ≥ 30: FIRE)
        #   [yield KeepaliveEvent() → ': keepalive' SSE frame emitted]
        #   call #7: `last_emit = clock()` after keepalive → T+31
        #   -- iteration 2 (after sleep #2) --
        #   call #8: `now = clock()` → T+31  (duration T+31-T+0=31 ≥ 30: RETURN)
        # Generator returns cleanly; ASGI transport completes normally.
        from datetime import UTC, datetime, timedelta  # noqa: PLC0415

        _base = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        _call_n = {"n": 0}

        class _FakeDatetime:
            @staticmethod
            def now(tz: object = None) -> datetime:
                n = _call_n["n"]
                _call_n["n"] += 1
                # Calls 0-5: return T+0 (keep started/last_emit at base,
                # iteration 1's now stays below the duration cap).
                # Call 6+: return T+31 (keepalive fires, then duration cap fires).
                if n <= 5:  # noqa: PLR2004
                    return _base
                return _base + timedelta(seconds=31)

        with (
            patch("homelab_monitor.kernel.api.routers.logs.datetime", _FakeDatetime),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            async with authenticated_client.stream(
                "GET",
                "/api/logs/tail?expr=test",
            ) as resp:
                assert resp.status_code == 200  # noqa: PLR2004

                lines: list[str] = []
                async for raw_line in resp.aiter_lines():
                    lines.append(raw_line)
                    if any(ln == ": keepalive" for ln in lines):
                        break

        assert any(ln == ": keepalive" for ln in lines)


__all__ = []
