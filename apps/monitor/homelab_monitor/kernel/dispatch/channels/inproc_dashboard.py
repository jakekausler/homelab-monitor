"""In-process dashboard channel: forwards alert events to the SSE broker."""

from __future__ import annotations

from typing import ClassVar

from homelab_monitor.kernel.dispatch.types import AlertEvent
from homelab_monitor.kernel.events import EventSink


class InprocDashboardChannel:
    """Dashboard channel that publishes alert events to the in-process SSE broker.

    Conforms to the ``Channel`` Protocol via duck-typing (``kind`` ClassVar +
    async ``deliver`` method). Constructor injects the broker so tests can
    swap in a fake EventSink without subclassing.
    """

    kind: ClassVar[str] = "inproc_dashboard"

    def __init__(self, broker: EventSink) -> None:
        self._broker = broker

    async def deliver(self, event: AlertEvent) -> None:
        """Forward the event to the SSE broker. MAY raise; dispatcher catches."""
        await self._broker.publish(event)
