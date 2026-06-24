"""pihole_config collector — query-logging enabled flag from GET /api/config.

Polls Pi-hole v6 ``GET /api/config`` and emits a single bare gauge
``homelab_pihole_query_logging_enabled`` (1.0 when ``config.dns.queryLogging`` is
True, else 0.0 — FAIL-CLOSED to 0.0 on any missing/mis-typed field) plus the
standard ``homelab_pihole_api_took_seconds{endpoint="config"}`` latency gauge.

The /api/config payload nests the flag two levels deep:
``{"config": {"dns": {"queryLogging": true, ...}, ...}, "took": ...}``.

FAIL-CLOSED: a missing ``config`` / ``dns`` object or a non-bool ``queryLogging``
yields 0.0 (logging assumed OFF) rather than skipping — this is a binary health
signal, not a count, so a defined 0 is the safe alert posture.

SCAFFOLDING: feeds alert rules (STAGE-006-016 family) + Grafana (STAGE-026).
"""

from __future__ import annotations

import time
from datetime import timedelta
from typing import ClassVar, cast

from homelab_monitor.kernel.pihole.errors import PiholeError
from homelab_monitor.kernel.plugins.base import BaseCollector
from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.types import CollectorResult

M_API_TOOK = "homelab_pihole_api_took_seconds"
M_QUERY_LOGGING = "homelab_pihole_query_logging_enabled"


def _query_logging_enabled(payload: object) -> float:
    """Extract config.dns.queryLogging as 1.0/0.0; fail-closed to 0.0 on any miss.

    All miss branches (payload not dict, config not dict, dns not dict,
    queryLogging not bool) collapse to 0.0 and are individually covered by tests.
    """
    if not isinstance(payload, dict):
        return 0.0
    config_obj = cast("dict[str, object]", payload).get("config")
    if not isinstance(config_obj, dict):
        return 0.0
    dns_obj = cast("dict[str, object]", config_obj).get("dns")
    if not isinstance(dns_obj, dict):
        return 0.0
    ql_obj = cast("dict[str, object]", dns_obj).get("queryLogging")
    if isinstance(ql_obj, bool):
        return 1.0 if ql_obj else 0.0
    return 0.0


class PiholeConfigCollector(BaseCollector):
    """Emit homelab_pihole_query_logging_enabled + api_took from GET /api/config.

    FAILURE SEMANTICS (mirrors PiholeBlockingCollector):
    - ctx.pihole is None -> ok=False, errors=["pihole client not configured"], 0 emits.
    - config() returns PiholeError -> ok=False, errors=[result.message], 0 emits.
    - success -> ok=True; emits api_took (1) + query_logging_enabled (1) = 2 metrics.
    """

    name: ClassVar[str] = "pihole_config"
    interval: ClassVar[timedelta] = timedelta(seconds=60)
    timeout: ClassVar[timedelta] = timedelta(seconds=15)
    concurrency_group: ClassVar[str] = "pihole"

    async def run(self, ctx: CollectorContext) -> CollectorResult:
        """Poll /api/config, emit gauges, return CollectorResult."""
        start = time.monotonic()

        if ctx.pihole is None:
            return CollectorResult(
                ok=False,
                metrics_emitted=0,
                errors=["pihole client not configured"],
                events=[],
                duration_seconds=time.monotonic() - start,
            )

        result = await ctx.pihole.config()
        if isinstance(result, PiholeError):
            return CollectorResult(
                ok=False,
                metrics_emitted=0,
                errors=[result.message],
                events=[],
                duration_seconds=time.monotonic() - start,
            )

        emitted = 0
        ctx.vm.write_gauge(M_API_TOOK, result.took_seconds, {"endpoint": result.endpoint})
        emitted += 1
        ctx.vm.write_gauge(M_QUERY_LOGGING, _query_logging_enabled(result.payload), {})
        emitted += 1

        return CollectorResult(
            ok=True,
            metrics_emitted=emitted,
            errors=[],
            events=[],
            duration_seconds=time.monotonic() - start,
        )
