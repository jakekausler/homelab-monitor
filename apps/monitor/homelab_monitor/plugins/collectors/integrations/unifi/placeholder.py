"""SCAFFOLDING (STAGE-007-002) — throwaway Unifi bundle placeholder collector.

Proves the Unifi integration bundle loads end-to-end: it registers, the
scheduler runs it, and ``homelab_unifi_bundle_loaded`` becomes visible in the
collector status API. It makes NO controller calls (never touches ``ctx.unifi``)
and therefore stays in the ``"default"`` concurrency group — it must not consume
the ``"unifi"`` group's serialization budget that real Wave-B collectors share.

REMOVAL: STAGE-007-005 (the first Wave-B device collector) deletes this file and
drops ``UnifiPlaceholderCollector`` from ``_UNIFI_COLLECTORS`` in the bundle
``__init__.py``.

OK SEMANTICS: the run always succeeds (``ok=True``); the single static gauge is
always emitted. Mirrors ``ha_up``'s always-ok=True convention.
"""

from __future__ import annotations

import time
from datetime import timedelta
from typing import ClassVar

from homelab_monitor.kernel.plugins.base import BaseCollector
from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.types import CollectorResult


class UnifiPlaceholderCollector(BaseCollector):
    """Emit ``homelab_unifi_bundle_loaded`` = 1.0 once per interval (no-op scaffolding)."""

    name: ClassVar[str] = "unifi_placeholder"
    interval: ClassVar[timedelta] = timedelta(seconds=60)
    timeout: ClassVar[timedelta] = timedelta(seconds=5)
    concurrency_group: ClassVar[str] = "default"

    async def run(self, ctx: CollectorContext) -> CollectorResult:
        """Emit the static bundle-loaded gauge. Always ok=True; never touches ctx.unifi."""
        start = time.monotonic()
        ctx.vm.write_gauge("homelab_unifi_bundle_loaded", 1.0, {})
        return CollectorResult(
            ok=True,
            metrics_emitted=1,
            errors=[],
            events=[],
            duration_seconds=time.monotonic() - start,
        )
