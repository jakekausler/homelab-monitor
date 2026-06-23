"""pihole_upstreams collector — per-upstream query counts from /api/stats/upstreams.

Polls Pi-hole v6 ``GET /api/stats/upstreams`` once per 30s and emits a gauge per
upstream DNS resolver showing its 24-hour rolling query count.

WINDOW SEMANTICS: ``count`` is a 24-hour ROLLING window gauge as reported by
Pi-hole FTL. It is NOT a monotonic counter — downstream consumers MUST NOT
apply ``rate()`` or ``increase()`` to this metric. Treat it as an instantaneous
snapshot of the 24h window.

PSEUDO-UPSTREAMS: Pi-hole includes two pseudo-upstreams in the list:
- ``ip="blocklist"  port=-1`` — queries answered by the block list
- ``ip="cache"      port=-1`` — queries answered from the DNS cache
These are identified by ``port == -1``. Their label is the bare ``ip`` string
(no ``#port`` suffix). Alert rules that detect upstream failures (e.g.
``UpstreamAllDown``, ``UpstreamDown`` in STAGE-006-016) MUST exclude
``upstream="cache"`` and ``upstream="blocklist"`` — these pseudo-upstreams are
always populated and do not represent real resolver health.

REAL UPSTREAMS: Entries with ``port != -1`` are real DNS resolvers. Their label
is ``{ip}#{port}`` (e.g. ``127.0.0.1#5335``).

INTENTIONALLY OMITTED:
- ``statistics.response`` / ``statistics.variance`` per-upstream latency fields —
  available if a future stage (e.g. STAGE-006-016 alerting) needs per-upstream
  latency histograms; not emitted here.
- Top-level ``total_queries`` / ``forwarded_queries`` — redundant with
  stats/summary fields already covered by STAGE-006-005.

NO CARDINALITY CAP: upstream cardinality is low and bounded by the number of
configured DNS resolvers (typically ≤ 5).

SCAFFOLDING: metrics consumed by alert stage 016 + Grafana dashboard STAGE-026.
The api-took gauge feeds the API-latency alerting introduced in STAGE-006-016.
"""

from __future__ import annotations

import time
from datetime import timedelta
from typing import ClassVar, cast

from homelab_monitor.kernel.pihole.errors import PiholeError
from homelab_monitor.kernel.plugins.base import BaseCollector
from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.types import CollectorResult
from homelab_monitor.plugins.collectors.integrations.pihole._parsing import emit_numeric

# ---------------------------------------------------------------------------
# Metric name constants
# ---------------------------------------------------------------------------

M_API_TOOK = "homelab_pihole_api_took_seconds"
M_UPSTREAM_QUERIES = "homelab_pihole_upstream_queries"


def _emit_upstream(
    ctx: CollectorContext,
    entry: object,
    emitted: list[int],
) -> None:
    """Emit homelab_pihole_upstream_queries for a single upstream entry.

    Silently skips entries that fail type narrowing. All skip branches are
    required for 100% branch coverage.
    """
    # 1. Entry must be a dict
    if not isinstance(entry, dict):
        return
    e = cast("dict[str, object]", entry)

    # 2. ip must be a str
    ip_obj = e.get("ip")
    if not isinstance(ip_obj, str):
        return

    # 3. port must be an int but NOT a bool (bool is a subclass of int in Python)
    port_obj = e.get("port")
    if not isinstance(port_obj, int) or isinstance(port_obj, bool):
        return

    # 4. Build upstream label
    upstream = ip_obj if port_obj == -1 else f"{ip_obj}#{port_obj}"

    # 5. Emit count (emit_numeric skips if non-numeric)
    emit_numeric(ctx, M_UPSTREAM_QUERIES, e.get("count"), {"upstream": upstream}, emitted)


class PiholeUpstreamsCollector(BaseCollector):
    """Emit per-upstream query counts from GET /api/stats/upstreams.

    Polls once per 30 seconds. Emits:
    - 1  api-took gauge            {endpoint="stats/upstreams"}
    - N  upstream-query gauges     {upstream="<ip>" or "<ip>#<port>"}

    On a healthy full-payload run with the reference Pi-hole (3 upstreams):
    metrics_emitted = 4 (1 api-took + 3 upstream counts).

    FAILURE SEMANTICS:
    - ctx.pihole is None → ok=False, errors=["pihole client not configured"], 0 emits.
    - stats_upstreams() returns PiholeError → ok=False, errors=[result.message], 0 emits.
    - payload not a dict → ok=False, errors=["unexpected payload shape"], 0 emits.
    - upstreams key missing or not a list → api-took already emitted; ok=True, partial.
    - individual entry fails narrowing → that entry silently skipped; others continue.
    - entry count non-numeric → that entry's metric skipped (handled inside emit_numeric).
    """

    name: ClassVar[str] = "pihole_upstreams"
    interval: ClassVar[timedelta] = timedelta(seconds=30)
    timeout: ClassVar[timedelta] = timedelta(seconds=15)
    concurrency_group: ClassVar[str] = "pihole"

    async def run(self, ctx: CollectorContext) -> CollectorResult:
        """Poll /api/stats/upstreams, emit gauges, return CollectorResult."""
        start = time.monotonic()

        # Guard: pihole client not configured
        if ctx.pihole is None:
            return CollectorResult(
                ok=False,
                metrics_emitted=0,
                errors=["pihole client not configured"],
                events=[],
                duration_seconds=time.monotonic() - start,
            )

        result = await ctx.pihole.stats_upstreams()

        # Guard: transport / auth / HTTP error
        if isinstance(result, PiholeError):
            return CollectorResult(
                ok=False,
                metrics_emitted=0,
                errors=[result.message],
                events=[],
                duration_seconds=time.monotonic() - start,
            )

        # Guard: payload shape — must be a dict
        raw_payload: object = result.payload
        if not isinstance(raw_payload, dict):
            return CollectorResult(
                ok=False,
                metrics_emitted=0,
                errors=["unexpected payload shape"],
                events=[],
                duration_seconds=time.monotonic() - start,
            )

        payload = cast("dict[str, object]", raw_payload)
        emitted: list[int] = [0]

        # --- api-took (always present when we have a successful response) ---
        ctx.vm.write_gauge(M_API_TOOK, result.took_seconds, {"endpoint": result.endpoint})
        emitted[0] += 1

        # --- upstreams list ---
        upstreams_obj = payload.get("upstreams")
        if not isinstance(upstreams_obj, list):
            # Missing key or non-list value — api_took already emitted; partial ok.
            return CollectorResult(
                ok=True,
                metrics_emitted=emitted[0],
                errors=[],
                events=[],
                duration_seconds=time.monotonic() - start,
            )

        upstreams = cast("list[object]", upstreams_obj)
        for entry in upstreams:
            _emit_upstream(ctx, entry, emitted)

        return CollectorResult(
            ok=True,
            metrics_emitted=emitted[0],
            errors=[],
            events=[],
            duration_seconds=time.monotonic() - start,
        )
