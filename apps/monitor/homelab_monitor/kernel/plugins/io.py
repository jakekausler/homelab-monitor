"""IO Protocols for the collector layer + in-memory test doubles.

Defines :class:`MetricsWriter`, :class:`LogsWriter`, :class:`SshClientFactory`,
:class:`SshConnection`, and :class:`HomeAssistantClient` as Protocol stubs. Real
implementations land in later stages:

- ``InMemoryMetricsWriter`` / ``InMemoryLogsWriter``: real now (test doubles).
- ``VictoriaMetricsWriter``: STAGE-001-015.
- ``VictoriaLogsWriter``: STAGE-001-016.
- ``SshClientFactory`` real impl: STAGE-017 (SSH probes).
- ``HomeAssistantClient`` real impl: EPIC-005.
"""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from homelab_monitor.kernel.db.time import utc_now_iso


@dataclass(frozen=True, slots=True)
class MetricEntry:
    """A single recorded metric write — used by :class:`InMemoryMetricsWriter`.

    ``kind`` is one of ``"gauge"``, ``"counter"``, ``"summary"``. ``labels`` is
    a flat dict of label name -> value.
    """

    kind: str
    name: str
    value: float
    labels: dict[str, str] = field(default_factory=lambda: {})


@dataclass(frozen=True, slots=True)
class LogEntry:
    """A single recorded log write — used by :class:`InMemoryLogsWriter`."""

    stream: str
    line: str
    ts: str


@runtime_checkable
class MetricsWriter(Protocol):
    """Minimal symmetric metrics-write surface.

    Buffering / batching is the impl's concern (real ``VictoriaMetricsWriter`` in
    STAGE-001-015), not the contract's. All three methods take a single observation.
    """

    def write_gauge(self, name: str, value: float, labels: dict[str, str]) -> None:
        """Record a gauge observation."""
        ...

    def write_counter(self, name: str, value: float, labels: dict[str, str]) -> None:
        """Record a counter increment."""
        ...

    def write_summary(self, name: str, value: float, labels: dict[str, str]) -> None:
        """Record a summary observation."""
        ...


@runtime_checkable
class LogsWriter(Protocol):
    """Minimal log-ingest surface. ``ts`` defaults to "now" (UTC ISO)."""

    def ingest(self, stream: str, line: str, ts: str | None = None) -> None:
        """Record a single log line on ``stream``."""
        ...


@runtime_checkable
class SshConnection(Protocol):
    """SCAFFOLDING: methods land in EPIC-017 (SSH collectors).

    NOTE: while empty, ``isinstance(x, SshConnection)`` is a tautology
    (always True for any object). Do NOT rely on it for narrowing in
    production code until methods are added.
    """


@runtime_checkable
class SshClientFactory(Protocol):
    """Opens connections to known targets, yielding via async context manager."""

    def open(self, target_id: str) -> AbstractAsyncContextManager[SshConnection]:
        """Open a connection to ``target_id`` and yield an :class:`SshConnection`.

        SCAFFOLDING: real impl in EPIC-017 SSH probes.
        """
        ...


@runtime_checkable
class HomeAssistantClient(Protocol):
    """SCAFFOLDING: methods land in the HA-collector epic.

    NOTE: while empty, ``isinstance(x, HomeAssistantClient)`` is a tautology
    (always True for any object). Do NOT rely on it for narrowing in
    production code until methods are added.
    """


class InMemoryMetricsWriter:
    """In-memory test double for :class:`MetricsWriter`. Records every call."""

    def __init__(self) -> None:
        self._entries: list[MetricEntry] = []
        # SCAFFOLDING: These timestamp logs are a temporary workaround to track
        # tick events. VictoriaMetrics queries (STAGE-015) will replace this
        # O(n) scan pattern.
        self._ts_log: list[tuple[str, str]] = []  # (metric_name, ts)
        self._collector_ts: dict[str, str] = {}  # collector_name -> last tick ISO ts

    @property
    def recorded(self) -> list[MetricEntry]:
        """Return all entries written since construction (insertion order)."""
        return list(self._entries)

    def write_gauge(self, name: str, value: float, labels: dict[str, str]) -> None:
        """Record a gauge observation."""
        self._entries.append(MetricEntry(kind="gauge", name=name, value=value, labels=dict(labels)))

    def write_counter(self, name: str, value: float, labels: dict[str, str]) -> None:
        """Record a counter increment."""
        self._entries.append(
            MetricEntry(kind="counter", name=name, value=value, labels=dict(labels))
        )
        # Track timestamps for success/failure metrics
        if name in ("homelab_collector_run_success_total", "homelab_collector_run_failure_total"):
            now_iso = utc_now_iso()
            self._ts_log.append((name, now_iso))
            collector_name = labels.get("name")
            if collector_name is not None:
                self._collector_ts[collector_name] = now_iso

    def write_summary(self, name: str, value: float, labels: dict[str, str]) -> None:
        """Record a summary observation."""
        self._entries.append(
            MetricEntry(kind="summary", name=name, value=value, labels=dict(labels))
        )

    def last_tick_at(self) -> str | None:
        """ISO-8601 UTC timestamp of the most recent success/failure tick, or None."""
        if not self._ts_log:
            return None
        return self._ts_log[-1][1]

    def last_tick_at_for(self, collector: str) -> str | None:
        """ISO-8601 UTC timestamp of the most recent success/failure for the named collector."""
        return self._collector_ts.get(collector)

    def last_error_for(self, collector: str) -> str | None:
        """Reason of the most recent failure_total for the named collector, or None."""
        for entry in reversed(self._entries):
            if (
                entry.name == "homelab_collector_run_failure_total"
                and entry.labels.get("name") == collector
            ):
                return entry.labels.get("reason")
        return None

    def failures_in_window(self, seconds: int) -> int:
        """Count of failure_total emissions within the last `seconds` (UTC wall-clock).

        SCAFFOLDING: currently counts all failures regardless of `seconds`. Real
        time-windowing lands when VictoriaMetrics queries replace this in STAGE-015.
        """
        del seconds
        count = 0
        for entry in self._entries:
            if entry.name == "homelab_collector_run_failure_total":
                count += int(entry.value)
        return count


class InMemoryLogsWriter:
    """In-memory test double for :class:`LogsWriter`. Records every call."""

    def __init__(self) -> None:
        self._entries: list[LogEntry] = []

    @property
    def recorded(self) -> list[LogEntry]:
        """Return all entries written since construction (insertion order)."""
        return list(self._entries)

    def ingest(self, stream: str, line: str, ts: str | None = None) -> None:
        """Record a single log line on ``stream``; defaults ``ts`` to current UTC."""
        self._entries.append(LogEntry(stream=stream, line=line, ts=ts or utc_now_iso()))
