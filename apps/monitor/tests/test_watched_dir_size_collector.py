"""Tests for :class:`WatchedDirSizeCollector` (100% branch coverage)."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest
import structlog

from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.io import (
    InMemoryLogsWriter,
    MemoryRetainingMetricsWriter,
)
from homelab_monitor.plugins.collectors.builtin import watched_dir_size as mod
from homelab_monitor.plugins.collectors.builtin.watched_dir_size import (
    WatchedDirectory,
    WatchedDirSizeCollector,
    WatchedDirSizeCollectorConfig,
    container_name,
    container_path,
    walk_dir,
)


def _ctx(writer: MemoryRetainingMetricsWriter, config: object) -> CollectorContext:
    """Minimal CollectorContext for the watched-dir collector.

    `config` is any object exposing `watched_directories` (read via getattr in run()).
    """
    return CollectorContext(
        config=config,  # pyright: ignore[reportArgumentType]
        db=None,  # pyright: ignore[reportArgumentType]
        vm=writer,
        vl=InMemoryLogsWriter(),
        http=None,  # pyright: ignore[reportArgumentType]
        ssh=None,  # pyright: ignore[reportArgumentType]
        secrets=None,  # pyright: ignore[reportArgumentType]
        log=structlog.get_logger().bind(collector="watched_dir_size"),  # pyright: ignore[reportArgumentType]
        ha=None,
    )


def _gauges(writer: MemoryRetainingMetricsWriter, name: str) -> dict[str, float]:
    """Map of path-label -> value for gauge `name`."""
    return {e.labels["path"]: e.value for e in writer.snapshot() if e.name == name}


# --------------------------------------------------------------------------- #
# name derivation / path mapping
# --------------------------------------------------------------------------- #


def test_container_name_derivations() -> None:
    assert container_name("/var") == "var"
    assert container_name("/tmp") == "tmp"
    assert container_name("/var/log") == "var-log"


def test_container_name_rejects_root() -> None:
    with pytest.raises(ValueError):
        container_name("/")


def test_container_path_maps_under_host_watch() -> None:
    assert container_path("/var") == "/host-watch/var"
    assert container_path("/var/log") == "/host-watch/var-log"


# --------------------------------------------------------------------------- #
# _walk_dir branches (sync helper)
# --------------------------------------------------------------------------- #


def test_walk_dir_sums_file_bytes(tmp_path: Path) -> None:
    (tmp_path / "a").write_bytes(b"x" * 100)
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "b").write_bytes(b"y" * 50)

    result = walk_dir(str(tmp_path))

    assert result.total_bytes == 150  # noqa: PLR2004
    assert result.truncated is False
    assert result.unreadable_subdirs == 0


def test_walk_dir_missing_dir_returns_zero() -> None:
    result = walk_dir("/nonexistent/path/xyz")
    assert result.total_bytes == 0
    assert result.truncated is False
    assert result.unreadable_subdirs == 0


def test_walk_dir_empty_dir_returns_zero(tmp_path: Path) -> None:
    result = walk_dir(str(tmp_path))
    assert result.total_bytes == 0
    assert result.truncated is False


def test_walk_dir_truncates_on_deadline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A monotonic clock already past the deadline truncates on first iteration."""
    (tmp_path / "a").write_bytes(b"x" * 10)

    # First time.monotonic() call (deadline base) returns 0.0; subsequent calls
    # return a value past the budget so the in-loop deadline check trips.
    calls = iter([0.0, _walk_budget_plus()])

    def fake_monotonic() -> float:
        try:
            return next(calls)
        except StopIteration:
            return _walk_budget_plus()

    monkeypatch.setattr(mod.time, "monotonic", fake_monotonic)

    result = walk_dir(str(tmp_path))
    assert result.truncated is True


def _walk_budget_plus() -> float:
    return mod.WALK_BUDGET_S + 1000.0


def test_walk_dir_counts_unreadable_subdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """os.walk onerror handler increments unreadable_subdirs."""
    bad = tmp_path / "locked"
    bad.mkdir()

    def fake_walk(
        top: str,
        followlinks: bool = False,
        onerror: Callable[[OSError], None] | None = None,
    ) -> Iterator[tuple[str, list[str], list[str]]]:
        # Drive the onerror callback once, then yield the real top dir entry.
        if onerror is not None:
            onerror(PermissionError(13, "Permission denied", str(bad)))
        yield (str(tmp_path), [], [])

    monkeypatch.setattr(mod.os, "walk", fake_walk)

    result = walk_dir(str(tmp_path))
    assert result.unreadable_subdirs == 1


def test_walk_dir_prunes_cross_filesystem_subdir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A subdir on a different st_dev is pruned from the walk."""
    top = tmp_path
    same = top / "same"
    other = top / "other"
    same.mkdir()
    other.mkdir()
    (top / "rootfile").write_bytes(b"r" * 10)
    (same / "f").write_bytes(b"s" * 20)
    (other / "f").write_bytes(b"o" * 40)

    top_dev = os.stat(str(top)).st_dev
    real_lstat = os.lstat

    def fake_lstat(path: str) -> os.stat_result:
        st = real_lstat(path)
        if path == str(other):
            # Report a different device for `other` so it gets pruned.
            return os.stat_result(
                (
                    st.st_mode,
                    st.st_ino,
                    top_dev + 1,
                    st.st_nlink,
                    st.st_uid,
                    st.st_gid,
                    st.st_size,
                    st.st_atime,
                    st.st_mtime,
                    st.st_ctime,
                )
            )
        return st

    monkeypatch.setattr(mod.os, "lstat", fake_lstat)

    result = walk_dir(str(top))
    # rootfile(10) + same/f(20) = 30; other/f(40) pruned.
    assert result.total_bytes == 30  # noqa: PLR2004


def test_walk_dir_lstat_error_during_prune_counts_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An OSError from lstat during st_dev pruning increments unreadable_subdirs."""
    sub = tmp_path / "sub"
    sub.mkdir()

    real_lstat = os.lstat

    def fake_lstat(path: str) -> os.stat_result:
        if path == str(sub):
            raise PermissionError(13, "Permission denied", path)
        return real_lstat(path)

    monkeypatch.setattr(mod.os, "lstat", fake_lstat)

    result = walk_dir(str(tmp_path))
    assert result.unreadable_subdirs == 1


