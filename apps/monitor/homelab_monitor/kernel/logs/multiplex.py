"""Multiplex LogsWriter that fans out every ingest to N inner writers.

Used in production to dual-write to:

1. ``InMemoryLogsWriter`` (so any in-process consumer / test snapshot keeps
   working without a round-trip to VictoriaLogs).
2. ``VictoriaLogsWriter`` (so VL becomes the long-horizon log store; the
   :class:`/api/logs/query` endpoint proxies LogsQL against it).

Order: writers are visited in registration order. Mirrors the symmetric design
of :class:`MultiplexMetricsWriter` (same module shape).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from homelab_monitor.kernel.plugins.io import LogsWriter


class MultiplexLogsWriter:
    """Fan-out :class:`LogsWriter` that delegates to N inner writers.

    Implements the :class:`LogsWriter` Protocol structurally. Every ``ingest``
    call is forwarded to each inner writer in registration order.
    """

    def __init__(self, writers: Sequence[LogsWriter]) -> None:
        self._writers: list[LogsWriter] = list(writers)

    def _fanout(self, op: Callable[[LogsWriter], None]) -> None:
        for w in self._writers:
            op(w)

    def ingest(
        self,
        stream: str,
        line: str,
        ts: str | None = None,
        *,
        service: str | None = None,
        source_type: str | None = None,
    ) -> None:
        """Fan-out a log ingest to every inner writer."""
        self._fanout(lambda w: w.ingest(stream, line, ts, service=service, source_type=source_type))
