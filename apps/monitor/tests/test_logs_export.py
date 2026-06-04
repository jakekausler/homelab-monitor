"""Unit tests for kernel/logs/export.py formatters (STAGE-004-020).

Pure formatter unit tests with no HTTP.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import pytest

from homelab_monitor.kernel.logs.export import format_txt_line, stream_export
from homelab_monitor.kernel.logs.models import LogLine


def _line(**over: object) -> LogLine:
    base: dict[str, object] = {
        "timestamp": "2026-05-07T00:00:00Z",
        "message": "hello",
        "stream": "s",
        "severity": "error",
        "host": "h",
        "service": "nginx",
        "fields": {},
    }
    base.update(over)
    return LogLine(**base)  # type: ignore[arg-type]


async def _agen(lines: list[LogLine]) -> AsyncIterator[LogLine]:
    for line in lines:
        yield line


async def _collect(it: AsyncIterator[bytes]) -> bytes:
    out = b""
    async for chunk in it:
        out += chunk
    return out


def test_format_txt_line_full() -> None:
    """Full txt line with all fields."""
    line = _line()
    assert format_txt_line(line) == "2026-05-07T00:00:00Z [error] nginx: hello\n"


def test_format_txt_line_none_severity() -> None:
    """severity=None → [unknown]."""
    line = _line(severity=None)
    assert format_txt_line(line) == "2026-05-07T00:00:00Z [unknown] nginx: hello\n"


def test_format_txt_line_none_service() -> None:
    """service=None → empty service field (space-colon)."""
    line = _line(service=None)
    assert format_txt_line(line) == "2026-05-07T00:00:00Z [error] : hello\n"


@pytest.mark.asyncio
async def test_stream_export_txt_empty() -> None:
    """Empty txt export → empty bytes."""
    result = await _collect(stream_export(_agen([]), "txt"))
    assert result == b""


@pytest.mark.asyncio
async def test_stream_export_txt_two() -> None:
    """Two-line txt export."""
    lines = [
        _line(timestamp="2026-05-07T00:00:00Z", message="first"),
        _line(timestamp="2026-05-07T00:00:01Z", message="second"),
    ]
    result = await _collect(stream_export(_agen(lines), "txt"))
    assert result == (
        b"2026-05-07T00:00:00Z [error] nginx: first\n2026-05-07T00:00:01Z [error] nginx: second\n"
    )


@pytest.mark.asyncio
async def test_stream_export_json_empty() -> None:
    """Empty json export → b"[]"."""
    result = await _collect(stream_export(_agen([]), "json"))
    assert result == b"[]"


@pytest.mark.asyncio
async def test_stream_export_json_one() -> None:
    """Single-line json export."""
    lines = [_line(message="single")]
    result = await _collect(stream_export(_agen(lines), "json"))
    parsed: list[dict[str, object]] = json.loads(result)
    assert isinstance(parsed, list)
    assert len(parsed) == 1
    assert parsed[0]["message"] == "single"


@pytest.mark.asyncio
async def test_stream_export_json_two() -> None:
    """Two-line json export (validates comma framing)."""
    lines = [
        _line(message="first"),
        _line(message="second"),
    ]
    result = await _collect(stream_export(_agen(lines), "json"))
    parsed: list[dict[str, object]] = json.loads(result)
    assert isinstance(parsed, list)
    assert len(parsed) == 2  # noqa: PLR2004
    assert parsed[0]["message"] == "first"
    assert parsed[1]["message"] == "second"


@pytest.mark.asyncio
async def test_stream_export_txt_format_hits_else_branch() -> None:
    """fmt='txt' → else branch → txt framer (covers the non-json default path)."""
    lines = [_line(message="test")]
    result = await _collect(stream_export(_agen(lines), "txt"))
    assert result == b"2026-05-07T00:00:00Z [error] nginx: test\n"
