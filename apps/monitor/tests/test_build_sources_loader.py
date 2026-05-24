"""Tests for build_sources_loader.py (STAGE-003-009 Wave G)."""

from __future__ import annotations

import asyncio
import textwrap
from pathlib import Path

import pytest
import structlog

from homelab_monitor.kernel.docker.build_sources_loader import BuildSourcesLoader


def _log() -> structlog.stdlib.BoundLogger:
    return structlog.get_logger().bind(component="test")  # type: ignore[return-value]


def _valid_yaml() -> str:
    return textwrap.dedent("""\
        compose_files:
          - host_path: /a/x.yml
            container_path: /host/x.yml
        build_context_roots: []
    """)


def _loader(
    tmp_path: Path, *, filename: str = "build-sources.yaml", interval: float = 30.0
) -> tuple[BuildSourcesLoader, Path]:
    p = tmp_path / filename
    loader = BuildSourcesLoader(config_path=p, log=_log(), refresh_interval_seconds=interval)
    return loader, p


# ---------------------------------------------------------------------------
# Absent file — graceful no-op
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_absent_file_no_error(tmp_path: Path) -> None:
    """Missing config file: current_config=None, current_error=None — no raise."""
    loader, _ = _loader(tmp_path)
    await loader.refresh()
    assert loader.current_config is None
    assert loader.current_error is None


# ---------------------------------------------------------------------------
# Valid YAML
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_valid_yaml_populates_current_config(tmp_path: Path) -> None:
    """Valid YAML: current_config is set, current_error is None."""
    loader, p = _loader(tmp_path)
    p.write_text(_valid_yaml(), encoding="utf-8")
    await loader.refresh()
    assert loader.current_config is not None
    assert loader.current_error is None
    assert len(loader.current_config.compose_files) == 1
    assert loader.current_config.compose_files[0].container_path == "/host/x.yml"


# ---------------------------------------------------------------------------
# Error states
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_malformed_yaml_sets_error(tmp_path: Path) -> None:
    """Malformed YAML: refresh() doesn't raise; current_error.reason == 'malformed_yaml'."""
    loader, p = _loader(tmp_path)
    p.write_text("compose_files: {\nbroken: [unclosed\n", encoding="utf-8")
    await loader.refresh()
    assert loader.current_config is None
    assert loader.current_error is not None
    assert loader.current_error.reason == "malformed_yaml"


@pytest.mark.asyncio
async def test_invalid_schema_sets_error(tmp_path: Path) -> None:
    """Missing compose_files key: current_error.reason == 'invalid_schema'."""
    loader, p = _loader(tmp_path)
    p.write_text("build_context_roots: []\n", encoding="utf-8")
    await loader.refresh()
    assert loader.current_config is None
    assert loader.current_error is not None
    assert loader.current_error.reason == "invalid_schema"


# ---------------------------------------------------------------------------
# Transitions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_absent_then_present_transition(tmp_path: Path) -> None:
    """File absent then written: second refresh loads config."""
    loader, p = _loader(tmp_path)
    await loader.refresh()
    assert loader.current_config is None

    p.write_text(_valid_yaml(), encoding="utf-8")
    await loader.refresh()
    assert loader.current_config is not None
    assert loader.current_error is None


@pytest.mark.asyncio
async def test_present_then_absent_transition(tmp_path: Path) -> None:
    """File present then removed: next refresh clears config and error."""
    loader, p = _loader(tmp_path)
    p.write_text(_valid_yaml(), encoding="utf-8")
    await loader.refresh()
    assert loader.current_config is not None

    p.unlink()
    await loader.refresh()
    assert loader.current_config is None
    assert loader.current_error is None


@pytest.mark.asyncio
async def test_hot_reload_updates_config(tmp_path: Path) -> None:
    """Editing the YAML between refreshes updates current_config."""
    loader, p = _loader(tmp_path)
    p.write_text(_valid_yaml(), encoding="utf-8")
    await loader.refresh()
    assert loader.current_config is not None
    assert len(loader.current_config.compose_files) == 1

    # Add a second compose file entry
    updated = textwrap.dedent("""\
        compose_files:
          - host_path: /a/x.yml
            container_path: /host/x.yml
          - host_path: /a/y.yml
            container_path: /host/y.yml
        build_context_roots: []
    """)
    p.write_text(updated, encoding="utf-8")
    await loader.refresh()
    assert loader.current_config is not None
    assert len(loader.current_config.compose_files) == 2  # noqa: PLR2004 -- test-only literal


# ---------------------------------------------------------------------------
# start_task / stop_task lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_task_idempotent_async(tmp_path: Path) -> None:
    """Second start_task() call is a no-op — task object is the same."""
    loader, _ = _loader(tmp_path)
    loader.start_task()
    task1 = loader._task  # pyright: ignore[reportPrivateUsage]
    loader.start_task()
    task2 = loader._task  # pyright: ignore[reportPrivateUsage]
    assert task1 is task2
    await loader.stop_task()


@pytest.mark.asyncio
async def test_stop_task_when_never_started(tmp_path: Path) -> None:
    """stop_task() when never started does not raise."""
    loader, _ = _loader(tmp_path)
    await loader.stop_task()  # must not raise


@pytest.mark.asyncio
async def test_stop_task_clears_task_reference(tmp_path: Path) -> None:
    """After stop_task(), internal task reference is None."""
    loader, _ = _loader(tmp_path)
    loader.start_task()
    assert loader._task is not None  # pyright: ignore[reportPrivateUsage]
    await loader.stop_task()
    assert loader._task is None  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_refresh_loop_runs_multiple_times(tmp_path: Path) -> None:
    """Refresh loop fires repeatedly; after brief sleep current_config is set."""
    loader, p = _loader(tmp_path, interval=0.02)
    p.write_text(_valid_yaml(), encoding="utf-8")
    loader.start_task()
    await asyncio.sleep(0.1)
    await loader.stop_task()
    assert loader.current_config is not None


@pytest.mark.asyncio
async def test_task_is_running_after_start(tmp_path: Path) -> None:
    """Task is not done immediately after start_task()."""
    loader, _ = _loader(tmp_path, interval=60.0)
    loader.start_task()
    task = loader._task  # pyright: ignore[reportPrivateUsage]
    assert task is not None
    assert not task.done()
    await loader.stop_task()
