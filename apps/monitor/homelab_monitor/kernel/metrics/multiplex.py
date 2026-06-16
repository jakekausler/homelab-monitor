"""Multiplex MetricsWriter that fans out every write to N inner writers.

Used in production to dual-write to:

1. ``MemoryRetainingMetricsWriter`` (so ``/api/metrics/snapshot`` keeps working
   without a round-trip to VictoriaMetrics — the Overview tile is the consumer).
2. ``PrometheusRegistryWriter`` (so vmagent can scrape ``/metrics`` and ship
   samples to VictoriaMetrics for long-horizon storage + range queries).

Order: writers are visited in registration order. ``replace_family`` is
forwarded only to writers that implement it (duck-typed via ``getattr``);
writers without it are silently skipped for the family-replacement semantics.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Protocol, runtime_checkable

from homelab_monitor.kernel.plugins.io import MetricsWriter


@runtime_checkable
class _SupportsReplaceFamily(Protocol):  # pyright: ignore[reportUnusedClass]
    """Subset protocol for writers that support atomic family swaps."""

    def replace_family(self, name: str, entries: list[tuple[float, dict[str, str]]]) -> None:
        """Atomically wipe and rewrite a metric family."""
        ...


class MultiplexMetricsWriter:
    """Fan-out :class:`MetricsWriter` that delegates to N inner writers.

    Implements the :class:`MetricsWriter` Protocol structurally. Also forwards
    ``replace_family`` to any inner writer that supports it (in registration
    order). Writers that do not implement ``replace_family`` are skipped for
    that call but still receive ``write_gauge`` / ``write_counter`` /
    ``write_summary`` normally.
    """

    def __init__(self, writers: Sequence[MetricsWriter]) -> None:
        self._writers: list[MetricsWriter] = list(writers)

    def _fanout(self, op: Callable[[MetricsWriter], None]) -> None:
        for w in self._writers:
            op(w)

    def write_gauge(self, name: str, value: float, labels: dict[str, str]) -> None:
        """Fan-out a gauge observation to every inner writer."""
        self._fanout(lambda w: w.write_gauge(name, value, labels))

    def write_counter(self, name: str, value: float, labels: dict[str, str]) -> None:
        """Fan-out a counter increment to every inner writer."""
        self._fanout(lambda w: w.write_counter(name, value, labels))

    def write_counter_absolute(self, name: str, value: float, labels: dict[str, str]) -> None:
        """Fan-out an absolute counter value to every inner writer."""
        self._fanout(lambda w: w.write_counter_absolute(name, value, labels))

    def write_summary(self, name: str, value: float, labels: dict[str, str]) -> None:
        """Fan-out a summary observation to every inner writer."""
        self._fanout(lambda w: w.write_summary(name, value, labels))

    def replace_family(self, name: str, entries: list[tuple[float, dict[str, str]]]) -> None:
        """Forward to inner writers that implement ``replace_family``.

        Writers without the method are silently skipped (their per-call
        write_gauge stream is the ground truth for them).
        """
        for w in self._writers:
            replacer = getattr(w, "replace_family", None)
            if callable(replacer):
                replacer(name, entries)
