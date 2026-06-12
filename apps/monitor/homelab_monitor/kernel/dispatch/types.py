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

    ``accepts`` is a sync pre-filter called by the dispatcher before
    ``deliver``. Return ``True`` to receive the event, ``False`` to skip.
    There is no default: ``accepts`` is a required member (a structural
    implementer that omits it fails conformance). ``InprocDashboardChannel``
    returns ``True`` (accept-all); ``HAPushChannel`` gates on severity.

    # TODO: STOPGAP — retire when EPIC-012 STAGE-012-005 lands (full routing engine)
    """

    kind: ClassVar[str]

    async def deliver(self, event: AlertEvent) -> None: ...

    def accepts(self, event: AlertEvent) -> bool: ...  # required — see concrete impls


class DeliveryResult(BaseModel):
    """Outcome of a single channel delivery attempt."""

    model_config = ConfigDict(extra="forbid")

    channel_kind: str
    ok: bool
    error: str | None = None
