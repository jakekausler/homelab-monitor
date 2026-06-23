"""pihole_dns_health collector — INDEPENDENT direct UDP :53 DNS health probe.

Resolves a fixed public name (``dns.google.com``, A record) directly against the
Pi-hole resolver's :53 listener over UDP — NOT via the Pi-hole REST API — so this
is a genuinely independent signal that DNS resolution works end to end.

Emits:
- homelab_pihole_up                {}  gauge 1/0, ALWAYS (1.0 iff DnsProbeResult.ok).
- homelab_pihole_dns_probe_seconds {}  gauge latency, ONLY on a real round-trip
  (response received: ok / servfail / nxdomain / refused / no_answer). OMITTED on
  no-response failures (timeout / socket_error / malformed / id_mismatch).
- homelab_pihole_dns_probe_result  {outcome="..."} gauge 1.0, ALWAYS exactly one
  series naming the current outcome.

SCAFFOLDING: feeds PiholeDnsProbeFailing alert rules (STAGE-006-016) and Grafana.
"""

from __future__ import annotations

import time
from datetime import timedelta
from typing import ClassVar, Final

from homelab_monitor.kernel.dns import DnsProbeResult, resolve_a
from homelab_monitor.kernel.plugins.base import BaseCollector
from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.types import CollectorResult

# Probe target: a stable public A record. Module-level constant per locked design.
_PROBE_QNAME: Final[str] = "dns.google.com"

M_UP = "homelab_pihole_up"
M_DNS_PROBE_SECONDS = "homelab_pihole_dns_probe_seconds"
M_DNS_PROBE_RESULT = "homelab_pihole_dns_probe_result"

# DnsProbeResult.error token (None == ok) -> outcome label value. One-hot precedent:
# gravity.py's _STATUS_NAMES. Exactly one outcome series emitted per run.
_OUTCOME_BY_ERROR: Final[dict[str | None, str]] = {
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
_RESPONSE_OUTCOMES: Final[frozenset[str]] = frozenset(
    {"ok", "servfail", "nxdomain", "refused", "no_answer", "truncated"}
)


class PiholeDnsHealthCollector(BaseCollector):
    """Direct UDP :53 DNS A-record probe against the Pi-hole resolver.

    The resolver host/port are injected post-construction by the lifespan
    (``_dns_host`` / ``_dns_port``), mirroring STAGE-006-013's UnboundStatsCollector
    wiring. ``_dns_host`` defaults to "" until injected; an empty host means the
    probe cannot run and the collector fails closed (up=0.0, outcome="socket_error").
    """

    name: ClassVar[str] = "pihole_dns_health"
    interval: ClassVar[timedelta] = timedelta(seconds=30)
    timeout: ClassVar[timedelta] = timedelta(seconds=15)
    concurrency_group: ClassVar[str] = "pihole"

    def __init__(self) -> None:
        self._dns_host: str = ""
        self._dns_port: int = 53

    async def run(self, ctx: CollectorContext) -> CollectorResult:
        start = time.monotonic()

        # Guard: resolver host not configured (lifespan injection missing / empty).
        if not self._dns_host:
            ctx.vm.write_gauge(M_UP, 0.0, {})
            ctx.vm.write_gauge(M_DNS_PROBE_RESULT, 1.0, {"outcome": "socket_error"})
            return CollectorResult(
                ok=False,
                metrics_emitted=2,
                errors=["dns_host not configured"],
                events=[],
                duration_seconds=time.monotonic() - start,
            )

        # Probe timeout: stay comfortably under the collector timeout (15s).
        result: DnsProbeResult = await resolve_a(
            self._dns_host, _PROBE_QNAME, port=self._dns_port, timeout_seconds=5.0
        )

        emitted: list[int] = [0]

        # --- up gauge (DNS-decisive; ALWAYS emitted) ---
        ctx.vm.write_gauge(M_UP, 1.0 if result.ok else 0.0, {})
        emitted[0] += 1

        # --- outcome one-hot (ALWAYS exactly one series) ---
        outcome = _OUTCOME_BY_ERROR[result.error]
        ctx.vm.write_gauge(M_DNS_PROBE_RESULT, 1.0, {"outcome": outcome})
        emitted[0] += 1

        # --- latency gauge (ONLY on a real round-trip) ---
        if outcome in _RESPONSE_OUTCOMES:
            ctx.vm.write_gauge(M_DNS_PROBE_SECONDS, result.latency_seconds, {})
            emitted[0] += 1

        return CollectorResult(
            ok=result.ok,
            metrics_emitted=emitted[0],
            errors=[] if result.error is None else [result.error],
            events=[],
            duration_seconds=time.monotonic() - start,
        )
