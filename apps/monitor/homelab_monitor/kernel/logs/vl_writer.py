"""VictoriaLogsWriter — pushes ingested log lines to VictoriaLogs over HTTP.

Bridges the sync :class:`LogsWriter` Protocol to async HTTP via a bounded
``asyncio.Queue`` + a single background worker task started by lifespan.
``ingest()`` is a non-blocking ``put_nowait``; on ``QueueFull`` it drops the
line, increments :attr:`dropped_count`, and logs a warning. The worker drains
up to ``batch_size`` events or waits up to ``batch_timeout_s`` then POSTs the
batch as JSONL to VL ``/insert/jsonline``.

HTTP failures are logged + counted via :attr:`error_count`; they never crash
the worker (best-effort: monitoring must not fail because logs ingest stalled).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import cast

import httpx
import structlog
from structlog.stdlib import BoundLogger

from homelab_monitor.kernel.db.time import utc_now_iso

_DEFAULT_QUEUE_SIZE = 10000
_DEFAULT_BATCH_SIZE = 100
_DEFAULT_BATCH_TIMEOUT_S = 1.0
_HTTP_TIMEOUT_S = 5.0
_HTTP_OK_MIN = 200
_HTTP_OK_MAX = 300


class VictoriaLogsWriter:
    """Implements :class:`LogsWriter` against a VictoriaLogs HTTP endpoint."""

    def __init__(
        self,
        *,
        vl_url: str,
        http_client: httpx.AsyncClient,
        queue_size: int = _DEFAULT_QUEUE_SIZE,
        batch_size: int = _DEFAULT_BATCH_SIZE,
        batch_timeout_s: float = _DEFAULT_BATCH_TIMEOUT_S,
    ) -> None:
        self._vl_url = vl_url.rstrip("/")
        self._http_client = http_client
        self._batch_size = batch_size
        self._batch_timeout_s = batch_timeout_s
        self._queue: asyncio.Queue[dict[str, str]] = asyncio.Queue(maxsize=queue_size)
        self._stop = asyncio.Event()
        self.dropped_count: int = 0
        self.error_count: int = 0
        self._log: BoundLogger = cast(
            BoundLogger,
            structlog.get_logger().bind(component="vl_writer"),
        )

    def ingest(
        self,
        stream: str,
        line: str,
        ts: str | None = None,
        *,
        service: str | None = None,
        source_type: str | None = None,
    ) -> None:
        """Enqueue a log line for async POST to VictoriaLogs.

        Sync API per the :class:`LogsWriter` Protocol. Non-blocking: on a full
        queue the line is dropped + :attr:`dropped_count` incremented.

        ``service`` / ``source_type``, when provided, are written as top-level
        queryable fields (they survive ``json.dumps`` in :meth:`_post_batch` and
        are NOT in ``_EXCLUDED_FIELDS``), enabling the logs query filter to scope
        to this stream. Omitted -> event carries only the three builtins.
        """
        event: dict[str, str] = {
            "_msg": line,
            "_stream_id": stream,
            "_time": ts or utc_now_iso(),
        }
        if service is not None:
            event["service"] = service
        if source_type is not None:
            event["source_type"] = source_type
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            self.dropped_count += 1
            # TODO: emit homelab_log_writer_dropped_total Prometheus counter.
            # Currently exposed only via instance attribute. Track in follow-up stage.
            self._log.warning(
                "vl_writer.queue_full",
                dropped_count=self.dropped_count,
                stream=stream,
            )

    async def run_flusher(self) -> None:
        """Background worker: batch-drain the queue and POST to VL.

        Loops until :meth:`aclose` sets the stop event AND the queue is empty.
        Each iteration collects up to ``batch_size`` events, waiting up to
        ``batch_timeout_s`` for the first event. HTTP errors are logged +
        counted but do not propagate.
        """
        while not (self._stop.is_set() and self._queue.empty()):
            try:
                batch: list[dict[str, str]] = []
                # Wait for first event, OR for stop signal.
                try:
                    first = await asyncio.wait_for(self._queue.get(), timeout=self._batch_timeout_s)
                    if first.get("_sentinel") == "":
                        continue
                    batch.append(first)
                except TimeoutError:
                    continue
                # Drain up to batch_size - 1 more without waiting.
                while len(batch) < self._batch_size:
                    try:
                        item = self._queue.get_nowait()
                        if item.get("_sentinel") == "":
                            continue
                        batch.append(item)
                    except asyncio.QueueEmpty:
                        break
                await self._post_batch(batch)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.error_count += 1
                self._log.warning("vl_writer.flusher_unexpected_error", error=str(exc))
                await asyncio.sleep(0.1)  # avoid tight loop if exceptions persist

    async def _post_batch(self, batch: list[dict[str, str]]) -> None:
        """POST a batch of log events to VL as NDJSON.

        Best-effort: on transport / non-2xx error, logs + increments
        :attr:`error_count` and returns.
        """
        if not batch:
            return
        body = "\n".join(json.dumps(e) for e in batch)
        try:
            resp = await self._http_client.post(
                f"{self._vl_url}/insert/jsonline",
                content=body,
                headers={"Content-Type": "application/x-ndjson"},
                timeout=_HTTP_TIMEOUT_S,
            )
        except (httpx.TimeoutException, httpx.RequestError) as exc:
            self.error_count += 1
            self._log.warning(
                "vl_writer.post_failed",
                error=str(exc),
                batch_size=len(batch),
                error_count=self.error_count,
            )
            return
        if not (_HTTP_OK_MIN <= resp.status_code < _HTTP_OK_MAX):
            self.error_count += 1
            self._log.warning(
                "vl_writer.post_status",
                status=resp.status_code,
                body=resp.text[:200],
                error_count=self.error_count,
            )

    async def aclose(self) -> None:
        """Signal the worker to drain and exit. Caller awaits the worker task."""
        self._stop.set()
        with contextlib.suppress(asyncio.QueueFull):
            self._queue.put_nowait({"_sentinel": ""})
