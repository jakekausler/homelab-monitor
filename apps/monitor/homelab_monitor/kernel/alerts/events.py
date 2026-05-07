"""Alert lifecycle events emitted on the EventSink.

Both events conform to the ``BaseEvent`` Protocol (Pydantic ``BaseModel`` with a
``kind`` discriminator literal). They are emitted by the alert ingest handler
(Spec B) and consumed by the dispatcher.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from homelab_monitor.kernel.events import BaseEvent


class AlertFiringEvent(BaseEvent):
    """Event emitted when an alert fires (new firing or re-firing of a known fingerprint)."""

    kind: Literal["alert.firing"] = "alert.firing"  # pyright: ignore[reportIncompatibleVariableOverride]
    alert_id: str
    fingerprint: str
    source_tool: str
    severity: str
    status: Literal["firing"] = "firing"
    opened_at: str
    last_seen_at: str
    labels: dict[str, str]
    annotations: dict[str, str] = Field(default_factory=dict)
    ts: str  # utc_now_iso() at publish time


class AlertResolvedEvent(BaseEvent):
    """Event emitted when an alert resolves."""

    kind: Literal["alert.resolved"] = "alert.resolved"  # pyright: ignore[reportIncompatibleVariableOverride]
    alert_id: str
    fingerprint: str
    source_tool: str
    severity: str
    resolved_at: str
    labels: dict[str, str]
    annotations: dict[str, str] = Field(default_factory=dict)
    ts: str
