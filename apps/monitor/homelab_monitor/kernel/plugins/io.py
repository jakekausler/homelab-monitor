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
    """Stub Protocol for an open SSH connection.

    SCAFFOLDING: methods (``run``, ``read_file``, ...) land in EPIC-017 SSH probes.
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
    """Stub Protocol for the Home Assistant REST/WS client.

    SCAFFOLDING: methods land in EPIC-005 (Home Assistant integration).
    Defined here only so :class:`CollectorContext` can type its ``ha`` field.
    """


class InMemoryMetricsWriter:
    """In-memory test double for :class:`MetricsWriter`. Records every call."""

    def __init__(self) -> None:
        self._entries: list[MetricEntry] = []

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

    def write_summary(self, name: str, value: float, labels: dict[str, str]) -> None:
        """Record a summary observation."""
        self._entries.append(
            MetricEntry(kind="summary", name=name, value=value, labels=dict(labels))
        )


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
