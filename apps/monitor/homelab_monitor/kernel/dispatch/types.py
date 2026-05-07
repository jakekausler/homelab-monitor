"""Channel Protocol and DeliveryResult for the alert dispatcher."""

from __future__ import annotations

from typing import ClassVar, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from homelab_monitor.kernel.alerts.events import AlertFiringEvent, AlertResolvedEvent

# Type alias documenting the two event types channels handle.
type AlertEvent = AlertFiringEvent | AlertResolvedEvent


@runtime_checkable
class Channel(Protocol):
    """Delivery channel for alert events.

    Implementations advertise their kind via ``kind`` (used for log + counter
    keys) and implement ``deliver``. ``deliver`` MAY raise; the dispatcher
    catches and records the failure.
    """

    kind: ClassVar[str]

    async def deliver(self, event: AlertEvent) -> None: ...


class DeliveryResult(BaseModel):
    """Outcome of a single channel delivery attempt."""

    model_config = ConfigDict(extra="forbid")

    channel_kind: str
    ok: bool
    error: str | None = None
