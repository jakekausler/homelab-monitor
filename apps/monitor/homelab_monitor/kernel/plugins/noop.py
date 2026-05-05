"""Noop collector — the simplest thing that satisfies :class:`Collector`.

Used by scheduler tests in later stages and as a smoke test that the contract
can actually be implemented.
"""

from __future__ import annotations

from datetime import timedelta
from typing import ClassVar

from homelab_monitor.kernel.plugins.base import BaseCollector
from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.types import CollectorResult


class NoopCollector(BaseCollector):
    """Does nothing successfully. ``metrics_emitted=0``, no events, no errors."""

    name: ClassVar[str] = "noop"
    interval: ClassVar[timedelta] = timedelta(seconds=60)
    timeout: ClassVar[timedelta] = timedelta(seconds=10)

    async def run(self, ctx: CollectorContext) -> CollectorResult:
        """Return an empty successful result; ``ctx`` is intentionally unused."""
        del ctx
        return CollectorResult(
            ok=True,
            metrics_emitted=0,
            errors=[],
            events=[],
            duration_seconds=0.0,
        )
