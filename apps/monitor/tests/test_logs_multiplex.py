"""Tests for ``MultiplexLogsWriter``."""

from __future__ import annotations

from homelab_monitor.kernel.logs.multiplex import MultiplexLogsWriter
from homelab_monitor.kernel.plugins.io import InMemoryLogsWriter


def test_multiplex_fans_out_ingest() -> None:
    """Every ingest goes to every inner writer."""
    a = InMemoryLogsWriter()
    b = InMemoryLogsWriter()
    mux = MultiplexLogsWriter([a, b])
    mux.ingest("svc.host", "hello")
    assert len(a.recorded) == 1
    assert len(b.recorded) == 1
    assert a.recorded[0].stream == "svc.host"
    assert a.recorded[0].line == "hello"
    assert b.recorded[0].stream == "svc.host"


def test_multiplex_forwards_explicit_ts() -> None:
    """Explicit timestamps are forwarded verbatim."""
    a = InMemoryLogsWriter()
    mux = MultiplexLogsWriter([a])
    mux.ingest("s", "x", ts="2026-05-08T00:00:00+00:00")
    assert a.recorded[0].ts == "2026-05-08T00:00:00+00:00"


def test_multiplex_preserves_registration_order() -> None:
    """Writers are visited in registration order on every fan-out."""
    seen: list[str] = []

    class _Recorder:
        def __init__(self, tag: str) -> None:
            self._tag = tag

        def ingest(  # noqa: PLR0913 -- mirrors LogsWriter protocol (service/source_type/client_ip)
            self,
            stream: str,
            line: str,
            ts: str | None = None,
            *,
            service: str | None = None,
            source_type: str | None = None,
            client_ip: str | None = None,
        ) -> None:
            del stream, line, ts, service, source_type, client_ip
            seen.append(self._tag)

    mux = MultiplexLogsWriter([_Recorder("first"), _Recorder("second")])
    mux.ingest("s", "x")
    mux.ingest("s", "x")
    assert seen == ["first", "second", "first", "second"]


def test_multiplex_with_zero_writers() -> None:
    """Empty writers list is a valid no-op."""
    mux = MultiplexLogsWriter([])
    mux.ingest("s", "x")  # must not raise


def test_multiplex_with_three_writers() -> None:
    """Fan-out to three inner writers."""
    a = InMemoryLogsWriter()
    b = InMemoryLogsWriter()
    c = InMemoryLogsWriter()
    mux = MultiplexLogsWriter([a, b, c])
    mux.ingest("s", "x")
    assert len(a.recorded) == 1
    assert len(b.recorded) == 1
    assert len(c.recorded) == 1
