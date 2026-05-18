"""Tests for kernel/metrics/host_boot_time.py (T3b — STAGE-002-010)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from homelab_monitor.kernel.metrics.host_boot_time import (
    read_host_btime,
    read_host_btime_dt,
)


def _write_stat(tmp_path: Path, content: str) -> Path:
    """Write a fake /proc/stat file and return its parent directory path."""
    stat_file = tmp_path / "stat"
    stat_file.write_text(content, encoding="utf-8")
    return tmp_path


def test_read_host_btime_parses_btime_line(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """read_host_btime() returns the epoch float from a valid stat file."""
    _write_stat(
        tmp_path,
        "cpu  1234 0 5678 0\nbtime 1700000000\nprocesses 999\n",
    )
    monkeypatch.setenv("HM_HOST_PROC_DIR", str(tmp_path))
    result = read_host_btime()
    assert result == 1700000000.0  # noqa: PLR2004


def test_read_host_btime_missing_file_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """read_host_btime() returns None when the stat file doesn't exist."""
    monkeypatch.setenv("HM_HOST_PROC_DIR", str(tmp_path / "no-such-dir"))
    assert read_host_btime() is None


def test_read_host_btime_no_btime_line_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """read_host_btime() returns None when the stat file has no btime line."""
    _write_stat(tmp_path, "cpu  1234 0 5678 0\nprocesses 999\n")
    monkeypatch.setenv("HM_HOST_PROC_DIR", str(tmp_path))
    assert read_host_btime() is None


def test_read_host_btime_malformed_value_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """read_host_btime() returns None when the btime value is non-numeric."""
    _write_stat(tmp_path, "btime xyz\n")
    monkeypatch.setenv("HM_HOST_PROC_DIR", str(tmp_path))
    assert read_host_btime() is None


def test_read_host_btime_dt_returns_tz_aware_utc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """read_host_btime_dt() returns a tz-aware UTC datetime."""
    _write_stat(tmp_path, "btime 1700000000\n")
    monkeypatch.setenv("HM_HOST_PROC_DIR", str(tmp_path))
    result = read_host_btime_dt()
    assert result is not None
    assert result.tzinfo is not None
    assert result == datetime.fromtimestamp(1700000000.0, tz=UTC)


def test_read_host_btime_dt_returns_none_when_file_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """read_host_btime_dt() returns None when stat file is missing."""
    monkeypatch.setenv("HM_HOST_PROC_DIR", str(tmp_path / "nope"))
    assert read_host_btime_dt() is None


def test_read_host_btime_btime_line_missing_value_skips_and_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Branch 41->38: 'btime' line with no numeric value causes inner-if to be
    False; loop continues past that line and eventually returns None (line 46)."""
    # "btime " has only one token after split(), so len(parts) < _BTIME_FIELDS.
    # A subsequent line keeps the loop going (covers the arc back to line 38).
    _write_stat(tmp_path, "btime \ncpu  0 0 0 0\n")
    monkeypatch.setenv("HM_HOST_PROC_DIR", str(tmp_path))
    assert read_host_btime() is None
