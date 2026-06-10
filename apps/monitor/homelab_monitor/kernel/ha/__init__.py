"""Home Assistant client package (STAGE-005-001).

REST client + typed errors for reaching Home Assistant over the LAN with a
long-lived bearer token. The websocket client (STAGE-005-002) lands beside this.
"""

from __future__ import annotations

from homelab_monitor.kernel.ha.client import (
    HaConfigResult,
    HaErrorLogResult,
    HaServiceResult,
    HaState,
    HomeAssistantRestClient,
)
from homelab_monitor.kernel.ha.errors import HaError, HaErrorReason
from homelab_monitor.kernel.ha.websocket import (
    HomeAssistantWebsocketClient,
    Subscription,
)

__all__ = [
    "HaConfigResult",
    "HaError",
    "HaErrorLogResult",
    "HaErrorReason",
    "HaServiceResult",
    "HaState",
    "HomeAssistantRestClient",
    "HomeAssistantWebsocketClient",
    "Subscription",
]
