"""Alert domain types: enums, alert model, Alertmanager v2 webhook payload."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class Severity(StrEnum):
    """Alert severity. Spec §8 defines info/warning/critical for STAGE-013."""

    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class AlertStatus(StrEnum):
    """Alert lifecycle status."""

    FIRING = "firing"
    RESOLVED = "resolved"


class AlertOutcome(StrEnum):
    """Operator/system decision recorded against an alert."""

    ACKED = "acked"
    DISMISSED = "dismissed"
    AUTO_FIXED = "auto_fixed"
    ESCALATED = "escalated"


class Alert(BaseModel):
    """Hydrated alert row.

    ``payload`` is the JSON-decoded ``payload_json`` column (the original
    Alertmanager webhook body). ``labels`` and ``annotations`` are extracted
    from that payload at insert time for filtering/listing.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    fingerprint: str
    source_tool: str
    severity: Severity
    status: AlertStatus
    opened_at: str
    last_seen_at: str
    resolved_at: str | None = None
    ack_at: str | None = None
    ack_by: int | None = None
    runbook_id: str | None = None
    payload: dict[str, Any]
    labels: dict[str, str]
    annotations: dict[str, str] = Field(default_factory=dict)


class AlertmanagerV2AlertItem(BaseModel):
    """One alert entry inside an Alertmanager v2 webhook payload."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["firing", "resolved"]
    labels: dict[str, str]
    annotations: dict[str, str] = Field(default_factory=dict)
    startsAt: str  # -- matches Alertmanager wire format
    endsAt: str = ""
    generatorURL: str = ""
    fingerprint: str = ""


class AlertmanagerV2Payload(BaseModel):
    """Top-level Alertmanager v2 webhook payload.

    Only the fields STAGE-013 cares about are declared; ``extra="forbid"`` is
    intentional so unknown fields surface as bugs to fix instead of silent
    drops. Spec B (the ``/api/alerts/ingest`` handler) parses this.
    """

    model_config = ConfigDict(extra="forbid")

    version: str = "4"
    groupKey: str = ""
    status: Literal["firing", "resolved"]
    receiver: str = ""
    groupLabels: dict[str, str] = Field(default_factory=dict)
    commonLabels: dict[str, str] = Field(default_factory=dict)
    commonAnnotations: dict[str, str] = Field(default_factory=dict)
    externalURL: str = ""
    alerts: list[AlertmanagerV2AlertItem]
