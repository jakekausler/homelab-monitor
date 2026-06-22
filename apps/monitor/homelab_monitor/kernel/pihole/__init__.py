"""Pi-hole client package (STAGE-006-001).

Pi-hole v6 REST client + typed errors for reaching a Pi-hole instance over the LAN
with session auth (``POST /api/auth`` -> ``X-FTL-SID``). Mirrors
:mod:`homelab_monitor.kernel.unifi` and :mod:`homelab_monitor.kernel.ha`.
"""

from __future__ import annotations

from homelab_monitor.kernel.pihole.client import (
    PiholeResponse,
    PiholeRestClient,
    PiholeSession,
)
from homelab_monitor.kernel.pihole.errors import PiholeError, PiholeErrorReason

__all__ = [
    "PiholeError",
    "PiholeErrorReason",
    "PiholeResponse",
    "PiholeRestClient",
    "PiholeSession",
]
