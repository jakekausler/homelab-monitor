"""Picklable narrow context + buffering metrics writer for PROCESS-mode collectors.

PROCESS-mode collectors execute inside a ``ProcessPoolExecutor`` worker. The
worker receives :class:`ProcessCollectorContext` (NOT :class:`CollectorContext`)
because the live context carries non-picklable handles (``httpx.AsyncClient``,
``BoundLogger``, the in-process ``MetricsWriter`` and ``SqliteRepository``).
The worker writes metrics into a :class:`BufferingMetricsWriter`; the parent
process replays the drained list through the real :class:`MetricsWriter` after
the future resolves.

This module is intentionally tiny + dependency-light — anything imported here
must be picklable.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from homelab_monitor.kernel.plugins.io import MetricEntry
from homelab_monitor.kernel.plugins.types import CollectorConfig
from homelab_monitor.kernel.secrets.resolver import SyncSecretsResolver


@dataclass(slots=True)
class BufferingMetricsWriter:
    """In-process buffering writer used inside PROCESS workers.

    Each ``write_*`` call appends a :class:`MetricEntry` to ``_entries``.
    :meth:`drain` returns the buffered list and clears the buffer in one
    atomic-from-caller's-POV step. Picklable: only carries a list of
    :class:`MetricEntry` (which is itself picklable).

    NOT thread-safe — workers are single-threaded inside the pool.
    """

    _entries: list[MetricEntry] = field(default_factory=lambda: [])

    def write_gauge(self, name: str, value: float, labels: dict[str, str]) -> None:
        """Buffer a gauge observation."""
        self._entries.append(MetricEntry(kind="gauge", name=name, value=value, labels=dict(labels)))

    def write_counter(self, name: str, value: float, labels: dict[str, str]) -> None:
        """Buffer a counter increment."""
        self._entries.append(
            MetricEntry(kind="counter", name=name, value=value, labels=dict(labels))
        )

    def write_summary(self, name: str, value: float, labels: dict[str, str]) -> None:
        """Buffer a summary observation."""
        self._entries.append(
            MetricEntry(kind="summary", name=name, value=value, labels=dict(labels))
        )

    def drain(self) -> list[MetricEntry]:
        """Return all buffered entries in insertion order, then clear the buffer."""
        out = list(self._entries)
        self._entries.clear()
        return out


# TODO(STAGE-001-009 or post): Introduce a separate ``ProcessCollector``
# Protocol whose ``run`` signature takes ``ProcessCollectorContext``,
# eliminating the ``# type: ignore[arg-type]`` PROCESS-mode plugin authors
# must write today (since the base ``Collector`` Protocol's ``run`` declares
# ``ctx: CollectorContext``). Deferred: not blocking; documented in
# ``scheduler.py:_process_runner`` and the wrapper class call site.


@dataclass(slots=True)
class ProcessCollectorContext:
    """Narrow, picklable context handed to PROCESS-mode collector workers.

    Only contains the three handles a process worker can actually use:

    - :attr:`config` — the validated :class:`CollectorConfig` (Pydantic model).
    - :attr:`secrets` — :class:`SyncSecretsResolver` (picklable via ``__reduce__``).
    - :attr:`metrics` — a fresh :class:`BufferingMetricsWriter`; parent drains
      it after the future resolves.

    PROCESS collectors that try to access ``db``, ``http``, ``ssh``, ``vl``,
    ``log``, or ``ha`` will fail because those fields don't exist here. This is
    by design: the contract for PROCESS run-kind is "pure CPU work, no
    cross-process I/O".
    """

    config: CollectorConfig
    secrets: SyncSecretsResolver
    metrics: BufferingMetricsWriter
