"""Tests for in-memory writers and IO Protocol stubs."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import cast

from homelab_monitor.kernel.plugins.io import (
    HomeAssistantClient,
    InMemoryLogsWriter,
    InMemoryMetricsWriter,
    LogsWriter,
    MetricsWriter,
    SshClientFactory,
    SshConnection,
)

# Test constants
GAUGE_VALUE = 0.5
SUMMARY_VALUE = 12.3


# --- InMemoryMetricsWriter ------------------------------------------------------------------


def test_metrics_writer_records_gauge() -> None:
    w = InMemoryMetricsWriter()
    w.write_gauge("cpu", GAUGE_VALUE, {"host": "alpha"})
    assert len(w.recorded) == 1
    e = w.recorded[0]
    assert e.kind == "gauge"
    assert e.name == "cpu"
    assert e.value == GAUGE_VALUE
    assert e.labels == {"host": "alpha"}


def test_metrics_writer_records_counter() -> None:
    w = InMemoryMetricsWriter()
    w.write_counter("requests", 1.0, {"route": "/x"})
    assert w.recorded[0].kind == "counter"


def test_metrics_writer_records_summary() -> None:
    w = InMemoryMetricsWriter()
    w.write_summary("latency", SUMMARY_VALUE, {"route": "/x"})
    assert w.recorded[0].kind == "summary"


def test_metrics_writer_recorded_returns_copy() -> None:
    """``recorded`` returns a fresh list each call so callers can't mutate internal state."""
    w = InMemoryMetricsWriter()
    w.write_gauge("a", 1.0, {})
    snapshot = w.recorded
    snapshot.clear()
    assert len(w.recorded) == 1


def test_metrics_writer_labels_are_copied() -> None:
    """The writer copies labels so caller-side mutation does not leak in."""
    w = InMemoryMetricsWriter()
    src = {"host": "alpha"}
    w.write_gauge("cpu", 1.0, src)
    src["host"] = "beta"
    assert w.recorded[0].labels == {"host": "alpha"}


def test_metrics_writer_satisfies_protocol() -> None:
    """InMemoryMetricsWriter is structurally a MetricsWriter."""
    w: MetricsWriter = InMemoryMetricsWriter()
    assert isinstance(w, MetricsWriter)


# --- InMemoryLogsWriter ---------------------------------------------------------------------


def test_logs_writer_records_line_with_explicit_ts() -> None:
    w = InMemoryLogsWriter()
    w.ingest("docker.sonarr", "started", ts="2026-05-05T00:00:00+00:00")
    assert len(w.recorded) == 1
    e = w.recorded[0]
    assert e.stream == "docker.sonarr"
    assert e.line == "started"
    assert e.ts == "2026-05-05T00:00:00+00:00"


def test_logs_writer_defaults_ts_to_utc_now() -> None:
    """When ``ts`` is omitted, the writer fills it with utc_now_iso() output."""
    w = InMemoryLogsWriter()
    w.ingest("docker.sonarr", "started")
    assert w.recorded[0].ts.endswith("+00:00")


def test_logs_writer_recorded_returns_copy() -> None:
    w = InMemoryLogsWriter()
    w.ingest("s", "l")
    snapshot = w.recorded
    snapshot.clear()
    assert len(w.recorded) == 1


def test_logs_writer_satisfies_protocol() -> None:
    w: LogsWriter = InMemoryLogsWriter()
    assert isinstance(w, LogsWriter)


# --- SSH stubs ------------------------------------------------------------------------------


class _FakeConn:
    """Mock SshConnection — empty stub Protocol means any class satisfies it."""


class _FakeFactory:
    """Mock SshClientFactory that yields a _FakeConn from an async context manager."""

    def open(self, target_id: str) -> AbstractAsyncContextManager[SshConnection]:
        del target_id
        return self._yield_conn()

    @asynccontextmanager
    async def _yield_conn(self) -> AsyncGenerator[SshConnection, None]:
        yield cast(SshConnection, _FakeConn())


def test_ssh_connection_protocol_accepts_empty_class() -> None:
    """Empty Protocol body means any object structurally satisfies it."""
    conn: SshConnection = _FakeConn()
    assert isinstance(conn, SshConnection)


def test_ssh_factory_protocol_accepts_open_method() -> None:
    """A class with ``open(target_id)`` satisfies SshClientFactory."""
    factory: SshClientFactory = _FakeFactory()
    assert isinstance(factory, SshClientFactory)


async def test_ssh_factory_open_context_manager_yields_conn() -> None:
    """The async context manager from ``open`` yields an SshConnection-shaped object."""
    factory: SshClientFactory = _FakeFactory()
    async with factory.open("host-1") as conn:
        assert isinstance(conn, SshConnection)


# --- HomeAssistantClient stub --------------------------------------------------------------


class _FakeHa:
    """Empty class — Protocol body is empty so this is structurally an HA client."""


def test_home_assistant_client_protocol_accepts_empty_class() -> None:
    ha: HomeAssistantClient = _FakeHa()
    assert isinstance(ha, HomeAssistantClient)
