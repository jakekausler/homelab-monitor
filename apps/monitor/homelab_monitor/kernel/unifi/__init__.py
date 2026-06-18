"""Unifi client package (STAGE-007-001).

Read-only REST client + typed errors for reaching the UDM controller over the LAN
with a single read-only API key (``X-API-KEY``). Mirrors :mod:`homelab_monitor.kernel.ha`.
"""

from __future__ import annotations

from homelab_monitor.kernel.unifi.client import UnifiResponse, UnifiRestClient
from homelab_monitor.kernel.unifi.errors import UnifiError, UnifiErrorReason

__all__ = [
    "UnifiError",
    "UnifiErrorReason",
    "UnifiResponse",
    "UnifiRestClient",
]
