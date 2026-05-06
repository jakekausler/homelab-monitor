"""Tests for kernel/events.py — EventSink Protocol, TriggerContext, tick IDs, contextvar."""

from __future__ import annotations

from contextvars import Token
from dataclasses import FrozenInstanceError

import pytest

from homelab_monitor.kernel.events import (
    EventSink,
    NullEventSink,
    SchedulerTickEvent,
    TriggerContext,
    current_tick,
    make_tick_id,
    reset_current_tick,
    set_current_tick,
)


@pytest.mark.asyncio
async def test_null_event_sink_satisfies_protocol() -> None:
    """NullEventSink() satisfies isinstance(x, EventSink)."""
    sink = NullEventSink()
    assert isinstance(sink, EventSink)


@pytest.mark.asyncio
async def test_null_event_sink_publish_noop() -> None:
    """NullEventSink.publish(event) returns None and does not raise."""
    sink = NullEventSink()
    event = SchedulerTickEvent(
        collector="test", tick_id="abc123", outcome="success", ts="2026-05-05T00:00:00Z"
    )
    result = await sink.publish(event)
    assert result is None


def test_trigger_context_frozen() -> None:
    """TriggerContext is frozen (mutation raises FrozenInstanceError)."""
    ctx = TriggerContext(kind="scheduled", request_id=None)
    with pytest.raises(FrozenInstanceError):
        ctx.kind = "retry"  # type: ignore[misc]


def test_trigger_context_hashable() -> None:
    """TriggerContext is hashable (slots=True, frozen=True)."""
    ctx1 = TriggerContext(kind="scheduled", request_id=None)
    ctx2 = TriggerContext(kind="scheduled", request_id=None)
    # If hashable, can be added to a set
    s = {ctx1, ctx2}
    assert len(s) == 1  # Same content → same hash


def test_scheduler_tick_event_json_roundtrip() -> None:
    """SchedulerTickEvent.model_dump(mode='json') round-trips through JSON."""
    event = SchedulerTickEvent(
        collector="test-collector",
        tick_id="abc123def456",
        outcome="success",
        reason=None,
        duration_seconds=0.5,
        trigger_kind="manual",
        request_id="req-001",
        ts="2026-05-05T00:00:00Z",
    )
    dumped = event.model_dump(mode="json")
    # Should be JSON-serializable
    assert dumped["collector"] == "test-collector"
    assert dumped["tick_id"] == "abc123def456"
    assert dumped["outcome"] == "success"
    assert dumped["trigger_kind"] == "manual"
    assert dumped["kind"] == "collector.tick"


def test_make_tick_id_returns_hex32() -> None:
    """make_tick_id() returns hex32 strings."""
    tid = make_tick_id()
    assert isinstance(tid, str)
    assert len(tid) == 32  # noqa: PLR2004
    assert all(c in "0123456789abcdef" for c in tid)


def test_make_tick_id_unique() -> None:
    """make_tick_id() produces distinct values across calls."""
    ids = [make_tick_id() for _ in range(10)]
    assert len(set(ids)) == 10  # noqa: PLR2004


def test_set_current_tick_returns_token() -> None:
    """set_current_tick returns a Token for later reset."""
    tick_id = "abc123"
    trigger = TriggerContext(kind="retry", request_id="req-001")
    token = set_current_tick(tick_id, trigger)
    assert isinstance(token, Token)
    reset_current_tick(token)


def test_current_tick_after_set() -> None:
    """current_tick() returns the bound tick after set_current_tick."""
    tick_id = "abc123"
    trigger = TriggerContext(kind="manual", request_id=None)
    token = set_current_tick(tick_id, trigger)
    try:
        result = current_tick()
        assert result is not None
        returned_id, returned_trigger = result
        assert returned_id == tick_id
        assert returned_trigger == trigger
    finally:
        reset_current_tick(token)


def test_reset_current_tick_clears_binding() -> None:
    """reset_current_tick(token) clears the contextvar."""
    tick_id = "abc123"
    trigger = TriggerContext(kind="scheduled")
    token = set_current_tick(tick_id, trigger)
    reset_current_tick(token)
    result = current_tick()
    assert result is None


def test_set_current_tick_nested_isolation() -> None:
    """Nested set_current_tick calls with reset preserve outer context."""
    outer_id = "outer123"
    outer_trigger = TriggerContext(kind="scheduled")
    outer_token = set_current_tick(outer_id, outer_trigger)

    try:
        # Nested set
        inner_id = "inner456"
        inner_trigger = TriggerContext(kind="retry", request_id="req-001")
        inner_token = set_current_tick(inner_id, inner_trigger)

        try:
            # Inside inner context
            result = current_tick()
            assert result is not None
            returned_id, returned_trigger = result
            assert returned_id == inner_id
            assert returned_trigger == inner_trigger
        finally:
            reset_current_tick(inner_token)

        # Back to outer context
        result = current_tick()
        assert result is not None
        returned_id, returned_trigger = result
        assert returned_id == outer_id
        assert returned_trigger == outer_trigger
    finally:
        reset_current_tick(outer_token)


def test_current_tick_default_none() -> None:
    """current_tick() returns None when no tick is set."""
    # This test assumes no other test has set a tick in this thread
    result = current_tick()
    assert result is None


def test_trigger_context_with_all_fields() -> None:
    """TriggerContext can be created with all field combinations."""
    # scheduled, no request_id
    ctx1 = TriggerContext(kind="scheduled")
    assert ctx1.kind == "scheduled"
    assert ctx1.request_id is None

    # retry with request_id
    ctx2 = TriggerContext(kind="retry", request_id="req-123")
    assert ctx2.kind == "retry"
    assert ctx2.request_id == "req-123"

    # manual with request_id
    ctx3 = TriggerContext(kind="manual", request_id="req-456")
    assert ctx3.kind == "manual"
    assert ctx3.request_id == "req-456"


def test_scheduler_tick_event_extra_forbid() -> None:
    """SchedulerTickEvent has extra='forbid' so extra fields are rejected."""
    with pytest.raises(ValueError):  # pydantic validation error
        SchedulerTickEvent(
            collector="test",
            tick_id="abc",
            outcome="success",
            ts="2026-05-05T00:00:00Z",
            extra_field="should_fail",  # type: ignore[call-arg]
        )
