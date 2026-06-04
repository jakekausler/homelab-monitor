"""Streaming export formatters for log-line export (STAGE-004-020).

Pure formatting + byte-framing for the GET /api/logs/export endpoint. The route
opens a streaming VictoriaLogs query (yielding VlLogLine), maps each to the
converged LogLine, and pipes them through one of these framers. O(1) memory:
exactly one line is held in memory at a time.

Two formats:
  - "txt":  one human-readable line per log:
            "<timestamp> [<severity-or-unknown>] <service-or-empty>: <message>\\n"
  - "json": a streamed JSON array of LogLine objects (compact model_dump_json),
            comma-separated, framed by "[" ... "]". Empty result -> "[]".
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Literal

from homelab_monitor.kernel.logs.models import LogLine

_SEVERITY_PLACEHOLDER = "unknown"


def format_txt_line(line: LogLine) -> str:
    """Format one LogLine as a single human-readable export line (with trailing \\n).

    severity None -> "unknown"; service None -> "" (empty). The message is emitted
    verbatim (it is the operator's data); any embedded newlines remain — txt export
    is best-effort human-readable, not a re-parseable format.
    """
    severity = line.severity if line.severity is not None else _SEVERITY_PLACEHOLDER
    service = line.service if line.service is not None else ""
    return f"{line.timestamp} [{severity}] {service}: {line.message}\n"


async def _stream_txt(lines: AsyncIterator[LogLine]) -> AsyncIterator[bytes]:
    """Yield each LogLine as a UTF-8 txt line. Empty input -> no bytes (empty body)."""
    async for line in lines:
        yield format_txt_line(line).encode("utf-8")


async def _stream_json(lines: AsyncIterator[LogLine]) -> AsyncIterator[bytes]:
    """Yield a streamed JSON array of LogLine objects.

    Framing state machine: emit b"[" lazily before the first element, prefix each
    subsequent element with b",", and always close with b"]". Empty input still
    emits b"[]" (the opening "[" is written on first iteration OR at close).
    """
    first = True
    async for line in lines:
        if first:
            yield b"["
            first = False
        else:
            yield b","
        yield line.model_dump_json().encode("utf-8")
    if first:
        # No lines were emitted: open + close together.
        yield b"[]"
    else:
        yield b"]"


async def stream_export(
    lines: AsyncIterator[LogLine], fmt: Literal["txt", "json"]
) -> AsyncIterator[bytes]:
    """Dispatch to the txt or json framer. ``fmt`` is "txt" or "json".

    Any value other than "json" is treated as "txt" (the route validates ``fmt``
    to the {"txt","json"} set via a Literal query param before calling this, so the
    else-branch is the txt default).
    """
    if fmt == "json":
        async for chunk in _stream_json(lines):
            yield chunk
    else:
        async for chunk in _stream_txt(lines):
            yield chunk


__all__ = ["format_txt_line", "stream_export"]
