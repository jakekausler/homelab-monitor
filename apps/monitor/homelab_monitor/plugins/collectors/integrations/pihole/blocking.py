"""pihole_blocking collector — DNS blocking state from /api/dns/blocking.

Polls Pi-hole v6 ``GET /api/dns/blocking`` once per 30s and emits:
- 1 api-took gauge            {endpoint="dns/blocking"}
- 1 blocking-enabled gauge    {} (1.0 if blocking=="enabled", else 0.0;
  fail-closed)
- 0 or 1 timer gauge          {} (emitted only when remaining seconds are
  known; omitted when null/missing/non-numeric)

BLOCKING STATE: ``blocking`` is a string enum from FTL:
  "enabled"  → gauge=1.0
  "disabled" → gauge=0.0
  "failed"   → gauge=0.0  (fail-closed)
  "unknown"  → gauge=0.0  (fail-closed)
  anything else / non-string / missing → gauge=0.0  (fail-closed)

TIMER SEMANTICS: ``timer`` is remaining seconds (number) when temporarily
disabled, null when not applicable. Omit entirely when null/missing/non-numeric
(do NOT emit 0). bool is excluded (as_float rejects bool).

SCAFFOLDING: feeds alert rules in STAGE-006-016 and Grafana in STAGE-026.
"""

from __future__ import annotations

import time
from datetime import timedelta
from typing import ClassVar, cast

from homelab_monitor.kernel.pihole.errors import PiholeError
from homelab_monitor.kernel.plugins.base import BaseCollector
from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.types import CollectorResult
from homelab_monitor.plugins.collectors.integrations.pihole._parsing import as_float

M_API_TOOK = "homelab_pihole_api_took_seconds"
M_BLOCKING_ENABLED = "homelab_pihole_blocking_enabled"
M_BLOCKING_TIMER = "homelab_pihole_blocking_timer_seconds"


class PiholeBlockingCollector(BaseCollector):
    """Emit DNS blocking state from GET /api/dns/blocking.

    Polls once per 30 seconds. Emits:
    - 1  api-took gauge              {endpoint="dns/blocking"}
    - 1  blocking-enabled gauge      {} (1.0=enabled, 0.0=anything else)
    - 0-1 blocking-timer gauge       {} (seconds remaining; omitted when null)

    FAILURE SEMANTICS:
    - ctx.pihole is None → ok=False, errors=["pihole client not configured"],
      0 emits.
    - dns_blocking() returns PiholeError → ok=False, errors=[result.message],
      0 emits.
    - payload not a dict → ok=False, errors=["unexpected payload shape"],
      metrics_emitted=1 (api_took already counted).
    """

    name: ClassVar[str] = "pihole_blocking"
    interval: ClassVar[timedelta] = timedelta(seconds=30)
    timeout: ClassVar[timedelta] = timedelta(seconds=15)
    concurrency_group: ClassVar[str] = "pihole"

    async def run(self, ctx: CollectorContext) -> CollectorResult:
        """Poll /api/dns/blocking, emit gauges, return CollectorResult."""
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

        result = await ctx.pihole.dns_blocking()

        # Guard: transport / auth / HTTP error
        if isinstance(result, PiholeError):
            return CollectorResult(
                ok=False,
                metrics_emitted=0,
                errors=[result.message],
                events=[],
                duration_seconds=time.monotonic() - start,
            )

        emitted: list[int] = [0]

        # --- api-took (always present when we have a successful response) ---
        ctx.vm.write_gauge(M_API_TOOK, result.took_seconds, {"endpoint": result.endpoint})
        emitted[0] += 1

        # Guard: payload shape — must be a dict
        raw_payload: object = result.payload
        if not isinstance(raw_payload, dict):
            return CollectorResult(
                ok=False,
                metrics_emitted=emitted[0],
                errors=["unexpected payload shape"],
                events=[],
                duration_seconds=time.monotonic() - start,
            )

        payload = cast("dict[str, object]", raw_payload)

        # --- blocking enabled gauge (fail-closed: only "enabled" → 1.0) ---
        blocking_obj = payload.get("blocking")
        enabled = 1.0 if (isinstance(blocking_obj, str) and blocking_obj == "enabled") else 0.0
        ctx.vm.write_gauge(M_BLOCKING_ENABLED, enabled, {})
        emitted[0] += 1

        # --- timer gauge (omit when null/missing/non-numeric/bool) ---
        timer_val = as_float(payload.get("timer"))
        if timer_val is not None:
            ctx.vm.write_gauge(M_BLOCKING_TIMER, timer_val, {})
            emitted[0] += 1

        return CollectorResult(
            ok=True,
            metrics_emitted=emitted[0],
            errors=[],
            events=[],
            duration_seconds=time.monotonic() - start,
        )
