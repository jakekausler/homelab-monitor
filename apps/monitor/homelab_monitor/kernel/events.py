"""Event sink Protocol, trigger context, and scheduler tick events.

Decouples the scheduler (which emits events) from API-layer subscribers.
The EventSink Protocol is the inversion-of-control boundary.
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict


@dataclass(frozen=True, slots=True)
class TriggerContext:
    """Metadata about what triggered a collector tick.

    Used by request_immediate_run to annotate ticks with context (e.g.,
    which API request initiated a manual retry).
    """

    kind: Literal["scheduled", "retry", "manual"]
    request_id: str | None = None


class BaseEvent(BaseModel):
    """Base for all SSE event payloads. Subclasses MUST override `kind`.

    Concrete events declare `kind: Literal["..."]` to discriminate at the
    SSE wire layer; Pydantic supports Literal-narrowing of an inherited
    str field.
    """

    model_config = ConfigDict(extra="forbid")
    kind: str


class SchedulerTickEvent(BaseEvent):
    """Event published by scheduler after every tick outcome."""

    kind: Literal["collector.tick"] = "collector.tick"  # pyright: ignore[reportIncompatibleVariableOverride]
    collector: str
    tick_id: str
    outcome: Literal["success", "failure", "shutdown", "skipped"]
    reason: str | None = None  # group_busy/quarantined/timeout/exception/result_error
    duration_seconds: float | None = None
    trigger_kind: Literal["scheduled", "retry", "manual"] = "scheduled"
    request_id: str | None = None
    ts: str  # utc_now_iso()


@runtime_checkable
class EventSink(Protocol):
    """Inversion-of-control boundary for event subscribers.

    The scheduler publishes ticks without knowing who listens. The API layer
    (lifespan) injects an EventSink implementation (e.g., SseBroker) that
    handles delivery, buffering, and backpressure.

    Accepts any ``BaseEvent``-conforming value: scheduler ticks, alert firing,
    alert resolved, etc. The implementation dispatches on ``event.kind``.

    MUST NOT raise — the implementation is responsible for catching its own
    errors so scheduler ticks are never disturbed by sink failures.
    """

    async def publish(self, event: BaseEvent) -> None:
        """Publish an event to subscribers."""
        ...


class NullEventSink:
    """No-op EventSink for tests and contexts with no subscribers."""

    async def publish(self, event: BaseEvent) -> None:
        del event


def make_tick_id() -> str:
    """Generate a unique tick ID (uuid4 hex)."""
    return uuid.uuid4().hex


# Contextvar for threading tick metadata through the collector's run call.
_TickAttachment = tuple[str, TriggerContext | None] | None
_CURRENT_TICK: ContextVar[_TickAttachment] = ContextVar(
    "homelab_monitor_current_tick", default=None
)


def set_current_tick(tick_id: str, trigger: TriggerContext | None) -> Token[_TickAttachment]:
    """Bind the current tick to the contextvar.

    Returns a Token for cleanup via reset_current_tick (usually in a try/finally).
    """
    return _CURRENT_TICK.set((tick_id, trigger))


def reset_current_tick(token: Token[_TickAttachment]) -> None:
    """Reset the contextvar after the tick is done."""
    _CURRENT_TICK.reset(token)


def current_tick() -> _TickAttachment:
    """Fetch the current tick context (if any)."""
    return _CURRENT_TICK.get()
