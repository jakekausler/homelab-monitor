"""HAEventChannel — push opted-in alert firing/resolved events to HA's event bus.

STAGE-005-020. Mirrors HAPushChannel (STAGE-005-017) but instead of calling a
notify service, it POSTs an event to Home Assistant's event bus (``fire_event``)
so the operator's own HA automations can react to homelab alerts.

Two gates:
  1. Global enable: ``HaConfig.event_type`` (env ``HOMELAB_MONITOR_HA_EVENT_TYPE``).
     Empty string -> channel is globally OFF (open-source-safe default).
  2. Per-alert opt-in: the ``push_to_ha=="true"`` label on the alert.

STOPGAP — the per-alert label gate is provisional. EPIC-012 STAGE-012-005 (the
full routing engine) will absorb this opt-in into routing_rules; retire the
label gate when that lands. This mirrors the STOPGAP note on HAPushChannel.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import ClassVar

from homelab_monitor.kernel.alerts.events import AlertResolvedEvent
from homelab_monitor.kernel.config import get_public_url
from homelab_monitor.kernel.dispatch.types import AlertEvent
from homelab_monitor.kernel.ha.client import HomeAssistantRestClient
from homelab_monitor.kernel.ha.errors import HaError


class HAEventChannel:
    """Channel that fires opted-in alert events onto HA's event bus."""

    kind: ClassVar[str] = "ha_event"

    def __init__(
        self,
        client: HomeAssistantRestClient,
        event_type: str,
        public_url_provider: Callable[[], str | None] = get_public_url,
    ) -> None:
        self._client = client
        self._event_type = event_type
        self._public_url_provider = public_url_provider

    def accepts(self, event: AlertEvent) -> bool:
        # Gate 1: global enable. Empty event_type -> channel OFF (no HA call).
        # Gate 2: per-alert opt-in via the push_to_ha label.
        # TODO: STOPGAP — retire the label gate when EPIC-012 STAGE-012-005 lands
        # (full routing engine absorbs the opt-in into routing_rules).
        if not self._event_type:
            return False
        return event.labels.get("push_to_ha") == "true"

    async def deliver(self, event: AlertEvent) -> None:
        data = self._build_data(event)
        result = await self._client.fire_event(self._event_type, data)
        if isinstance(result, HaError):
            msg = f"ha_event delivery failed: {result.reason} status={result.status}"
            raise RuntimeError(msg)

    def _build_data(self, event: AlertEvent) -> dict[str, str]:
        status = "resolved" if isinstance(event, AlertResolvedEvent) else "firing"
        data: dict[str, str] = {
            "status": status,
            "fingerprint": event.fingerprint,
            "alertname": event.labels.get("alertname", ""),
            "severity": event.severity.value,
            "summary": event.annotations.get("summary", ""),
            "source_tool": event.source_tool,
        }
        # Deep-link guard: get_public_url() returns str | None. To keep this a
        # clean dict[str, str], OMIT the url key entirely when None (mirrors
        # HAPushChannel._build_firing). Flat dashboard link — no explorer path.
        public_url = self._public_url_provider()
        if public_url is not None:
            data["url"] = public_url.rstrip("/") + "/alerts/active"
        return data
