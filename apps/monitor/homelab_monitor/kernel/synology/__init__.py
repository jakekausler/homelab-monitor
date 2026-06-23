"""Synology DSM client package (STAGE-008-001).

Synology DSM v7 REST client + typed errors for reaching the NAS over a self-signed
HTTPS endpoint with session auth (``SYNO.API.Auth`` v7 ``method=login`` -> ``sid``,
re-auth on DSM error 119). Mirrors :mod:`homelab_monitor.kernel.pihole` and
:mod:`homelab_monitor.kernel.unifi`.
"""

from __future__ import annotations

from homelab_monitor.kernel.synology.client import (
    SynologyResponse,
    SynologyRestClient,
    SynologySession,
)
from homelab_monitor.kernel.synology.errors import SynologyError, SynologyErrorReason

__all__ = [
    "SynologyError",
    "SynologyErrorReason",
    "SynologyResponse",
    "SynologyRestClient",
    "SynologySession",
]