def test_walk_dir_skips_vanished_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An OSError from lstat on a file is skipped silently (no byte added)."""
    (tmp_path / "good").write_bytes(b"g" * 10)
    (tmp_path / "ghost").write_bytes(b"x" * 10)

    real_lstat = os.lstat

    def fake_lstat(path: str) -> os.stat_result:
        if path.endswith("ghost"):
            raise FileNotFoundError(2, "No such file", path)
        return real_lstat(path)

    monkeypatch.setattr(mod.os, "lstat", fake_lstat)

    result = walk_dir(str(tmp_path))
    assert result.total_bytes == 10  # noqa: PLR2004


# --------------------------------------------------------------------------- #
# run() integration
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_run_emits_three_gauges_per_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Normal walk emits bytes + truncated(0) + unreadable(0) per path."""
    var_dir = tmp_path / "var"
    var_dir.mkdir()
    (var_dir / "f").write_bytes(b"x" * 200)

    def fake_container_path(friendly: str) -> str:
        assert friendly == "/var"
        return str(var_dir)

    monkeypatch.setattr(mod, "container_path", fake_container_path)

    config = SimpleNamespace(
        watched_directories=[
            WatchedDirectory(path="/var", warn_bytes=10 * 1024**3, crit_bytes=25 * 1024**3)
        ]
    )
    writer = MemoryRetainingMetricsWriter()
    result = await WatchedDirSizeCollector().run(_ctx(writer, config))

    assert result.ok
    assert result.metrics_emitted == 3  # noqa: PLR2004
    assert _gauges(writer, "homelab_host_directory_bytes")["/var"] == 200.0  # noqa: PLR2004
    assert _gauges(writer, "homelab_host_directory_walk_truncated")["/var"] == 0.0
    assert _gauges(writer, "homelab_host_directory_unreadable_subdirs")["/var"] == 0.0


@pytest.mark.asyncio
async def test_run_uses_default_config_when_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ctx.config without watched_directories falls back to baked default (/tmp,/var)."""
    empty = tmp_path / "empty"
    empty.mkdir()

    def _fake_container_path(friendly: str) -> str:
        return str(empty)

    monkeypatch.setattr(mod, "container_path", _fake_container_path)

    config = SimpleNamespace()  # no watched_directories attr -> getattr default
    writer = MemoryRetainingMetricsWriter()
    result = await WatchedDirSizeCollector().run(_ctx(writer, config))

    assert result.ok
    paths = set(_gauges(writer, "homelab_host_directory_bytes"))
    assert paths == {"/tmp", "/var"}


@pytest.mark.asyncio
async def test_run_emits_truncated_gauge_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When a walk truncates, the truncated gauge is 1.0."""
    d = tmp_path / "d"
    d.mkdir()
    (d / "f").write_bytes(b"x" * 10)

    def _fake_container_path(friendly: str) -> str:
        return str(d)

    monkeypatch.setattr(mod, "container_path", _fake_container_path)

    calls = iter([0.0, mod.WALK_BUDGET_S + 1000.0])

    def fake_monotonic() -> float:
        try:
            return next(calls)
        except StopIteration:
            return mod.WALK_BUDGET_S + 2000.0

    monkeypatch.setattr(mod.time, "monotonic", fake_monotonic)

    config = SimpleNamespace(
        watched_directories=[WatchedDirectory(path="/d", warn_bytes=1, crit_bytes=2)]
    )
    writer = MemoryRetainingMetricsWriter()
    result = await WatchedDirSizeCollector().run(_ctx(writer, config))

    assert result.ok
    assert _gauges(writer, "homelab_host_directory_walk_truncated")["/d"] == 1.0


@pytest.mark.asyncio
async def test_run_records_error_for_root_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A '/' watched path is rejected by _container_path; recorded as an error."""
    config = SimpleNamespace(
        watched_directories=[WatchedDirectory(path="/", warn_bytes=1, crit_bytes=2)]
    )
    writer = MemoryRetainingMetricsWriter()
    result = await WatchedDirSizeCollector().run(_ctx(writer, config))

    assert result.ok is False
    assert result.metrics_emitted == 0
    assert any("may not be '/'" in e for e in result.errors)


def test_config_default_watched_directories() -> None:
    cfg = WatchedDirSizeCollectorConfig(name="watched_dir_size")
    paths = {d.path for d in cfg.watched_directories}
    assert paths == {"/tmp", "/var"}
    tmp = next(d for d in cfg.watched_directories if d.path == "/tmp")
    assert tmp.warn_bytes == 1 * 1024**3
    assert tmp.crit_bytes == 4 * 1024**3
