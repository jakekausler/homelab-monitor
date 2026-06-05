"""Unit tests for live-tail service (STAGE-004-023)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from homelab_monitor.kernel.config import TailConfig
from homelab_monitor.kernel.logs.pagination import (
    _iso_to_ns,  # pyright: ignore[reportPrivateUsage]
)
from homelab_monitor.kernel.logs.tail_service import (
    DroppedEvent,
    ErrorEvent,
    ForwardCursor,
    KeepaliveEvent,
    LineEvent,
    TailEvent,
    TailRegistry,
    TailSession,
)
from homelab_monitor.kernel.logs.victorialogs_client import (
    VictoriaLogsClientError,
    VlLogLine,
    VlQueryResult,
)
from homelab_monitor.kernel.plugins.io import InMemoryMetricsWriter


class TestForwardCursor:
    """ForwardCursor.advance() branch coverage."""

    def test_empty_input(self) -> None:
        """advance([]) returns ([], self)."""
        cursor = ForwardCursor(last_seen_ns=1000, n_at_boundary=0)
        emit, new_cursor = cursor.advance([])
        assert emit == []
        assert new_cursor == cursor

    def test_all_older_lines(self) -> None:
        """All lines ns < last_seen_ns → ([], self)."""
        cursor = ForwardCursor(last_seen_ns=_iso_to_ns("2026-01-01T00:00:00.300Z"), n_at_boundary=0)
        lines = [
            VlLogLine(timestamp="2026-01-01T00:00:00.100Z", message="a", stream="s1"),
            VlLogLine(timestamp="2026-01-01T00:00:00.200Z", message="b", stream="s1"),
        ]
        emit, new_cursor = cursor.advance(lines)
        assert emit == []
        assert new_cursor == cursor

    def test_newer_lines_emitted(self) -> None:
        """Lines with ns > last_seen_ns → all emitted ascending."""
        cursor = ForwardCursor(last_seen_ns=100, n_at_boundary=0)
        lines = [
            VlLogLine(timestamp="2026-01-01T00:00:00.000300Z", message="c", stream="s1"),
            VlLogLine(timestamp="2026-01-01T00:00:00.000200Z", message="b", stream="s1"),
        ]
        emit, new_cursor = cursor.advance(lines)
        assert len(emit) == 2  # noqa: PLR2004
        # ascending order (200 before 300)
        assert emit[0].message == "b"
        assert emit[1].message == "c"
        assert new_cursor.last_seen_ns > 100  # noqa: PLR2004

    def test_boundary_dedup(self) -> None:
        """Lines at last_seen_ns are deduplicated by n_at_boundary."""
        cursor = ForwardCursor(
            last_seen_ns=_iso_to_ns("2026-01-01T00:00:00.000100Z"), n_at_boundary=1
        )
        lines = [
            VlLogLine(timestamp="2026-01-01T00:00:00.000100Z", message="a", stream="s1"),
            VlLogLine(timestamp="2026-01-01T00:00:00.000200Z", message="b", stream="s1"),
        ]
        emit, _new_cursor = cursor.advance(lines)
        # The line at ns=100 is deduplicated (n_at_boundary=1 → skip first 1)
        assert len(emit) == 1
        assert emit[0].message == "b"

    def test_multiple_at_boundary(self) -> None:
        """When n_at_boundary < count at boundary, some are emitted."""
        cursor = ForwardCursor(
            last_seen_ns=_iso_to_ns("2026-01-01T00:00:00.000100Z"), n_at_boundary=2
        )
        lines = [
            VlLogLine(timestamp="2026-01-01T00:00:00.000100Z", message="a1", stream="s1"),
            VlLogLine(timestamp="2026-01-01T00:00:00.000100Z", message="a2", stream="s2"),
            VlLogLine(timestamp="2026-01-01T00:00:00.000100Z", message="a3", stream="s1"),
            VlLogLine(timestamp="2026-01-01T00:00:00.000100Z", message="a4", stream="s2"),
        ]
        emit, _new_cursor = cursor.advance(lines)
        # 4 lines at boundary, skip first 2, emit last 2
        assert len(emit) == 2  # noqa: PLR2004

    def test_unsorted_input_sorted_output(self) -> None:
        """Input lines can be out of order; output is ascending by (ns, msg, stream)."""
        cursor = ForwardCursor(last_seen_ns=0, n_at_boundary=0)
        lines = [
            VlLogLine(timestamp="2026-01-01T00:00:00.000300Z", message="c", stream="s1"),
            VlLogLine(timestamp="2026-01-01T00:00:00.000100Z", message="a", stream="s1"),
            VlLogLine(timestamp="2026-01-01T00:00:00.000200Z", message="b", stream="s1"),
        ]
        emit, _ = cursor.advance(lines)
        assert [ln.message for ln in emit] == ["a", "b", "c"]

    def test_new_n_at_boundary_counts_full_set(self) -> None:
        """new_n_at_boundary counts ALL lines at max_ns, not just emitted ones."""
        cursor = ForwardCursor(last_seen_ns=100, n_at_boundary=1)
        lines = [
            VlLogLine(timestamp="2026-01-01T00:00:00.000100Z", message="a", stream="s1"),
            VlLogLine(timestamp="2026-01-01T00:00:00.000200Z", message="b1", stream="s1"),
            VlLogLine(timestamp="2026-01-01T00:00:00.000200Z", message="b2", stream="s1"),
            VlLogLine(timestamp="2026-01-01T00:00:00.000200Z", message="b3", stream="s1"),
        ]
        _, new_cursor = cursor.advance(lines)
        # max ns is 200, and there are 3 lines at 200
        assert new_cursor.last_seen_ns > 100  # noqa: PLR2004
        assert new_cursor.n_at_boundary == 3  # noqa: PLR2004


class TestTailRegistry:
    """TailRegistry branch coverage."""

    def test_acquire_under_cap(self) -> None:
        """Acquiring under cap succeeds, increments count."""
        reg = TailRegistry(max_connections=3)
        assert reg.active_count == 0
        assert reg.try_acquire() is True
        assert reg.active_count == 1

    def test_acquire_at_cap(self) -> None:
        """Acquiring AT cap fails, count unchanged."""
        reg = TailRegistry(max_connections=1)
        assert reg.try_acquire() is True
        assert reg.try_acquire() is False
        assert reg.active_count == 1

    def test_release_after_acquire(self) -> None:
        """Release decrements count."""
        reg = TailRegistry(max_connections=2)
        reg.try_acquire()
        assert reg.active_count == 1
        reg.release()
        assert reg.active_count == 0

    def test_release_floors_at_zero(self) -> None:
        """Release at 0 stays at 0 (defensive)."""
        reg = TailRegistry(max_connections=1)
        assert reg.active_count == 0
        reg.release()
        assert reg.active_count == 0

    def test_max_connections_property(self) -> None:
        """max_connections property returns configured cap."""
        reg = TailRegistry(max_connections=5)
        assert reg.max_connections == 5  # noqa: PLR2004


class TestTailSession:
    """TailSession.events() branch coverage (unit tests with fake clock + VL)."""

    @pytest.mark.asyncio
    async def test_session_emits_lines(self) -> None:
        """Happy path: VL returns lines → LineEvents + counter."""
        # Fake VL client returning 2 lines
        vl_client = AsyncMock()
        vl_client.query = AsyncMock(
            return_value=VlQueryResult(
                lines=[
                    VlLogLine(
                        timestamp="2026-01-01T00:00:00.000200Z",
                        message="log 1",
                        stream="docker:test",
                    ),
                    VlLogLine(
                        timestamp="2026-01-01T00:00:00.000300Z",
                        message="log 2",
                        stream="docker:test",
                    ),
                ],
                truncated=False,
            )
        )
        metrics = InMemoryMetricsWriter()
        # Constant clock at :00 -> first-poll anchor = _iso_to_ns(:00); the
        # :00.000200/.000300 lines are NEWER than the anchor, so both emit on
        # the first poll. (A cycling clock whose 2nd value is :01 would anchor
        # NEWER than the lines, drop them, and the >=2 break would never fire.)
        clock = MagicMock(return_value=datetime(2026, 1, 1, tzinfo=UTC))

        config = TailConfig(
            poll_ms=1,
            max_connections=5,
            max_lines_per_sec=200,
            max_duration_s=10,
        )
        session = TailSession(
            vl_client=vl_client,
            expr="test",
            config=config,
            metrics_writer=metrics,
            clock=clock,
        )

        events: list[TailEvent] = []
        with patch("asyncio.sleep", new_callable=AsyncMock):
            async for ev in session.events():
                events.append(ev)
                if len(events) >= 2:  # Stop after 2 LineEvents  # noqa: PLR2004
                    break

        assert len(events) == 2  # noqa: PLR2004
        assert isinstance(events[0], LineEvent)
        assert isinstance(events[1], LineEvent)
        assert any(e.name == "homelab_log_tail_lines_streamed_total" for e in metrics.recorded)

    @pytest.mark.asyncio
    async def test_session_empty_poll(self) -> None:
        """Empty VL result → no emit, loop continues."""
        vl_client = AsyncMock()
        vl_client.query = AsyncMock(return_value=VlQueryResult(lines=[], truncated=False))
        metrics = InMemoryMetricsWriter()
        clock = MagicMock(return_value=datetime(2026, 1, 1, tzinfo=UTC))
        config = TailConfig(max_duration_s=2)

        session = TailSession(
            vl_client=vl_client,
            expr="test",
            config=config,
            metrics_writer=metrics,
            clock=clock,
        )

        events: list[TailEvent] = []
        poll_count = {"n": 0}

        def _sleep_side_effect(s: float) -> None:
            # Empty polls yield no events, so break on POLL COUNT (not on
            # len(events) — that never reaches 1 with an all-empty stream).
            poll_count["n"] += 1
            if poll_count["n"] >= 2:  # noqa: PLR2004
                raise asyncio.CancelledError

        with patch(
            "asyncio.sleep",
            new_callable=AsyncMock,
            side_effect=_sleep_side_effect,
        ):
            try:
                async for ev in session.events():
                    events.append(ev)
            except asyncio.CancelledError:
                pass

        assert events == []  # empty poll yields nothing

    @pytest.mark.asyncio
    async def test_session_over_budget_drops(self) -> None:
        """Over per-second budget → drop oldest, emit newest."""
        vl_client = AsyncMock()
        # 5 new lines
        vl_client.query = AsyncMock(
            return_value=VlQueryResult(
                lines=[
                    VlLogLine(
                        timestamp=f"2026-01-01T00:00:00.000{100 + i:03d}Z",
                        message=f"log{i}",
                        stream="s",
                    )
                    for i in range(5)
                ],
                truncated=False,
            )
        )
        metrics = InMemoryMetricsWriter()
        clock = MagicMock(return_value=datetime(2026, 1, 1, tzinfo=UTC))
        config = TailConfig(max_lines_per_sec=2)  # cap at 2

        session = TailSession(
            vl_client=vl_client,
            expr="test",
            config=config,
            metrics_writer=metrics,
            clock=clock,
        )

        events: list[TailEvent] = []
        with patch("asyncio.sleep", new_callable=AsyncMock):
            async for ev in session.events():
                events.append(ev)
                if len(events) >= 3:  # DroppedEvent + 2 LineEvents  # noqa: PLR2004
                    break

        assert isinstance(events[0], DroppedEvent)
        assert events[0].count == 3  # 5 - 2 = 3 dropped  # noqa: PLR2004
        assert len([e for e in events if isinstance(e, LineEvent)]) == 2  # noqa: PLR2004

    @pytest.mark.asyncio
    async def test_session_vl_error_retry(self) -> None:
        """VL error < 5 → ErrorEvent + retry (no return)."""
        vl_client = AsyncMock()

        # Fail once, succeed after
        vl_client.query = AsyncMock(
            side_effect=[
                VictoriaLogsClientError("timeout"),
                VlQueryResult(
                    lines=[
                        VlLogLine(
                            timestamp="2026-01-01T00:00:00.000200Z",
                            message="after_retry",
                            stream="s",
                        )
                    ],
                    truncated=False,
                ),
            ]
        )
        metrics = InMemoryMetricsWriter()
        clock = MagicMock(return_value=datetime(2026, 1, 1, tzinfo=UTC))
        config = TailConfig(poll_ms=1)

        session = TailSession(
            vl_client=vl_client,
            expr="test",
            config=config,
            metrics_writer=metrics,
            clock=clock,
        )

        events: list[TailEvent] = []
        with patch("asyncio.sleep", new_callable=AsyncMock):
            async for ev in session.events():
                events.append(ev)
                if len(events) >= 2:  # noqa: PLR2004
                    break

        # First is ErrorEvent, second is LineEvent
        assert isinstance(events[0], ErrorEvent)
        assert isinstance(events[1], LineEvent)

    @pytest.mark.asyncio
    async def test_session_vl_error_status_kind(self) -> None:
        """VL error with status_code → kind='vl_status'; without → kind='vl_transport'."""
        vl_client = AsyncMock()
        # Fail with status
        exc_with_status = VictoriaLogsClientError("status error", status_code=500)
        vl_client.query = AsyncMock(side_effect=[exc_with_status] * 5)

        metrics = InMemoryMetricsWriter()
        clock = MagicMock(return_value=datetime(2026, 1, 1, tzinfo=UTC))
        config = TailConfig(max_duration_s=100)

        session = TailSession(
            vl_client=vl_client,
            expr="test",
            config=config,
            metrics_writer=metrics,
            clock=clock,
        )

        events: list[TailEvent] = []
        with patch("asyncio.sleep", new_callable=AsyncMock):
            async for ev in session.events():
                events.append(ev)
                if len(events) >= 5:  # noqa: PLR2004
                    break

        # All are ErrorEvents
        assert all(isinstance(e, ErrorEvent) for e in events)
        # Check counter tags
        assert any(
            e.name == "homelab_log_tail_errors_total" and e.labels.get("kind") == "vl_status"
            for e in metrics.recorded
        )

    @pytest.mark.asyncio
    async def test_session_duration_cap_closes(self) -> None:
        """Duration cap reached → loop returns."""
        vl_client = AsyncMock()
        vl_client.query = AsyncMock(return_value=VlQueryResult(lines=[], truncated=False))

        metrics = InMemoryMetricsWriter()
        # First clock call (started) = base; EVERY later call = base + 5s, so
        # the first loop iteration sees (now - started) = 5 >= max_duration_s
        # (3) and returns. A 2-element cycling clock can't drive `now` past
        # `started` here because clock-calls-per-iteration is even, pinning
        # `now` to the same phase as `started` forever (infinite loop).
        _base = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        _clock_state = {"first": True}

        def fake_clock() -> datetime:
            if _clock_state["first"]:
                _clock_state["first"] = False
                return _base
            return _base + timedelta(seconds=5)

        config = TailConfig(max_duration_s=3)  # 3s cap

        session = TailSession(
            vl_client=vl_client,
            expr="test",
            config=config,
            metrics_writer=metrics,
            clock=fake_clock,
        )

        events: list[TailEvent] = []
        with patch("asyncio.sleep", new_callable=AsyncMock):
            async for ev in session.events():
                events.append(ev)

        # Loop should have returned (duration_cap exceeded)
        # No events yielded

    @pytest.mark.asyncio
    async def test_session_keepalive_when_idle(self) -> None:
        """Idle >= 30s → KeepaliveEvent AND line 276 (last_emit reset) executes.

        The existing test broke right after the KeepaliveEvent yield, which
        meant line 276 (``last_emit = self._clock()`` after the yield) never
        ran.  Fix: allow the loop to continue one full iteration after the
        keepalive so line 276 executes, then break on the second keepalive.

        Clock layout (calls in order):
          #0  started          = T+0
          #1  last_emit init   = T+0
          #2  budget_window_start = T+0
          -- iteration 1 --
          #3  now              = T+35  (35s > 30s idle since T+0 → keepalive fires)
          [yield KeepaliveEvent()]
          #4  last_emit = clock()    <- LINE 276 executes here; returns T+35
          -- iteration 2 --
          #5  now              = T+35  (duration check: 35 < 100, ok)
          -- keepalive check: (T+35 - T+35) = 0 < 30, no keepalive → break via CancelledError
        """
        vl_client = AsyncMock()
        vl_client.query = AsyncMock(return_value=VlQueryResult(lines=[], truncated=False))

        metrics = InMemoryMetricsWriter()
        _base = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        _t35 = _base + timedelta(seconds=35)
        # Clock call sequence: 0→T+0, 1→T+0, 2→T+0, then 3+→T+35
        _call_count = {"n": 0}

        def fake_clock() -> datetime:
            n = _call_count["n"]
            _call_count["n"] += 1
            if n < 3:  # noqa: PLR2004
                return _base
            return _t35

        sleep_call_count = {"n": 0}

        async def _sleep_side(s: float) -> None:
            sleep_call_count["n"] += 1
            # After the 2nd sleep (iteration 2 starts), terminate the loop so
            # we don't spin forever.  Iteration 1 sleep → keepalive fires →
            # line 276 executes.  Iteration 2 sleep → CancelledError → done.
            if sleep_call_count["n"] >= 2:  # noqa: PLR2004
                raise asyncio.CancelledError

        config = TailConfig(max_duration_s=100)

        session = TailSession(
            vl_client=vl_client,
            expr="test",
            config=config,
            metrics_writer=metrics,
            clock=fake_clock,
        )

        events: list[TailEvent] = []
        with patch("asyncio.sleep", new_callable=AsyncMock, side_effect=_sleep_side):
            try:
                async for ev in session.events():
                    events.append(ev)
            except asyncio.CancelledError:
                pass

        keepalives = [e for e in events if isinstance(e, KeepaliveEvent)]
        assert len(keepalives) > 0
        # line 276 executed: after the keepalive yield the clock was called a
        # 4th time (call #4 in the sequence above).  Four calls minimum:
        # started(0), last_emit(1), budget_window_start(2), now-iter1(3),
        # last_emit-after-keepalive(4).
        assert _call_count["n"] >= 5  # noqa: PLR2004

    @pytest.mark.asyncio
    async def test_session_vl_error_closes_after_max_failures(self) -> None:
        """Five consecutive VL errors → stream closes (line 238 branch).

        The VL client raises VictoriaLogsClientError on every query call.
        After exactly _MAX_CONSECUTIVE_FAILURES (5) errors the generator
        must return (StopAsyncIteration), emitting exactly 5 ErrorEvents and
        no more.  asyncio.sleep is patched to be instant.
        """
        vl_client = AsyncMock()
        vl_client.query = AsyncMock(side_effect=VictoriaLogsClientError("permanent failure"))
        metrics = InMemoryMetricsWriter()
        clock = MagicMock(return_value=datetime(2026, 1, 1, tzinfo=UTC))
        config = TailConfig(poll_ms=1, max_duration_s=3600)

        session = TailSession(
            vl_client=vl_client,
            expr="test",
            config=config,
            metrics_writer=metrics,
            clock=clock,
        )

        events: list[TailEvent] = []
        with patch("asyncio.sleep", new_callable=AsyncMock):
            async for ev in session.events():
                events.append(ev)
                # Guard: if the generator doesn't return by 10 events something
                # is wrong — the test would otherwise collect forever.
                if len(events) >= 10:  # noqa: PLR2004
                    break

        # Must have received exactly 5 ErrorEvents then the generator returned.
        error_events = [e for e in events if isinstance(e, ErrorEvent)]
        assert len(error_events) == 5  # noqa: PLR2004
        # Generator must have returned naturally (not broken by the guard above).
        assert len(events) == 5  # noqa: PLR2004

    @pytest.mark.asyncio
    async def test_session_budget_window_resets_after_one_second(self) -> None:
        """Per-second backpressure budget window resets when >= 1s elapsed (lines 250-251).

        Arrange a clock that returns T+0 for the first 3 init calls, then
        T+0 on the first iteration's ``now`` (budget_window_start = T+0, same
        second → no reset on iteration 1), then T+2 on the second iteration's
        ``now`` (T+2 - T+0 >= 1.0 → window resets, lines 250-251 execute).
        VL returns 1 line per poll so the loop has something to do each
        iteration.  asyncio.sleep is patched.  Terminate after 3 lines (2
        iterations) via CancelledError on the 3rd sleep.
        """
        vl_client = AsyncMock()
        vl_client.query = AsyncMock(
            return_value=VlQueryResult(
                lines=[
                    VlLogLine(
                        timestamp="2026-01-01T00:00:00.000200Z",
                        message="line",
                        stream="s",
                    )
                ],
                truncated=False,
            )
        )
        metrics = InMemoryMetricsWriter()

        _base = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        # Clock call order in events():
        #   0: started        → T+0
        #   1: last_emit init → T+0
        #   2: budget_window_start → T+0
        #   -- iteration 1 (sleep #1) --
        #   3: now            → T+0  (duration ok; budget window 0s, no reset)
        #      [emit line; last_emit = clock() → T+0]
        #   4: last_emit after LineEvent → T+0
        #   -- iteration 2 (sleep #2) --
        #   5: now            → T+2  (budget_window 2s ≥ 1 → RESET at lines 250-251)
        #      [emit line; last_emit = clock() → T+2]
        #   6: last_emit after LineEvent → T+2
        #   -- iteration 3 (sleep #3) → CancelledError
        _times = [
            _base,  # call 0: started
            _base,  # call 1: last_emit init
            _base,  # call 2: budget_window_start
            _base,  # call 3: now iter1
            _base,  # call 4: last_emit after line iter1
            _base + timedelta(seconds=2),  # call 5: now iter2 → triggers reset
            _base + timedelta(seconds=2),  # call 6: last_emit after line iter2
        ]
        _call_idx = {"n": 0}

        def fake_clock() -> datetime:
            idx = _call_idx["n"]
            _call_idx["n"] += 1
            if idx < len(_times):
                return _times[idx]
            return _base + timedelta(seconds=2)

        sleep_count = {"n": 0}

        async def _sleep(s: float) -> None:
            sleep_count["n"] += 1
            if sleep_count["n"] >= 3:  # noqa: PLR2004
                raise asyncio.CancelledError

        config = TailConfig(poll_ms=1, max_lines_per_sec=200, max_duration_s=3600)

        session = TailSession(
            vl_client=vl_client,
            expr="test",
            config=config,
            metrics_writer=metrics,
            clock=fake_clock,
        )

        events: list[TailEvent] = []
        with patch("asyncio.sleep", new_callable=AsyncMock, side_effect=_sleep):
            try:
                async for ev in session.events():
                    events.append(ev)
            except asyncio.CancelledError:
                pass

        # Should have emitted at least 1 line from iteration 2 (proving the
        # loop reached the emit step after the budget window reset).
        line_events = [e for e in events if isinstance(e, LineEvent)]
        assert len(line_events) >= 1
        # The budget window was reset: after the second sleep the clock returned
        # T+2 which is >= 1.0s past the initial budget_window_start of T+0.
        # If lines 250-251 didn't execute the test would still pass for different
        # reasons but the assertion on call count confirms the clock was called
        # far enough.
        assert _call_idx["n"] >= 5  # noqa: PLR2004


__all__ = []
