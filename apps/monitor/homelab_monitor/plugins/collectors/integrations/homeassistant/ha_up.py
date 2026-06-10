"""ha_up collector — probes Home Assistant reachability via GET /api/config.

Emits ``homelab_ha_up`` = 1.0 when HA answers ``/api/config`` successfully,
0.0 when the probe completes but HA is down / unreachable / auth-rejected
(an :class:`HaError`), and 0.0 when no HA client is wired (defensive).

OK SEMANTICS (load-bearing — see STAGE-005-003): a probe that COMPLETES and
finds HA down is a SUCCESSFUL collector run. ``get_config()`` is return-not-raise
(never propagates), so the run never fails; ``homelab_ha_up=0`` carries the
"HA is down" signal, NOT ``CollectorResult.ok=False``. ``ok=False`` is reserved
for the collector itself failing, which cannot happen here. The metric is always
emitted (0, never absent) so STAGE-005-015's alert rule distinguishes "HA down"
from "collector never ran".
"""

from __future__ import annotations

import time
from datetime import timedelta
from typing import ClassVar

from homelab_monitor.kernel.ha.errors import HaError
from homelab_monitor.kernel.plugins.base import BaseCollector
from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.types import CollectorResult


class HaUpCollector(BaseCollector):
    """Emit ``homelab_ha_up`` (1.0 reachable / 0.0 down) once per interval."""

    name: ClassVar[str] = "ha_up"
    interval: ClassVar[timedelta] = timedelta(seconds=30)
    timeout: ClassVar[timedelta] = timedelta(seconds=10)
    concurrency_group: ClassVar[str] = "homeassistant"

    async def run(self, ctx: CollectorContext) -> CollectorResult:
        """Probe HA once and emit ``homelab_ha_up``. Always ok=True (see module docstring)."""
        start = time.monotonic()
        if ctx.ha is None:
            # ctx.ha is always wired by lifespan; this guard satisfies pyright
            # (Optional type) and is defensive — treat "no client" as down.
            up = 0.0
        else:
            result = await ctx.ha.get_config()
            up = 0.0 if isinstance(result, HaError) else 1.0
        ctx.vm.write_gauge("homelab_ha_up", up, {})
        return CollectorResult(
            ok=True,
            metrics_emitted=1,
            errors=[],
            events=[],
            duration_seconds=time.monotonic() - start,
        )
