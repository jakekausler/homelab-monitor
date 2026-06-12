"""HA mobile-push channel: delivers alert events to a Home Assistant notify service.

Conforms to the ``Channel`` Protocol via duck-typing (``kind`` ClassVar + async
``deliver``). Constructor injects the shared :class:`HomeAssistantRestClient`,
the notify-service target name, and a ``public_url_provider`` (defaults to the
module-level :func:`get_public_url`, read per-deliver so a post-startup
``HOMELAB_MONITOR_PUBLIC_URL`` is picked up without a restart).

Open-source-safe: when ``notify_service`` is empty the channel is a no-op — no HA
call is made — so a public release never targets a notify service that does not
exist on the operator's instance.

SECURITY: the bearer token is handled entirely inside the HA client and never
reaches this channel; the RuntimeError raised on failure is built from the
client's token-safe ``HaError.reason`` + ``status`` only.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import ClassVar

from homelab_monitor.kernel.alerts.events import AlertFiringEvent, AlertResolvedEvent
from homelab_monitor.kernel.config import get_public_url
from homelab_monitor.kernel.dispatch.types import AlertEvent
from homelab_monitor.kernel.ha.client import HomeAssistantRestClient
from homelab_monitor.kernel.ha.errors import HaError


class HAPushChannel:
    """Channel that pushes alert firing/resolved events to an HA notify service."""

    kind: ClassVar[str] = "ha_push"

    def __init__(
        self,
        client: HomeAssistantRestClient,
        notify_service: str,
        public_url_provider: Callable[[], str | None] = get_public_url,
    ) -> None:
        """Initialize the channel.

        Args:
            client: shared HA REST client (its token_provider handles auth).
            notify_service: HA ``notify`` target name (e.g. ``mobile_app_pixel``).
                Empty string => the channel is a no-op (no HA call).
            public_url_provider: zero-arg callable returning the monitor's public
                base URL or None. Defaults to :func:`get_public_url`; called per
                deliver so a rotated/late-set public URL is picked up live.
        """
        self._client = client
        self._notify_service = notify_service
        self._public_url_provider = public_url_provider

    def accepts(self, event: AlertEvent) -> bool:
        """Admit only error/critical-severity events (raw label, uncoerced).

        Uses the raw ``severity`` label to avoid any Severity-enum normalisation
        that could change case or canonical form. Fail-closed: missing label →
        False.

        # TODO: STOPGAP — retire when EPIC-012 STAGE-012-005 lands (full routing engine)
        """
        raw = (event.labels.get("severity") or "").strip().lower()
        return raw in {"error", "critical"}

    async def deliver(self, event: AlertEvent) -> None:
        """Push ``event`` to the HA notify service. MAY raise; dispatcher catches.

        No-op when ``notify_service`` is empty. Builds the notify payload from the
        event, calls ``notify.<notify_service>``, and raises RuntimeError with a
        token-safe message on an HaError result.
        """
        if not self._notify_service:
            return

        if isinstance(event, AlertResolvedEvent):
            title, message, data = self._build_resolved(event)
        else:
            title, message, data = self._build_firing(event)

        result = await self._client.call_service(
            "notify",
            self._notify_service,
            {"message": message, "title": title, "data": data},
        )
        if isinstance(result, HaError):
            msg = f"ha_push delivery failed: {result.reason} status={result.status}"
            raise RuntimeError(msg)

    def _build_firing(self, event: AlertFiringEvent) -> tuple[str, str, dict[str, object]]:
        """Build (title, message, data) for a firing event, with deep link."""
        alertname = event.labels.get("alertname") or "alert"
        summary = event.annotations.get("summary") or alertname
        desc = event.annotations.get("description") or summary
        title = f"[{event.severity.value.upper()}] {summary}"
        message = desc
        data: dict[str, object] = {
            "tag": event.fingerprint,
            "severity": event.severity.value,
            "group": event.labels.get("integration") or event.source_tool,
        }
        public_url = self._public_url_provider()
        if public_url is not None:
            explorer = event.annotations.get("explorer")
            base = public_url.rstrip("/")
            data["url"] = base + explorer if explorer else base + "/alerts/active"
        return title, message, data

    def _build_resolved(self, event: AlertResolvedEvent) -> tuple[str, str, dict[str, object]]:
        """Build (title, message, data) for a resolved event (no deep link)."""
        alertname = event.labels.get("alertname") or "alert"
        summary = event.annotations.get("summary") or alertname
        title = f"[RESOLVED] {summary}"
        message = f"{summary} resolved"
        data: dict[str, object] = {
            "tag": event.fingerprint,
            "severity": event.severity.value,
        }
        return title, message, data
