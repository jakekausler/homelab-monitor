"""pihole_dns_split collector — STATELESS dual-path DNS resolution probe.

Resolves a fixed public name (``dns.google.com``, A record) over UDP :53 via TWO
paths every cycle:
- ``path="pihole"`` — through the Pi-hole resolver (``_pihole_host``/``_pihole_port``).
- ``path="direct"``  — directly against a WAN-bypass resolver (``_direct_host``/
  ``_direct_port``, default ``1.1.1.1:53``), bypassing Pi-hole entirely.

Reuses ``kernel/dns/resolver.py::resolve_a`` (one call per path) and the shared
outcome vocabulary in ``kernel/dns/outcomes.py``. This collector is a STATELESS
emitter: it never compares the two paths. Divergence / split detection is PromQL in
STAGE-006-016.

Emits the cross-epic ``homelab_dns_resolution_*`` family:
- homelab_dns_resolution_up{path}              gauge 1/0, ALWAYS for BOTH paths
  (1.0 iff DnsProbeResult.ok else 0.0).
- homelab_dns_resolution_seconds{path}         gauge latency, ONLY on a real
  round-trip (outcome in RESPONSE_OUTCOMES). OMITTED on no-response failures.
- homelab_dns_resolution_probe_result{path,outcome}  gauge 1.0, ALWAYS exactly one
  series per path naming the current outcome.

SCAFFOLDING: feeds the DNS split-check PromQL / alerts (STAGE-006-016) and Grafana.
"""

from __future__ import annotations

import time
from datetime import timedelta
from typing import ClassVar

from homelab_monitor.kernel.dns import (
    OUTCOME_BY_ERROR,
    PROBE_QNAME,
    RESPONSE_OUTCOMES,
    DnsProbeResult,
    resolve_a,
)
from homelab_monitor.kernel.plugins.base import BaseCollector
from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.types import CollectorResult

M_UP = "homelab_dns_resolution_up"
M_SECONDS = "homelab_dns_resolution_seconds"
M_PROBE_RESULT = "homelab_dns_resolution_probe_result"

PATH_PIHOLE = "pihole"
PATH_DIRECT = "direct"


class PiholeDnsSplitCollector(BaseCollector):
    """Dual-path (Pi-hole vs WAN-bypass) UDP :53 DNS A-record probe.

    Host/port for each path are injected post-construction by the lifespan
    (``_pihole_host``/``_pihole_port`` from ``PiholeConfig.dns_host``/``dns_port``;
    ``_direct_host``/``_direct_port`` from ``direct_dns_host``/``direct_dns_port``).
    ``_pihole_host`` defaults to "" until injected; an empty Pi-hole host fails that
    path closed (up=0.0, outcome="socket_error", no latency, resolve_a NOT called)
    while the direct path is STILL probed. The direct host is never empty
    (defaults to ``1.1.1.1``).
    """

    name: ClassVar[str] = "pihole_dns_split"
    interval: ClassVar[timedelta] = timedelta(seconds=60)
    timeout: ClassVar[timedelta] = timedelta(seconds=15)
    concurrency_group: ClassVar[str] = "pihole"

    def __init__(self) -> None:
        self._pihole_host: str = ""
        self._pihole_port: int = 53
        self._direct_host: str = "1.1.1.1"
        self._direct_port: int = 53

    async def run(self, ctx: CollectorContext) -> CollectorResult:
        start = time.monotonic()
        emitted: list[int] = [0]

        pihole_result = await self._probe_pihole()
        direct_result = await resolve_a(
            self._direct_host, PROBE_QNAME, port=self._direct_port, timeout_seconds=5.0
        )

        self._emit_path(ctx, PATH_PIHOLE, pihole_result, emitted)
        self._emit_path(ctx, PATH_DIRECT, direct_result, emitted)

        errors = [r.error for r in (pihole_result, direct_result) if r.error is not None]

        return CollectorResult(
            ok=pihole_result.ok and direct_result.ok,
            metrics_emitted=emitted[0],
            errors=errors,
            events=[],
            duration_seconds=time.monotonic() - start,
        )

    async def _probe_pihole(self) -> DnsProbeResult:
        """Probe the Pi-hole path, failing closed (socket_error) on empty host."""
        if not self._pihole_host:
            return DnsProbeResult(
                ok=False,
                rcode=0,
                truncated=False,
                latency_seconds=0.0,
                error="socket_error",
            )
        return await resolve_a(
            self._pihole_host, PROBE_QNAME, port=self._pihole_port, timeout_seconds=5.0
        )

    def _emit_path(
        self,
        ctx: CollectorContext,
        path: str,
        result: DnsProbeResult,
        emitted: list[int],
    ) -> None:
        """Emit the up/outcome/latency series for one path."""
        ctx.vm.write_gauge(M_UP, 1.0 if result.ok else 0.0, {"path": path})
        emitted[0] += 1

        outcome = OUTCOME_BY_ERROR[result.error]
        ctx.vm.write_gauge(M_PROBE_RESULT, 1.0, {"path": path, "outcome": outcome})
        emitted[0] += 1

        if outcome in RESPONSE_OUTCOMES:
            ctx.vm.write_gauge(M_SECONDS, result.latency_seconds, {"path": path})
            emitted[0] += 1
