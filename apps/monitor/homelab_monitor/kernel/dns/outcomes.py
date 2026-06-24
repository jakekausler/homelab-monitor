"""Shared DNS-probe outcome vocabulary (STAGE-006-015).

Extracted from STAGE-006-014's pihole.dns_health collector so the 014
(PiholeDnsHealthCollector) and 015 (PiholeDnsSplitCollector) collectors share ONE
definition of resolve_a's outcome semantics. These describe the DnsProbeResult.error
token -> outcome-label mapping and which outcomes represent a real round-trip (and
therefore carry a meaningful latency). Pure constants — stdlib typing only, no upward
imports — so importing them from collectors introduces no cycle.
"""

from __future__ import annotations

from typing import Final

# Probe target: a stable public A record. Shared by both DNS probe collectors.
PROBE_QNAME: Final[str] = "dns.google.com"

# DnsProbeResult.error token (None == ok) -> outcome label value. One-hot:
# exactly one outcome series emitted per probe.
OUTCOME_BY_ERROR: Final[dict[str | None, str]] = {
    None: "ok",
    "timeout": "timeout",
    "servfail": "servfail",
    "nxdomain": "nxdomain",
    "refused": "refused",
    "malformed": "malformed",
    "socket_error": "socket_error",
    "id_mismatch": "id_mismatch",
    "truncated": "truncated",
    "no_answer": "no_answer",
}

# Outcomes that represent a REAL round-trip (a response was received) -> emit latency.
# Everything else (timeout / socket_error / malformed / id_mismatch) -> OMIT latency.
RESPONSE_OUTCOMES: Final[frozenset[str]] = frozenset(
    {"ok", "servfail", "nxdomain", "refused", "no_answer", "truncated"}
)
