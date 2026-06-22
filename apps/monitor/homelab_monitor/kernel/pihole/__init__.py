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
from homelab_monitor.kernel.pihole.clients import (
    ClassifiedClient,
    ClientClassification,
    ClientKind,
    RawClient,
    cap_domains,
    classify_clients,
    classify_one,
)
from homelab_monitor.kernel.pihole.errors import PiholeError, PiholeErrorReason
from homelab_monitor.kernel.pihole.unbound_control import (
    ExecCapture,
    UnboundError,
    UnboundErrorReason,
    UnboundStats,
    fetch_unbound_stats,
    parse_unbound_stats,
)

__all__ = [
    "ClassifiedClient",
    "ClientClassification",
    "ClientKind",
    "ExecCapture",
    "PiholeError",
    "PiholeErrorReason",
    "PiholeResponse",
    "PiholeRestClient",
    "PiholeSession",
    "RawClient",
    "UnboundError",
    "UnboundErrorReason",
    "UnboundStats",
    "cap_domains",
    "classify_clients",
    "classify_one",
    "fetch_unbound_stats",
    "parse_unbound_stats",
]
