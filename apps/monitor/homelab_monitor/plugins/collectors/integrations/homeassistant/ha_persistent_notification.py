"""ha_persistent_notification collector — active persistent notifications from HA (STAGE-005-012).

Privacy contract:
- Emits ONE metric label: ``notification_id``. The fields ``title``, ``message``,
  and ``created_at`` are NEVER read into any label, event, or log record.
- ``run()`` appends NO events carrying notification body text. Only the
  ``CappedEmitter``-appended cap-drop gauge event (carrying metric name + drop
  count) may appear in ``result.events``.
- ``run()`` makes NO log calls. This module has no structlog usage.
- This collector stores NOTHING in memory. No body cache, no DB writes.
  Body-sourcing for the panel is deferred to STAGE-005-021.

Each tick takes a ONE-SHOT ``persistent_notification/get`` snapshot over the
injected HA WebSocket client (NO subscription) and emits one cardinality-capped
gauge family:

- ``homelab_ha_persistent_notification{notification_id}`` — 1.0 for each current
  notification returned by HA.

The WS client is injected by the FastAPI lifespan AFTER construction (the
``HaConfigEntryCollector._ws`` / ``HaRepairsCollector._ws`` precedent), so
``self._ws`` is None until the lifespan wires it. A None / not-connected client
makes the tick a FAILED run (``ok=False``) — transient; the scheduler /
FailureBudget handles recovery.
"""

from __future__ import annotations

import time
from datetime import timedelta
from typing import TYPE_CHECKING, ClassVar, Final, cast

from homelab_monitor.kernel.config import load_cardinality_caps_config
from homelab_monitor.kernel.ha.errors import HaError
from homelab_monitor.kernel.ha.notifications import extract_notifications
from homelab_monitor.kernel.metrics.cardinality import CappedEmitter
from homelab_monitor.kernel.plugins.base import BaseCollector
from homelab_monitor.kernel.plugins.types import CollectorEvent, CollectorResult

if TYPE_CHECKING:
    from homelab_monitor.kernel.ha.websocket import HomeAssistantWebsocketClient
    from homelab_monitor.kernel.plugins.context import CollectorContext

# Metric family name.
M_PERSISTENT_NOTIFICATION: Final[str] = "homelab_ha_persistent_notification"

# WS command for the one-shot snapshot.
_WS_COMMAND: Final[str] = "persistent_notification/get"


def _notification_labels(notification: object) -> dict[str, str] | None:
    """Build the {notification_id} label-set for one notification, or None to SKIP.

    Privacy rule: ONLY ``notification_id`` is read. ``title``, ``message``,
    ``created_at``, and any other fields are NEVER accessed or returned.

    Skip when:
    - ``notification`` is not a dict.
    - ``notification_id`` is missing, empty, or non-str.
    """
    if not isinstance(notification, dict):
        return None
    notification_dict = cast("dict[str, object]", notification)

    # notification_id: required, non-empty str.
    nid_obj = notification_dict.get("notification_id")
    nid = nid_obj if isinstance(nid_obj, str) else ""
    if not nid:
        return None

    return {"notification_id": nid}


class HaPersistentNotificationCollector(BaseCollector):
    """Emit per-persistent-notification gauges from an HA WS snapshot."""

    name: ClassVar[str] = "ha_persistent_notification"
    interval: ClassVar[timedelta] = timedelta(seconds=60)
    timeout: ClassVar[timedelta] = timedelta(seconds=15)
    concurrency_group: ClassVar[str] = "homeassistant"

    def __init__(self) -> None:
        """Construct with no WS client; the lifespan injects ``self._ws``."""
        super().__init__()
        self._ws: HomeAssistantWebsocketClient | None = None

    async def run(self, ctx: CollectorContext) -> CollectorResult:
        """Snapshot persistent notifications over the WS and emit the gauge family."""
        start = time.monotonic()

        if self._ws is None:
            return CollectorResult(
                ok=False,
                metrics_emitted=0,
                errors=["ha websocket not configured"],
                events=[],
                duration_seconds=time.monotonic() - start,
            )
        if not self._ws.connected:
            return CollectorResult(
                ok=False,
                metrics_emitted=0,
                errors=["ha websocket not connected"],
                events=[],
                duration_seconds=time.monotonic() - start,
            )

        result = await self._ws.send_command(_WS_COMMAND)
        if isinstance(result, HaError):
            return CollectorResult(
                ok=False,
                metrics_emitted=0,
                errors=[result.message],
                events=[],
                duration_seconds=time.monotonic() - start,
            )

        notifications = extract_notifications(result)

        observations: list[tuple[dict[str, str], float]] = []
        for notification in notifications:
            labels = _notification_labels(notification)
            if labels is None:
                continue
            observations.append((labels, 1.0))

        caps = load_cardinality_caps_config()
        cap = caps.cap_for(M_PERSISTENT_NOTIFICATION)

        events: list[CollectorEvent] = []
        emitter = CappedEmitter(writer=ctx.vm, events=events)
        survivors = emitter.emit_family(M_PERSISTENT_NOTIFICATION, cap, observations)

        # emit_family writes ONE drop gauge -> +1 for the single family.
        metrics_emitted = survivors + 1

        return CollectorResult(
            ok=True,
            metrics_emitted=metrics_emitted,
            errors=[],
            events=events,
            duration_seconds=time.monotonic() - start,
        )
