"""Tests for LocalBuildUpdateCollector (STAGE-003-009)."""

from __future__ import annotations

import importlib
import os
import textwrap
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import structlog

from homelab_monitor.kernel.db.repositories.docker_build_hashes_repository import (
    DockerBuildHashesRepository,
)
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.docker.build_sources_loader import BuildSourcesLoader
from homelab_monitor.kernel.docker.socket_client import DockerSocketClient
from homelab_monitor.kernel.docker.source_hash import (
    SourceHashLimits,
    SourceHashResult,
)
from homelab_monitor.kernel.metrics import local_build_update_collector as lbuc_module
from homelab_monitor.kernel.metrics.local_build_update_collector import (
    _DEFAULT_INTERVAL_SECONDS,  # pyright: ignore[reportPrivateUsage]
    LocalBuildUpdateCollector,
    _resolve_interval_seconds,  # pyright: ignore[reportPrivateUsage]
)
from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.io import InMemoryLogsWriter, MemoryRetainingMetricsWriter
from homelab_monitor.kernel.plugins.types import CollectorConfig


def _ctx(writer: MemoryRetainingMetricsWriter, repo: SqliteRepository) -> CollectorContext:
    return CollectorContext(
        config=CollectorConfig(name="local_build_update_checker"),
        db=repo,
        vm=writer,
        vl=InMemoryLogsWriter(),
        http=AsyncMock(),
        ssh=None,  # pyright: ignore[reportArgumentType]
        secrets=None,  # pyright: ignore[reportArgumentType]
        log=structlog.get_logger().bind(collector="local_build_update_checker"),  # pyright: ignore[reportArgumentType]
        ha=None,
    )


def _make_collector(
    repo: SqliteRepository,
    socket_client: AsyncMock,
    compose_dir: Path | None,
    tmp_path: Path | None = None,
) -> LocalBuildUpdateCollector:
    build_hashes_repo = DockerBuildHashesRepository(repo)
    return LocalBuildUpdateCollector(
        db=repo,
        socket_client=socket_client,
        build_hashes_repo=build_hashes_repo,
        compose_dir=compose_dir,
    )


def _write_compose(compose_dir: Path, content: str) -> Path:
    compose_dir.mkdir(parents=True, exist_ok=True)
    p = compose_dir / "docker-compose.yml"
    p.write_text(content, encoding="utf-8")
    return p


def _make_context_dir(base: Path, name: str, files: dict[str, str] | None = None) -> Path:
    ctx = base / name
    ctx.mkdir(parents=True, exist_ok=True)
    for fname, content in (files or {"Dockerfile": "FROM ubuntu\n"}).items():
        (ctx / fname).write_text(content, encoding="utf-8")
    return ctx


# ---------------------------------------------------------------------------
# Dependency checks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_returns_error_when_dependencies_unwired(repo: SqliteRepository) -> None:
    """run() returns ok=False when dependencies are None (no db/socket/repo)."""
    writer = MemoryRetainingMetricsWriter()
    ctx = _ctx(writer, repo)
    collector = LocalBuildUpdateCollector()
    result = await collector.run(ctx)
    assert result.ok is False
    assert "dependencies_unwired" in result.errors


# ---------------------------------------------------------------------------
# compose_dir unset → graceful skip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compose_dir_unset_emits_readable_zero_and_ok(
    repo: SqliteRepository,
) -> None:
    """compose_dir=None emits homelab_docker_compose_readable=0 and returns ok=True."""
    writer = MemoryRetainingMetricsWriter()
    ctx = _ctx(writer, repo)
    socket_client = AsyncMock(spec=DockerSocketClient)
    collector = _make_collector(repo, socket_client, compose_dir=None)

    result = await collector.run(ctx)

    assert result.ok is True
    assert writer.last_gauge("homelab_docker_compose_readable") == 0.0
    # No DB writes expected
    build_hashes_repo = DockerBuildHashesRepository(repo)
    rows = await build_hashes_repo.list_all()
    assert rows == []


# ---------------------------------------------------------------------------
# Compose file unreadable
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compose_file_missing_emits_readable_zero_and_error(
    repo: SqliteRepository,
    tmp_path: Path,
) -> None:
    """Missing compose file emits compose_readable=0, returns ok=False."""
    writer = MemoryRetainingMetricsWriter()
    ctx = _ctx(writer, repo)
    socket_client = AsyncMock(spec=DockerSocketClient)
    compose_dir = tmp_path / "compose"
    compose_dir.mkdir()
    # No docker-compose.yml written

    collector = _make_collector(repo, socket_client, compose_dir=compose_dir)
    result = await collector.run(ctx)

    assert result.ok is False
    assert writer.last_gauge("homelab_docker_compose_readable") == 0.0
    assert any("compose_read_failed" in e for e in result.errors)


@pytest.mark.asyncio
async def test_compose_file_malformed_emits_readable_zero_and_error(
    repo: SqliteRepository,
    tmp_path: Path,
) -> None:
    """Malformed compose file emits compose_readable=0, returns ok=False."""
    writer = MemoryRetainingMetricsWriter()
    ctx = _ctx(writer, repo)
    socket_client = AsyncMock(spec=DockerSocketClient)
    compose_dir = tmp_path / "compose"
    _write_compose(compose_dir, "services: {\nbroken: [unclosed\n")

    collector = _make_collector(repo, socket_client, compose_dir=compose_dir)
    result = await collector.run(ctx)

    assert result.ok is False
    assert writer.last_gauge("homelab_docker_compose_readable") == 0.0


# ---------------------------------------------------------------------------
# No build services in compose
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_build_services_returns_ok_no_upserts(
    repo: SqliteRepository,
    tmp_path: Path,
) -> None:
    """Compose with no build: services returns ok=True with no DB writes."""
    writer = MemoryRetainingMetricsWriter()
    ctx = _ctx(writer, repo)
    socket_client = AsyncMock(spec=DockerSocketClient)
    socket_client.list_containers.return_value = []

    compose_dir = tmp_path / "compose"
    _write_compose(compose_dir, "services:\n  db:\n    image: postgres:16\n")

    collector = _make_collector(repo, socket_client, compose_dir=compose_dir)
    result = await collector.run(ctx)

    assert result.ok is True
    assert writer.last_gauge("homelab_docker_compose_readable") == 1.0
    rows = await DockerBuildHashesRepository(repo).list_all()
    assert rows == []


# ---------------------------------------------------------------------------
# First-check baseline
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_first_check_baseline_emits_zero_and_stores_hash(
    repo: SqliteRepository,
    tmp_path: Path,
) -> None:
    """First ever check stores hash and emits homelab_image_update_available=0.

    (D-FIRST-CHECK-BASELINE).
    """
    writer = MemoryRetainingMetricsWriter()
    ctx = _ctx(writer, repo)

    ctx_dir = _make_context_dir(tmp_path, "myapp")
    compose_dir = tmp_path / "compose"
    _write_compose(
        compose_dir,
        (
            f"services:\n  myapp:\n    build: {ctx_dir}\n"
            f"    labels:\n      com.docker.compose.service: myapp\n"
        ),
    )

    socket_client = AsyncMock(spec=DockerSocketClient)
    socket_client.list_containers.return_value = [
        {
            "Names": ["/myapp"],
            "Labels": {"com.docker.compose.service": "myapp"},
            "ImageID": "sha256:imageA",
        }
    ]

    collector = _make_collector(repo, socket_client, compose_dir=compose_dir)
    result = await collector.run(ctx)

    assert result.ok is True
    assert writer.last_gauge("homelab_image_update_available") == 0.0

    rows = await DockerBuildHashesRepository(repo).list_all()
    assert len(rows) == 1
    assert rows[0].container_name == "myapp"
    assert rows[0].last_source_hash is not None
    assert rows[0].update_available is False
    assert rows[0].baseline_source_hash == rows[0].last_source_hash
    assert rows[0].baseline_image_id == "sha256:imageA"


# ---------------------------------------------------------------------------
# Hash matches prior → update_available=0
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hash_matches_prior_emits_zero(
    repo: SqliteRepository,
    tmp_path: Path,
) -> None:
    """Second run with unchanged context emits homelab_image_update_available=0."""
    writer = MemoryRetainingMetricsWriter()
    ctx = _ctx(writer, repo)

    ctx_dir = _make_context_dir(tmp_path, "myapp")
    compose_dir = tmp_path / "compose"
    _write_compose(
        compose_dir,
        f"services:\n  myapp:\n    build: {ctx_dir}\n",
    )

    socket_client = AsyncMock(spec=DockerSocketClient)
    socket_client.list_containers.return_value = [
        {
            "Names": ["/myapp"],
            "Labels": {"com.docker.compose.service": "myapp"},
            "ImageID": "sha256:imageA",
        }
    ]

    collector = _make_collector(repo, socket_client, compose_dir=compose_dir)
    # First run — baseline
    await collector.run(ctx)
    # Second run — no changes
    result = await collector.run(ctx)

    assert result.ok is True
    assert writer.last_gauge("homelab_image_update_available") == 0.0

    rows = await DockerBuildHashesRepository(repo).list_all()
    assert rows[0].update_available is False
    assert rows[0].baseline_source_hash == rows[0].last_source_hash


# ---------------------------------------------------------------------------
# Hash differs from prior → update_available=1
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hash_differs_from_prior_emits_one(
    repo: SqliteRepository,
    tmp_path: Path,
) -> None:
    """Changed context emits homelab_image_update_available=1, update_available=True.

    Baseline hash is preserved (not overwritten) across ticks when image_id is unchanged.
    """
    writer = MemoryRetainingMetricsWriter()
    ctx = _ctx(writer, repo)

    ctx_dir = _make_context_dir(tmp_path, "myapp")
    compose_dir = tmp_path / "compose"
    _write_compose(
        compose_dir,
        f"services:\n  myapp:\n    build: {ctx_dir}\n",
    )

    socket_client = AsyncMock(spec=DockerSocketClient)
    socket_client.list_containers.return_value = [
        {
            "Names": ["/myapp"],
            "Labels": {"com.docker.compose.service": "myapp"},
            "ImageID": "sha256:imageA",
        }
    ]

    collector = _make_collector(repo, socket_client, compose_dir=compose_dir)
    # First run — baseline
    await collector.run(ctx)
    rows_after_first = await DockerBuildHashesRepository(repo).list_all()
    baseline_hash = rows_after_first[0].last_source_hash

    # Mutate context
    (ctx_dir / "new_file.py").write_text("print('changed')", encoding="utf-8")
    # Second run — changed, same image_id
    result = await collector.run(ctx)

    assert result.ok is True
    assert writer.last_gauge("homelab_image_update_available") == 1.0

    rows = await DockerBuildHashesRepository(repo).list_all()
    assert rows[0].update_available is True
    assert rows[0].baseline_source_hash == baseline_hash
    assert rows[0].last_source_hash != baseline_hash

    # Third run — no further file changes, same image_id — update_available must persist
    result3 = await collector.run(ctx)
    assert result3.ok is True
    assert writer.last_gauge("homelab_image_update_available") == 1.0
    rows3 = await DockerBuildHashesRepository(repo).list_all()
    assert rows3[0].update_available is True
    assert rows3[0].baseline_source_hash == baseline_hash


# ---------------------------------------------------------------------------
# Context missing on disk
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_context_missing_on_disk_sets_error_reason(
    repo: SqliteRepository,
    tmp_path: Path,
) -> None:
    """build_context dir absent → check_error_reason='context_missing', update_available=False."""
    writer = MemoryRetainingMetricsWriter()
    ctx = _ctx(writer, repo)

    compose_dir = tmp_path / "compose"
    # Reference a context dir that doesn't exist
    _write_compose(
        compose_dir,
        "services:\n  myapp:\n    build: ./nonexistent\n",
    )

    socket_client = AsyncMock(spec=DockerSocketClient)
    socket_client.list_containers.return_value = [
        {
            "Names": ["/myapp"],
            "Labels": {"com.docker.compose.service": "myapp"},
            "ImageID": "sha256:imageA",
        }
    ]

    collector = _make_collector(repo, socket_client, compose_dir=compose_dir)
    result = await collector.run(ctx)

    assert result.ok is True
    rows = await DockerBuildHashesRepository(repo).list_all()
    assert len(rows) == 1
    assert rows[0].check_error_reason == "context_missing"
    assert rows[0].update_available is False


# ---------------------------------------------------------------------------
# Oversized context
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_oversized_context_emits_one_and_sets_error_reason(
    repo: SqliteRepository,
    tmp_path: Path,
) -> None:
    """Oversized build context.

    Sentinel hash, update_available=True, check_error_reason='context_too_large'.
    """
    writer = MemoryRetainingMetricsWriter()
    ctx = _ctx(writer, repo)

    ctx_dir = tmp_path / "ctx"
    ctx_dir.mkdir()
    # Write a file bigger than our tiny limit
    (ctx_dir / "big.bin").write_bytes(b"x" * 200)

    compose_dir = tmp_path / "compose"
    _write_compose(
        compose_dir,
        f"services:\n  myapp:\n    build: {ctx_dir}\n",
    )

    socket_client = AsyncMock(spec=DockerSocketClient)
    socket_client.list_containers.return_value = [
        {
            "Names": ["/myapp"],
            "Labels": {"com.docker.compose.service": "myapp"},
            "ImageID": "sha256:imageA",
        }
    ]

    tiny_limits = SourceHashLimits(max_file_bytes=10)
    build_hashes_repo = DockerBuildHashesRepository(repo)
    collector = LocalBuildUpdateCollector(
        db=repo,
        socket_client=socket_client,
        build_hashes_repo=build_hashes_repo,
        compose_dir=compose_dir,
        limits=tiny_limits,
    )
    result = await collector.run(ctx)

    assert result.ok is True
    assert writer.last_gauge("homelab_image_update_available") == 1.0

    rows = await DockerBuildHashesRepository(repo).list_all()
    assert len(rows) == 1
    assert rows[0].check_error_reason == "context_too_large"
    assert rows[0].update_available is True
    assert rows[0].last_source_hash is not None
    assert rows[0].last_source_hash.startswith("OVERSIZED:")


# ---------------------------------------------------------------------------
# Per-container isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_services_one_context_missing_other_ok(
    repo: SqliteRepository,
    tmp_path: Path,
) -> None:
    """Two build services: one with missing context, one OK — both processed independently."""
    writer = MemoryRetainingMetricsWriter()
    ctx = _ctx(writer, repo)

    ok_dir = _make_context_dir(tmp_path, "ok-app")
    compose_dir = tmp_path / "compose"
    _write_compose(
        compose_dir,
        f"services:\n  ok-app:\n    build: {ok_dir}\n  missing-app:\n    build: ./nonexistent\n",
    )

    socket_client = AsyncMock(spec=DockerSocketClient)
    socket_client.list_containers.return_value = [
        {
            "Names": ["/ok-app"],
            "Labels": {"com.docker.compose.service": "ok-app"},
            "ImageID": "sha256:imageA",
        },
        {
            "Names": ["/missing-app"],
            "Labels": {"com.docker.compose.service": "missing-app"},
            "ImageID": "sha256:imageB",
        },
    ]

    collector = _make_collector(repo, socket_client, compose_dir=compose_dir)
    result = await collector.run(ctx)

    assert result.ok is True
    rows = await DockerBuildHashesRepository(repo).list_all()
    row_map = {r.container_name: r for r in rows}

    assert "ok-app" in row_map
    assert row_map["ok-app"].check_error_reason is None

    assert "missing-app" in row_map
    assert row_map["missing-app"].check_error_reason == "context_missing"


# ---------------------------------------------------------------------------
# Reconcile delete
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconcile_deletes_stale_rows(
    repo: SqliteRepository,
    tmp_path: Path,
) -> None:
    """Container no longer in socket → row removed on next tick."""
    writer = MemoryRetainingMetricsWriter()
    ctx = _ctx(writer, repo)

    ctx_dir = _make_context_dir(tmp_path, "myapp")
    compose_dir = tmp_path / "compose"
    _write_compose(
        compose_dir,
        f"services:\n  myapp:\n    build: {ctx_dir}\n",
    )

    socket_client = AsyncMock(spec=DockerSocketClient)
    socket_client.list_containers.return_value = [
        {
            "Names": ["/myapp"],
            "Labels": {"com.docker.compose.service": "myapp"},
            "ImageID": "sha256:imageA",
        }
    ]

    collector = _make_collector(repo, socket_client, compose_dir=compose_dir)
    # First tick — row created
    await collector.run(ctx)
    assert len(await DockerBuildHashesRepository(repo).list_all()) == 1

    # Second tick — container gone from socket
    socket_client.list_containers.return_value = []
    await collector.run(ctx)

    rows = await DockerBuildHashesRepository(repo).list_all()
    assert rows == []


# ---------------------------------------------------------------------------
# Match by com.docker.compose.service label
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_match_by_compose_service_label(
    repo: SqliteRepository,
    tmp_path: Path,
) -> None:
    """Container with label 'com.docker.compose.service' matches the right service."""
    writer = MemoryRetainingMetricsWriter()
    ctx = _ctx(writer, repo)

    ctx_dir = _make_context_dir(tmp_path, "udo-viewer")
    compose_dir = tmp_path / "compose"
    _write_compose(
        compose_dir,
        f"services:\n  udo-viewer:\n    build: {ctx_dir}\n",
    )

    socket_client = AsyncMock(spec=DockerSocketClient)
    # Container has a different raw name but the label identifies the compose service
    socket_client.list_containers.return_value = [
        {
            "Names": ["/abc123def456_udo-viewer_1"],
            "Labels": {"com.docker.compose.service": "udo-viewer"},
            "ImageID": "sha256:imageA",
        }
    ]

    collector = _make_collector(repo, socket_client, compose_dir=compose_dir)
    result = await collector.run(ctx)

    assert result.ok is True
    rows = await DockerBuildHashesRepository(repo).list_all()
    assert len(rows) == 1
    assert rows[0].compose_service == "udo-viewer"


@pytest.mark.asyncio
async def test_image_only_service_ignored(
    repo: SqliteRepository,
    tmp_path: Path,
) -> None:
    """Container whose compose service has only image: (no build:) is ignored."""
    writer = MemoryRetainingMetricsWriter()
    ctx = _ctx(writer, repo)

    compose_dir = tmp_path / "compose"
    _write_compose(
        compose_dir,
        "services:\n  db:\n    image: postgres:16\n",
    )

    socket_client = AsyncMock(spec=DockerSocketClient)
    socket_client.list_containers.return_value = [
        {
            "Names": ["/db"],
            "Labels": {"com.docker.compose.service": "db"},
            "ImageID": "sha256:imageA",
        }
    ]

    collector = _make_collector(repo, socket_client, compose_dir=compose_dir)
    result = await collector.run(ctx)

    assert result.ok is True
    rows = await DockerBuildHashesRepository(repo).list_all()
    assert rows == []


# ---------------------------------------------------------------------------
# Interval resolution
# ---------------------------------------------------------------------------


def test_resolve_interval_seconds_returns_default_when_unset() -> None:
    """_resolve_interval_seconds() returns 1800 (30 min) when env unset."""
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("HOMELAB_MONITOR_LOCAL_BUILD_INTERVAL_SECONDS", None)
        assert _resolve_interval_seconds() == _DEFAULT_INTERVAL_SECONDS


def test_resolve_interval_seconds_reads_env_var() -> None:
    """_resolve_interval_seconds() reads HOMELAB_MONITOR_LOCAL_BUILD_INTERVAL_SECONDS."""
    with patch.dict(os.environ, {"HOMELAB_MONITOR_LOCAL_BUILD_INTERVAL_SECONDS": "60"}):
        importlib.reload(lbuc_module)
        result = lbuc_module._resolve_interval_seconds()  # pyright: ignore[reportPrivateUsage]
        assert result == 60  # noqa: PLR2004 -- test-only literal (60s dev interval)


def test_resolve_interval_seconds_uses_default_on_malformed() -> None:
    """_resolve_interval_seconds() returns default on non-numeric env."""
    with patch.dict(
        os.environ,
        {"HOMELAB_MONITOR_LOCAL_BUILD_INTERVAL_SECONDS": "not-a-number"},
    ):
        assert _resolve_interval_seconds() == _DEFAULT_INTERVAL_SECONDS


def test_resolve_interval_seconds_uses_default_on_zero() -> None:
    """_resolve_interval_seconds() returns default when env < 1."""
    with patch.dict(os.environ, {"HOMELAB_MONITOR_LOCAL_BUILD_INTERVAL_SECONDS": "0"}):
        assert _resolve_interval_seconds() == _DEFAULT_INTERVAL_SECONDS


# ---------------------------------------------------------------------------
# Self-metric
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_self_metric_emitted_on_ok(repo: SqliteRepository, tmp_path: Path) -> None:
    """homelab_collector_run_local_build_update_checker{phase=tick, result=ok}.

    Emitted on success.
    """
    writer = MemoryRetainingMetricsWriter()
    ctx = _ctx(writer, repo)
    socket_client = AsyncMock(spec=DockerSocketClient)
    socket_client.list_containers.return_value = []

    compose_dir = tmp_path / "compose"
    _write_compose(compose_dir, "services:\n  db:\n    image: postgres\n")

    collector = _make_collector(repo, socket_client, compose_dir=compose_dir)
    result = await collector.run(ctx)

    assert result.ok is True
    gauges = writer.gauges
    self_metrics = [g for g in gauges if g[0] == "homelab_collector_run_local_build_update_checker"]
    assert any(g[2].get("phase") == "tick" and g[2].get("result") == "ok" for g in self_metrics)


@pytest.mark.asyncio
async def test_self_metric_emitted_on_error_when_compose_dir_none(
    repo: SqliteRepository,
) -> None:
    """homelab_collector_run_local_build_update_checker{result=ok} emitted when compose_dir=None."""
    writer = MemoryRetainingMetricsWriter()
    ctx = _ctx(writer, repo)
    socket_client = AsyncMock(spec=DockerSocketClient)
    collector = _make_collector(repo, socket_client, compose_dir=None)
    result = await collector.run(ctx)

    assert result.ok is True
    gauges = writer.gauges
    self_metrics = [g for g in gauges if g[0] == "homelab_collector_run_local_build_update_checker"]
    assert len(self_metrics) > 0


# ---------------------------------------------------------------------------
# Edge cases: empty Names, image-only services
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_entry_with_empty_names_is_skipped(
    repo: SqliteRepository,
    tmp_path: Path,
) -> None:
    """Container entries with empty Names list are skipped; no DB rows written."""
    writer = MemoryRetainingMetricsWriter()
    ctx = _ctx(writer, repo)

    ctx_dir = _make_context_dir(tmp_path, "app")
    compose_dir = tmp_path / "compose"
    _write_compose(
        compose_dir,
        (
            f"services:\n  app:\n    build: {ctx_dir}\n"
            f"    labels:\n      com.docker.compose.service: app\n"
        ),
    )

    socket_client = AsyncMock(spec=DockerSocketClient)
    socket_client.list_containers.return_value = [
        {"Names": [], "Labels": {}, "ImageID": "sha256:imageA"},
    ]

    collector = _make_collector(repo, socket_client, compose_dir=compose_dir)
    result = await collector.run(ctx)

    assert result.ok is True
    rows = await DockerBuildHashesRepository(repo).list_all()
    assert rows == []


@pytest.mark.asyncio
async def test_image_only_service_container_is_skipped(
    repo: SqliteRepository,
    tmp_path: Path,
) -> None:
    """Container whose compose service has only image: (no build context) is skipped."""
    writer = MemoryRetainingMetricsWriter()
    ctx = _ctx(writer, repo)

    ctx_dir = _make_context_dir(tmp_path, "builder")
    compose_dir = tmp_path / "compose"
    _write_compose(
        compose_dir,
        (
            f"services:\n  builder:\n    build: {ctx_dir}\n"
            f"    labels:\n      com.docker.compose.service: builder\n"
            f"  app:\n    image: nginx:latest\n"
        ),
    )

    socket_client = AsyncMock(spec=DockerSocketClient)
    socket_client.list_containers.return_value = [
        {
            "Names": ["/app"],
            "Labels": {"com.docker.compose.service": "app"},
            "ImageID": "sha256:imageA",
        },
    ]

    collector = _make_collector(repo, socket_client, compose_dir=compose_dir)
    result = await collector.run(ctx)

    assert result.ok is True
    rows = await DockerBuildHashesRepository(repo).list_all()
    assert rows == []


# ---------------------------------------------------------------------------
# Regression: baseline-tracking across multiple ticks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_available_persists_across_multiple_ticks_when_image_unchanged(
    repo: SqliteRepository,
    tmp_path: Path,
) -> None:
    """update_available stays True across repeated ticks when image_id is unchanged.

    Regression guard: the old code reset update_available to False on tick 2
    because it compared last_source_hash against itself.
    """
    writer = MemoryRetainingMetricsWriter()
    ctx = _ctx(writer, repo)

    ctx_dir = _make_context_dir(tmp_path, "myapp")
    compose_dir = tmp_path / "compose"
    _write_compose(compose_dir, f"services:\n  myapp:\n    build: {ctx_dir}\n")

    socket_client = AsyncMock(spec=DockerSocketClient)
    socket_client.list_containers.return_value = [
        {
            "Names": ["/myapp"],
            "Labels": {"com.docker.compose.service": "myapp"},
            "ImageID": "sha256:img1",
        }
    ]

    collector = _make_collector(repo, socket_client, compose_dir=compose_dir)

    # Tick 1 — baseline established
    await collector.run(ctx)
    rows = await DockerBuildHashesRepository(repo).list_all()
    baseline_hash = rows[0].last_source_hash

    # Mutate build context
    (ctx_dir / "change.py").write_text("x = 1", encoding="utf-8")

    # Tick 2 — update detected
    await collector.run(ctx)
    rows = await DockerBuildHashesRepository(repo).list_all()
    assert rows[0].update_available is True
    assert rows[0].baseline_source_hash == baseline_hash
    assert rows[0].last_source_hash != baseline_hash

    # Tick 3 — no further file changes, same image_id; update must persist
    await collector.run(ctx)
    rows = await DockerBuildHashesRepository(repo).list_all()
    assert rows[0].update_available is True
    assert rows[0].baseline_source_hash == baseline_hash

    # Tick 4 — still no changes; still True
    await collector.run(ctx)
    rows = await DockerBuildHashesRepository(repo).list_all()
    assert rows[0].update_available is True


@pytest.mark.asyncio
async def test_image_rebuild_resets_baseline_and_clears_update_available(
    repo: SqliteRepository,
    tmp_path: Path,
) -> None:
    """When image_id changes (rebuild), baseline resets to current hash.

    update_available=False.
    """
    writer = MemoryRetainingMetricsWriter()
    ctx = _ctx(writer, repo)

    ctx_dir = _make_context_dir(tmp_path, "myapp")
    compose_dir = tmp_path / "compose"
    _write_compose(compose_dir, f"services:\n  myapp:\n    build: {ctx_dir}\n")

    socket_client = AsyncMock(spec=DockerSocketClient)
    socket_client.list_containers.return_value = [
        {
            "Names": ["/myapp"],
            "Labels": {"com.docker.compose.service": "myapp"},
            "ImageID": "sha256:imageA",
        }
    ]

    collector = _make_collector(repo, socket_client, compose_dir=compose_dir)

    # Tick 1 — baseline established with imageA
    await collector.run(ctx)

    # Mutate build context so update is detected on next tick
    (ctx_dir / "change.py").write_text("x = 1", encoding="utf-8")

    # Tick 2 — update detected, still imageA
    await collector.run(ctx)
    rows = await DockerBuildHashesRepository(repo).list_all()
    assert rows[0].update_available is True
    new_hash = rows[0].last_source_hash

    # Tick 3 — imageB (image was rebuilt with the changed context)
    socket_client.list_containers.return_value = [
        {
            "Names": ["/myapp"],
            "Labels": {"com.docker.compose.service": "myapp"},
            "ImageID": "sha256:imageB",
        }
    ]
    await collector.run(ctx)

    rows = await DockerBuildHashesRepository(repo).list_all()
    assert rows[0].update_available is False
    assert rows[0].baseline_source_hash == new_hash
    assert rows[0].baseline_image_id == "sha256:imageB"
    assert rows[0].last_source_hash == new_hash


@pytest.mark.asyncio
async def test_image_rebuild_with_unchanged_source_also_resets_baseline(
    repo: SqliteRepository,
    tmp_path: Path,
) -> None:
    """Image rebuild with same source hash resets baseline_image_id.

    update_available stays False.
    """
    writer = MemoryRetainingMetricsWriter()
    ctx = _ctx(writer, repo)

    ctx_dir = _make_context_dir(tmp_path, "myapp")
    compose_dir = tmp_path / "compose"
    _write_compose(compose_dir, f"services:\n  myapp:\n    build: {ctx_dir}\n")

    socket_client = AsyncMock(spec=DockerSocketClient)
    socket_client.list_containers.return_value = [
        {
            "Names": ["/myapp"],
            "Labels": {"com.docker.compose.service": "myapp"},
            "ImageID": "sha256:imageA",
        }
    ]

    collector = _make_collector(repo, socket_client, compose_dir=compose_dir)

    # Tick 1 — baseline established with imageA, source unchanged
    await collector.run(ctx)
    rows = await DockerBuildHashesRepository(repo).list_all()
    original_hash = rows[0].last_source_hash

    # Tick 2 — imageB rebuilt but source files are identical
    socket_client.list_containers.return_value = [
        {
            "Names": ["/myapp"],
            "Labels": {"com.docker.compose.service": "myapp"},
            "ImageID": "sha256:imageB",
        }
    ]
    await collector.run(ctx)

    rows = await DockerBuildHashesRepository(repo).list_all()
    assert rows[0].update_available is False
    assert rows[0].baseline_source_hash == original_hash
    assert rows[0].baseline_image_id == "sha256:imageB"
    assert rows[0].last_source_hash == original_hash


def _build_sources_yaml(compose_path: Path) -> str:
    """Return a minimal build-sources YAML pointing at *compose_path*."""
    return textwrap.dedent(f"""\
        compose_files:
          - host_path: {compose_path}
            container_path: {compose_path}
        build_context_roots: []
    """)


async def _make_loader(yaml_path: Path) -> BuildSourcesLoader:
    """Construct and refresh a BuildSourcesLoader from *yaml_path*."""
    loader = BuildSourcesLoader(
        config_path=yaml_path,
        log=structlog.get_logger().bind(component="test"),  # type: ignore[arg-type]
    )
    await loader.refresh()
    return loader


@pytest.mark.asyncio
async def test_oversized_context_preserves_baseline_from_prior_row(
    repo: SqliteRepository,
    tmp_path: Path,
) -> None:
    """Oversized context preserves baseline_source_hash and baseline_image_id from prior row."""
    writer = MemoryRetainingMetricsWriter()
    ctx = _ctx(writer, repo)

    ctx_dir = _make_context_dir(tmp_path, "myapp")
    compose_dir = tmp_path / "compose"
    _write_compose(compose_dir, f"services:\n  myapp:\n    build: {ctx_dir}\n")

    socket_client = AsyncMock(spec=DockerSocketClient)
    socket_client.list_containers.return_value = [
        {
            "Names": ["/myapp"],
            "Labels": {"com.docker.compose.service": "myapp"},
            "ImageID": "sha256:imageA",
        }
    ]

    # Tick 1 — normal, establishes baseline
    collector = _make_collector(repo, socket_client, compose_dir=compose_dir)
    await collector.run(ctx)
    rows = await DockerBuildHashesRepository(repo).list_all()
    baseline_hash = rows[0].last_source_hash

    # Now write a huge file to make context oversized
    (ctx_dir / "big.bin").write_bytes(b"x" * 200)

    tiny_limits = SourceHashLimits(max_file_bytes=10)
    build_hashes_repo = DockerBuildHashesRepository(repo)
    oversized_collector = LocalBuildUpdateCollector(
        db=repo,
        socket_client=socket_client,
        build_hashes_repo=build_hashes_repo,
        compose_dir=compose_dir,
        limits=tiny_limits,
    )

    # Tick 2 — oversized: prior baseline must survive
    await oversized_collector.run(ctx)

    rows = await DockerBuildHashesRepository(repo).list_all()
    assert rows[0].check_error_reason == "context_too_large"
    assert rows[0].update_available is True
    assert rows[0].last_source_hash is not None
    assert rows[0].last_source_hash.startswith("OVERSIZED:")
    assert rows[0].baseline_source_hash == baseline_hash
    assert rows[0].baseline_image_id == "sha256:imageA"


# ---------------------------------------------------------------------------
# BuildSourcesLoader integration — STAGE-003-009 Wave G
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loader_yaml_config_used_in_preference_to_env_var(
    repo: SqliteRepository,
    tmp_path: Path,
) -> None:
    """YAML config via loader takes precedence over compose_dir env-var path.

    Only the YAML-declared services appear in DB rows; env-var services are
    ignored. homelab_build_sources_config_loaded == 1.0.
    """
    writer = MemoryRetainingMetricsWriter()
    ctx = _ctx(writer, repo)

    env_dir = tmp_path / "env-compose"
    yaml_dir = tmp_path / "yaml-compose"
    ctx_dir = _make_context_dir(tmp_path, "yaml-app")

    _write_compose(env_dir, "services:\n  env-app:\n    image: nginx\n")
    _write_compose(yaml_dir, f"services:\n  yaml-app:\n    build: {ctx_dir}\n")

    yaml_path = tmp_path / "build-sources.yaml"
    yaml_path.write_text(_build_sources_yaml(yaml_dir / "docker-compose.yml"), encoding="utf-8")

    loader = await _make_loader(yaml_path)

    socket_client = AsyncMock(spec=DockerSocketClient)
    socket_client.list_containers.return_value = [
        {
            "Names": ["/yaml-app"],
            "Labels": {"com.docker.compose.service": "yaml-app"},
            "ImageID": "sha256:img",
        }
    ]
    collector = LocalBuildUpdateCollector(
        db=repo,
        socket_client=socket_client,
        build_hashes_repo=DockerBuildHashesRepository(repo),
        compose_dir=env_dir,  # ignored because loader.current_config is set
        build_sources_loader=loader,
    )
    result = await collector.run(ctx)

    assert result.ok is True
    rows = await DockerBuildHashesRepository(repo).list_all()
    assert {r.container_name for r in rows} == {"yaml-app"}
    assert writer.last_gauge("homelab_build_sources_config_loaded") == 1.0


@pytest.mark.asyncio
async def test_loader_absent_falls_back_to_env_var(
    repo: SqliteRepository,
    tmp_path: Path,
) -> None:
    """When loader.current_config is None (file absent), env-var compose_dir is used.

    homelab_build_sources_config_loaded == 0.0.
    """
    writer = MemoryRetainingMetricsWriter()
    ctx = _ctx(writer, repo)

    ctx_dir = _make_context_dir(tmp_path, "env-app")
    compose_dir = tmp_path / "compose"
    _write_compose(compose_dir, f"services:\n  env-app:\n    build: {ctx_dir}\n")

    # Loader with missing file → current_config=None, current_error=None
    missing_yaml = tmp_path / "nonexistent.yaml"
    loader = await _make_loader(missing_yaml)
    assert loader.current_config is None
    assert loader.current_error is None

    socket_client = AsyncMock(spec=DockerSocketClient)
    socket_client.list_containers.return_value = [
        {
            "Names": ["/env-app"],
            "Labels": {"com.docker.compose.service": "env-app"},
            "ImageID": "sha256:img",
        }
    ]
    collector = LocalBuildUpdateCollector(
        db=repo,
        socket_client=socket_client,
        build_hashes_repo=DockerBuildHashesRepository(repo),
        compose_dir=compose_dir,
        build_sources_loader=loader,
    )
    result = await collector.run(ctx)

    assert result.ok is True
    rows = await DockerBuildHashesRepository(repo).list_all()
    assert {r.container_name for r in rows} == {"env-app"}
    assert writer.last_gauge("homelab_build_sources_config_loaded") == 0.0


@pytest.mark.asyncio
async def test_loader_invalid_yaml_returns_error(
    repo: SqliteRepository,
    tmp_path: Path,
) -> None:
    """Loader with current_error set → collector returns ok=False.

    errors contains 'build_sources_config_invalid:<reason>', metric = 0.0.
    """
    writer = MemoryRetainingMetricsWriter()
    ctx = _ctx(writer, repo)

    bad_yaml = tmp_path / "build-sources.yaml"
    bad_yaml.write_text("compose_files: {\nbroken: [unclosed\n", encoding="utf-8")
    loader = await _make_loader(bad_yaml)
    assert loader.current_error is not None
    assert loader.current_error.reason == "malformed_yaml"

    socket_client = AsyncMock(spec=DockerSocketClient)
    collector = LocalBuildUpdateCollector(
        db=repo,
        socket_client=socket_client,
        build_hashes_repo=DockerBuildHashesRepository(repo),
        build_sources_loader=loader,
    )
    result = await collector.run(ctx)

    assert result.ok is False
    assert any("build_sources_config_invalid" in e for e in result.errors)
    assert writer.last_gauge("homelab_build_sources_config_loaded") == 0.0


@pytest.mark.asyncio
async def test_loader_remaps_build_context(
    repo: SqliteRepository,
    tmp_path: Path,
) -> None:
    """Remap rule in YAML config rewrites build_context; source hash succeeds."""
    writer = MemoryRetainingMetricsWriter()
    ctx = _ctx(writer, repo)

    # Real context dir that will be found after remap
    _ctx_dir = _make_context_dir(tmp_path, "myapp")
    compose_dir = tmp_path / "compose"
    # Compose declares a "host" path that doesn't actually exist on disk,
    # but the remap will redirect it to ctx_dir.
    fake_host_prefix = "/fake-host-prefix"
    fake_ctx_path = f"{fake_host_prefix}/myapp"
    _write_compose(compose_dir, f"services:\n  myapp:\n    build: {fake_ctx_path}\n")

    yaml_path = tmp_path / "build-sources.yaml"
    yaml_path.write_text(
        textwrap.dedent(f"""\
            compose_files:
              - host_path: {compose_dir / "docker-compose.yml"}
                container_path: {compose_dir / "docker-compose.yml"}
            build_context_roots:
              - host_prefix: {fake_host_prefix}
                container_prefix: {tmp_path}
        """),
        encoding="utf-8",
    )
    loader = await _make_loader(yaml_path)
    assert loader.current_config is not None

    socket_client = AsyncMock(spec=DockerSocketClient)
    socket_client.list_containers.return_value = [
        {
            "Names": ["/myapp"],
            "Labels": {"com.docker.compose.service": "myapp"},
            "ImageID": "sha256:img",
        }
    ]
    collector = LocalBuildUpdateCollector(
        db=repo,
        socket_client=socket_client,
        build_hashes_repo=DockerBuildHashesRepository(repo),
        build_sources_loader=loader,
    )
    result = await collector.run(ctx)

    assert result.ok is True
    rows = await DockerBuildHashesRepository(repo).list_all()
    assert len(rows) == 1
    assert rows[0].check_error_reason is None  # context was found after remap


@pytest.mark.asyncio
async def test_metric_config_loaded_is_one_in_yaml_mode(
    repo: SqliteRepository,
    tmp_path: Path,
) -> None:
    """homelab_build_sources_config_loaded == 1.0 when YAML loader is active."""
    writer = MemoryRetainingMetricsWriter()
    ctx = _ctx(writer, repo)

    compose_dir = tmp_path / "compose"
    _write_compose(compose_dir, "services:\n  db:\n    image: postgres\n")
    yaml_path = tmp_path / "build-sources.yaml"
    yaml_path.write_text(_build_sources_yaml(compose_dir / "docker-compose.yml"), encoding="utf-8")
    loader = await _make_loader(yaml_path)

    socket_client = AsyncMock(spec=DockerSocketClient)
    socket_client.list_containers.return_value = []
    collector = LocalBuildUpdateCollector(
        db=repo,
        socket_client=socket_client,
        build_hashes_repo=DockerBuildHashesRepository(repo),
        build_sources_loader=loader,
    )
    await collector.run(ctx)
    assert writer.last_gauge("homelab_build_sources_config_loaded") == 1.0


@pytest.mark.asyncio
async def test_metric_config_loaded_is_zero_in_env_var_mode(
    repo: SqliteRepository,
    tmp_path: Path,
) -> None:
    """homelab_build_sources_config_loaded == 0.0 when env-var fallback is used."""
    writer = MemoryRetainingMetricsWriter()
    ctx = _ctx(writer, repo)

    compose_dir = tmp_path / "compose"
    _write_compose(compose_dir, "services:\n  db:\n    image: postgres\n")

    socket_client = AsyncMock(spec=DockerSocketClient)
    socket_client.list_containers.return_value = []
    # No build_sources_loader → env-var fallback
    collector = _make_collector(repo, socket_client, compose_dir=compose_dir)
    await collector.run(ctx)
    assert writer.last_gauge("homelab_build_sources_config_loaded") == 0.0


@pytest.mark.asyncio
async def test_empty_image_id_resets_baseline_like_first_check(
    repo: SqliteRepository, tmp_path: Path
) -> None:
    """Container entry with empty/missing ImageID is treated as first-check (baseline reset)."""
    writer = MemoryRetainingMetricsWriter()
    ctx = _ctx(writer, repo)

    # Pre-seed a row with a baseline + image_id
    async with repo.transaction() as conn:
        await DockerBuildHashesRepository.upsert_conn(
            conn,
            container_name="myapp",
            compose_service="myapp",
            build_context_path="/tmp/test",
            last_source_hash="hash-v1",
            last_checked_at="2026-01-01T00:00:00+00:00",
            check_failed_at=None,
            check_error_reason=None,
            update_available=False,
            baseline_source_hash="hash-v1",
            baseline_image_id="sha256:old-image-id",
        )

    # Setup compose and build context
    compose_dir = tmp_path / "compose"
    build_ctx = tmp_path / "build"
    build_ctx.mkdir()
    (build_ctx / "Dockerfile").write_text("FROM alpine\nRUN echo test")

    _write_compose(
        compose_dir,
        textwrap.dedent(f"""
            services:
              myapp:
                build: {build_ctx}
                image: myapp:local
        """),
    )

    # Mock socket client to return empty ImageID
    socket_client = AsyncMock(spec=DockerSocketClient)
    socket_client.list_containers.return_value = [
        {
            "Id": "cont-id-1",
            "Names": ["/myapp"],
            "Config": {"Labels": {}},
            "ImageID": "",  # Empty image ID
        }
    ]

    loader = BuildSourcesLoader(
        config_path=compose_dir / "build-sources.yaml",
        log=structlog.get_logger().bind(component="test"),  # type: ignore[arg-type]
    )

    collector = LocalBuildUpdateCollector(
        db=repo,
        socket_client=socket_client,
        build_hashes_repo=DockerBuildHashesRepository(repo),
        compose_dir=compose_dir,
        build_sources_loader=loader,
    )
    await collector.run(ctx)

    # Assert: baseline_source_hash = current hash, update_available=False
    row = await DockerBuildHashesRepository(repo).get_by_container("myapp")
    assert row is not None
    assert row.baseline_image_id == ""  # Empty string, same as current_image_id
    assert row.update_available is False  # No update detected


@pytest.mark.asyncio
async def test_preserve_check_failed_at_when_failure_reason_unchanged(
    repo: SqliteRepository, tmp_path: Path
) -> None:
    """check_failed_at is preserved if the failure reason is the same (not reset on each tick)."""
    writer = MemoryRetainingMetricsWriter()
    ctx = _ctx(writer, repo)

    # Pre-seed a row with a failure and a fixed check_failed_at timestamp
    original_failed_at = "2026-01-01T00:00:00+00:00"
    async with repo.transaction() as conn:
        await DockerBuildHashesRepository.upsert_conn(
            conn,
            container_name="myapp",
            compose_service="myapp",
            build_context_path="/tmp/missing",
            last_source_hash=None,
            last_checked_at="2026-01-02T00:00:00+00:00",
            check_failed_at=original_failed_at,
            check_error_reason="context_missing",
            update_available=False,
            baseline_source_hash=None,
            baseline_image_id=None,
        )

    # Setup compose with missing build context (same failure condition)
    compose_dir = tmp_path / "compose"
    _write_compose(
        compose_dir,
        textwrap.dedent("""
            services:
              myapp:
                build: /nonexistent-path
        """),
    )

    socket_client = AsyncMock(spec=DockerSocketClient)
    socket_client.list_containers.return_value = [
        {
            "Id": "cont-id-1",
            "Names": ["/myapp"],
            "Config": {"Labels": {}},
            "ImageID": "sha256:image-id",
        }
    ]

    loader = BuildSourcesLoader(
        config_path=compose_dir / "build-sources.yaml",
        log=structlog.get_logger().bind(component="test"),  # type: ignore[arg-type]
    )

    collector = LocalBuildUpdateCollector(
        db=repo,
        socket_client=socket_client,
        build_hashes_repo=DockerBuildHashesRepository(repo),
        compose_dir=compose_dir,
        build_sources_loader=loader,
    )
    # Run on a later timestamp (now > original_failed_at)
    await collector.run(ctx)

    # Assert: check_failed_at is preserved, not updated to now
    row = await DockerBuildHashesRepository(repo).get_by_container("myapp")
    assert row is not None
    assert row.check_error_reason == "context_missing"
    assert row.check_failed_at == original_failed_at  # Preserved, not updated


@pytest.mark.asyncio
async def test_refresh_container_resets_baseline_and_clears_update_available(
    repo: SqliteRepository, tmp_path: Path
) -> None:
    """T1: refresh_container resets baseline and clears update_available."""
    # Pre-seed a row with update_available=True and an old baseline
    async with repo.transaction() as conn:
        await DockerBuildHashesRepository.upsert_conn(
            conn,
            container_name="myapp",
            compose_service="myapp",
            build_context_path="/app/build",
            last_source_hash="old_hash",
            last_checked_at="2026-01-01T00:00:00+00:00",
            check_failed_at=None,
            check_error_reason=None,
            update_available=True,
            baseline_source_hash="old_baseline",
            baseline_image_id="old_image_id",
        )

    # Setup compose
    compose_dir = tmp_path / "compose"
    build_context = tmp_path / "app" / "build"
    build_context.mkdir(parents=True, exist_ok=True)
    _write_compose(
        compose_dir,
        textwrap.dedent("""
            services:
              myapp:
                build: ../app/build
        """),
    )

    # Mock socket client to return container with known image ID
    socket_client = AsyncMock(spec=DockerSocketClient)
    socket_client.list_containers.return_value = [
        {
            "Id": "cont-id-1",
            "Names": ["/myapp"],
            "Config": {"Labels": {"com.docker.compose.service": "myapp"}},
            "ImageID": "sha256:new_image_id",
        }
    ]

    # Mock compute_source_hash to return a new hash
    with patch(
        "homelab_monitor.kernel.metrics.local_build_update_collector.compute_source_hash"
    ) as mock_compute:
        mock_compute.return_value = SourceHashResult(
            hash="new_hash",
            files_hashed=10,
            bytes_hashed=50000,
            files_skipped=0,
            exceeded=None,
        )

        loader = BuildSourcesLoader(
            config_path=compose_dir / "build-sources.yaml",
            log=structlog.get_logger().bind(component="test"),  # type: ignore[arg-type]
        )

        collector = LocalBuildUpdateCollector(
            db=repo,
            socket_client=socket_client,
            build_hashes_repo=DockerBuildHashesRepository(repo),
            compose_dir=compose_dir,
            build_sources_loader=loader,
        )

        # Call refresh_container
        await collector.refresh_container(container_name="myapp")

    # Assert: upserted row has update_available=False, new baseline
    row = await DockerBuildHashesRepository(repo).get_by_container("myapp")
    assert row is not None
    assert row.update_available is False
    assert row.baseline_source_hash == "new_hash"
    assert row.baseline_image_id == "sha256:new_image_id"
    assert row.check_failed_at is None
    assert row.check_error_reason is None


@pytest.mark.asyncio
async def test_refresh_container_noop_when_dependencies_unwired(
    tmp_path: Path,
) -> None:
    """T2: refresh_container no-ops when dependencies unwired."""
    collector = LocalBuildUpdateCollector(
        db=None,  # Unwired
        socket_client=None,
        build_hashes_repo=None,
        compose_dir=tmp_path,
    )

    # Must not raise, must not call any DB method
    await collector.refresh_container(container_name="test")


@pytest.mark.asyncio
async def test_refresh_container_noop_when_container_not_in_list(
    repo: SqliteRepository, tmp_path: Path
) -> None:
    """T3: refresh_container no-ops when container not in list."""
    compose_dir = tmp_path / "compose"
    _write_compose(
        compose_dir,
        textwrap.dedent("""
            services:
              myapp:
                build: ../app/build
        """),
    )

    socket_client = AsyncMock(spec=DockerSocketClient)
    socket_client.list_containers.return_value = []  # Empty list

    loader = BuildSourcesLoader(
        config_path=compose_dir / "build-sources.yaml",
        log=structlog.get_logger().bind(component="test"),  # type: ignore[arg-type]
    )

    collector = LocalBuildUpdateCollector(
        db=repo,
        socket_client=socket_client,
        build_hashes_repo=DockerBuildHashesRepository(repo),
        compose_dir=compose_dir,
        build_sources_loader=loader,
    )

    # Must not raise
    await collector.refresh_container(container_name="nonexistent")


# ---------------------------------------------------------------------------
# Lines 338-345: context_missing with ctx AND prior_row with same error reason
# (preserves check_failed_at)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_context_missing_with_ctx_emits_skipped_gauge(
    repo: SqliteRepository, tmp_path: Path
) -> None:
    """context_missing branch emits homelab_build_source_hash_skipped_total when ctx is set."""
    writer = MemoryRetainingMetricsWriter()
    ctx = _ctx(writer, repo)

    compose_dir = tmp_path / "compose"
    _write_compose(compose_dir, "services:\n  myapp:\n    build: /nonexistent-path\n")

    socket_client = AsyncMock(spec=DockerSocketClient)
    socket_client.list_containers.return_value = [
        {
            "Names": ["/myapp"],
            "Labels": {"com.docker.compose.service": "myapp"},
            "ImageID": "sha256:imageA",
        }
    ]

    collector = _make_collector(repo, socket_client, compose_dir=compose_dir)
    await collector.run(ctx)

    skipped = [g for g in writer.gauges if g[0] == "homelab_build_source_hash_skipped_total"]
    assert any(g[2].get("reason") == "context_missing" for g in skipped)


@pytest.mark.asyncio
async def test_context_missing_preserves_check_failed_at_when_same_reason(
    repo: SqliteRepository, tmp_path: Path
) -> None:
    """check_failed_at is preserved when context_missing reason unchanged (line 345-348)."""
    writer = MemoryRetainingMetricsWriter()
    ctx = _ctx(writer, repo)

    original_failed_at = "2026-01-01T00:00:00+00:00"
    async with repo.transaction() as conn:
        await DockerBuildHashesRepository.upsert_conn(
            conn,
            container_name="myapp",
            compose_service="myapp",
            build_context_path="/nonexistent-path",
            last_source_hash=None,
            last_checked_at="2026-01-02T00:00:00+00:00",
            check_failed_at=original_failed_at,
            check_error_reason="context_missing",
            update_available=False,
            baseline_source_hash=None,
            baseline_image_id=None,
        )

    compose_dir = tmp_path / "compose"
    _write_compose(compose_dir, "services:\n  myapp:\n    build: /nonexistent-path\n")

    socket_client = AsyncMock(spec=DockerSocketClient)
    socket_client.list_containers.return_value = [
        {
            "Names": ["/myapp"],
            "Labels": {"com.docker.compose.service": "myapp"},
            "ImageID": "sha256:imageA",
        }
    ]

    collector = _make_collector(repo, socket_client, compose_dir=compose_dir)
    await collector.run(ctx)

    row = await DockerBuildHashesRepository(repo).get_by_container("myapp")
    assert row is not None
    assert row.check_failed_at == original_failed_at  # preserved, not reset to now


# ---------------------------------------------------------------------------
# Lines 366-384: exceeded with ctx AND prior_row with same error reason
# (preserves check_failed_at for oversized case)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_oversized_with_ctx_emits_two_gauges(repo: SqliteRepository, tmp_path: Path) -> None:
    """Exceeded branch emits both skipped + image_update_available gauges (lines 366-382)."""
    writer = MemoryRetainingMetricsWriter()
    ctx = _ctx(writer, repo)

    ctx_dir = tmp_path / "ctx"
    ctx_dir.mkdir()
    (ctx_dir / "big.bin").write_bytes(b"x" * 200)

    compose_dir = tmp_path / "compose"
    _write_compose(compose_dir, f"services:\n  myapp:\n    build: {ctx_dir}\n")

    socket_client = AsyncMock(spec=DockerSocketClient)
    socket_client.list_containers.return_value = [
        {
            "Names": ["/myapp"],
            "Labels": {"com.docker.compose.service": "myapp"},
            "ImageID": "sha256:imageA",
        }
    ]

    tiny_limits = SourceHashLimits(max_file_bytes=10)
    collector = LocalBuildUpdateCollector(
        db=repo,
        socket_client=socket_client,
        build_hashes_repo=DockerBuildHashesRepository(repo),
        compose_dir=compose_dir,
        limits=tiny_limits,
    )
    await collector.run(ctx)

    skipped = [g for g in writer.gauges if g[0] == "homelab_build_source_hash_skipped_total"]
    assert any(g[2].get("reason") == "context_too_large" for g in skipped)
    update_gauges = [g for g in writer.gauges if g[0] == "homelab_image_update_available"]
    assert any(g[2].get("source") == "local_build" for g in update_gauges)


@pytest.mark.asyncio
async def test_oversized_preserves_check_failed_at_when_same_reason(
    repo: SqliteRepository, tmp_path: Path
) -> None:
    """check_failed_at preserved when exceeded reason unchanged (line 384-387)."""
    writer = MemoryRetainingMetricsWriter()
    ctx = _ctx(writer, repo)

    ctx_dir = tmp_path / "ctx"
    ctx_dir.mkdir()
    (ctx_dir / "big.bin").write_bytes(b"x" * 200)

    original_failed_at = "2026-01-01T00:00:00+00:00"
    async with repo.transaction() as conn:
        await DockerBuildHashesRepository.upsert_conn(
            conn,
            container_name="myapp",
            compose_service="myapp",
            build_context_path=str(ctx_dir),
            last_source_hash="OVERSIZED:abc",
            last_checked_at="2026-01-02T00:00:00+00:00",
            check_failed_at=original_failed_at,
            check_error_reason="context_too_large",
            update_available=True,
            baseline_source_hash=None,
            baseline_image_id=None,
        )

    compose_dir = tmp_path / "compose"
    _write_compose(compose_dir, f"services:\n  myapp:\n    build: {ctx_dir}\n")

    socket_client = AsyncMock(spec=DockerSocketClient)
    socket_client.list_containers.return_value = [
        {
            "Names": ["/myapp"],
            "Labels": {"com.docker.compose.service": "myapp"},
            "ImageID": "sha256:imageA",
        }
    ]

    tiny_limits = SourceHashLimits(max_file_bytes=10)
    collector = LocalBuildUpdateCollector(
        db=repo,
        socket_client=socket_client,
        build_hashes_repo=DockerBuildHashesRepository(repo),
        compose_dir=compose_dir,
        limits=tiny_limits,
    )
    await collector.run(ctx)

    row = await DockerBuildHashesRepository(repo).get_by_container("myapp")
    assert row is not None
    assert row.check_failed_at == original_failed_at  # preserved


# ---------------------------------------------------------------------------
# Line 485: refresh_container no-op when config=None and compose_dir=None
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_container_noop_when_no_compose_config_and_no_compose_dir(
    repo: SqliteRepository,
) -> None:
    """refresh_container returns early when config=None and compose_dir=None (line 485)."""
    socket_client = AsyncMock(spec=DockerSocketClient)

    # Loader whose current_config is None (missing file)
    loader = BuildSourcesLoader(
        config_path=Path("/nonexistent/build-sources.yaml"),
        log=structlog.get_logger().bind(component="test"),  # type: ignore[arg-type]
    )

    collector = LocalBuildUpdateCollector(
        db=repo,
        socket_client=socket_client,
        build_hashes_repo=DockerBuildHashesRepository(repo),
        compose_dir=None,  # no env-var fallback
        build_sources_loader=loader,
    )

    # Must not raise and must not call socket_client
    await collector.refresh_container(container_name="myapp")
    socket_client.list_containers.assert_not_called()


# ---------------------------------------------------------------------------
# Lines 488-489: refresh_container uses loader compose_paths + PathResolver
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_container_uses_loader_config_when_present(
    repo: SqliteRepository, tmp_path: Path
) -> None:
    """refresh_container reads compose_paths from loader config (lines 488-489)."""
    build_context = tmp_path / "build"
    build_context.mkdir()
    (build_context / "Dockerfile").write_text("FROM alpine\n", encoding="utf-8")

    compose_dir = tmp_path / "compose"
    _write_compose(compose_dir, f"services:\n  myapp:\n    build: {build_context}\n")

    yaml_path = tmp_path / "build-sources.yaml"
    yaml_path.write_text(_build_sources_yaml(compose_dir / "docker-compose.yml"), encoding="utf-8")
    loader = await _make_loader(yaml_path)
    assert loader.current_config is not None

    socket_client = AsyncMock(spec=DockerSocketClient)
    socket_client.list_containers.return_value = [
        {
            "Names": ["/myapp"],
            "Labels": {"com.docker.compose.service": "myapp"},
            "ImageID": "sha256:new-img",
        }
    ]

    collector = LocalBuildUpdateCollector(
        db=repo,
        socket_client=socket_client,
        build_hashes_repo=DockerBuildHashesRepository(repo),
        compose_dir=None,  # force loader path
        build_sources_loader=loader,
    )

    # Must not raise; should upsert a row
    await collector.refresh_container(container_name="myapp")

    row = await DockerBuildHashesRepository(repo).get_by_container("myapp")
    assert row is not None
    assert row.update_available is False
    assert row.baseline_image_id == "sha256:new-img"


# ---------------------------------------------------------------------------
# Lines 497-498: refresh_container no-op on ComposeReadError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_container_noop_on_compose_read_error(
    repo: SqliteRepository, tmp_path: Path
) -> None:
    """refresh_container returns early on ComposeReadError (lines 497-498)."""

    compose_dir = tmp_path / "compose"
    _write_compose(compose_dir, "services: {\nbroken: [unclosed\n")

    socket_client = AsyncMock(spec=DockerSocketClient)

    collector = LocalBuildUpdateCollector(
        db=repo,
        socket_client=socket_client,
        build_hashes_repo=DockerBuildHashesRepository(repo),
        compose_dir=compose_dir,
    )

    # Must not raise; no upsert
    await collector.refresh_container(container_name="myapp")
    socket_client.list_containers.assert_not_called()


# ---------------------------------------------------------------------------
# Lines 503-504: refresh_container no-op on socket error
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_container_noop_on_socket_error(
    repo: SqliteRepository, tmp_path: Path
) -> None:
    """refresh_container returns early when list_containers raises (lines 503-504)."""
    build_context = tmp_path / "build"
    build_context.mkdir()
    compose_dir = tmp_path / "compose"
    _write_compose(compose_dir, f"services:\n  myapp:\n    build: {build_context}\n")

    socket_client = AsyncMock(spec=DockerSocketClient)
    socket_client.list_containers.side_effect = OSError("socket broken")

    collector = LocalBuildUpdateCollector(
        db=repo,
        socket_client=socket_client,
        build_hashes_repo=DockerBuildHashesRepository(repo),
        compose_dir=compose_dir,
    )

    # Must not raise
    await collector.refresh_container(container_name="myapp")

    row = await DockerBuildHashesRepository(repo).get_by_container("myapp")
    assert row is None  # nothing written


# ---------------------------------------------------------------------------
# Line 517: refresh_container skips entries with empty Names
# Line 522-523: target_entry is None → no-op
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_container_skips_entries_with_no_names(
    repo: SqliteRepository, tmp_path: Path
) -> None:
    """refresh_container skips entries with empty Names and returns early (lines 516-523)."""
    build_context = tmp_path / "build"
    build_context.mkdir()
    compose_dir = tmp_path / "compose"
    _write_compose(compose_dir, f"services:\n  myapp:\n    build: {build_context}\n")

    socket_client = AsyncMock(spec=DockerSocketClient)
    # One entry with no Names — should be skipped, leaving target_entry=None
    socket_client.list_containers.return_value = [
        {"Names": [], "Labels": {}, "ImageID": "sha256:img"},
    ]

    collector = LocalBuildUpdateCollector(
        db=repo,
        socket_client=socket_client,
        build_hashes_repo=DockerBuildHashesRepository(repo),
        compose_dir=compose_dir,
    )

    await collector.refresh_container(container_name="myapp")

    row = await DockerBuildHashesRepository(repo).get_by_container("myapp")
    assert row is None  # no upsert since target_entry was None


@pytest.mark.asyncio
async def test_refresh_container_noop_when_name_not_matched_in_list(
    repo: SqliteRepository, tmp_path: Path
) -> None:
    """Return early when no entry name matches (branch 518->514 exhausts loop)."""
    build_context = tmp_path / "build"
    build_context.mkdir()
    compose_dir = tmp_path / "compose"
    _write_compose(compose_dir, f"services:\n  myapp:\n    build: {build_context}\n")

    socket_client = AsyncMock(spec=DockerSocketClient)
    # Entry has a non-empty name that does NOT match "myapp"
    socket_client.list_containers.return_value = [
        {"Names": ["/other-container"], "Labels": {}, "ImageID": "sha256:img"},
    ]

    collector = LocalBuildUpdateCollector(
        db=repo,
        socket_client=socket_client,
        build_hashes_repo=DockerBuildHashesRepository(repo),
        compose_dir=compose_dir,
    )

    await collector.refresh_container(container_name="myapp")

    row = await DockerBuildHashesRepository(repo).get_by_container("myapp")
    assert row is None  # no upsert — loop exhausted without finding target


# ---------------------------------------------------------------------------
# Lines 529-530: refresh_container no-op when service not in build_services
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_container_noop_when_service_not_a_build_service(
    repo: SqliteRepository, tmp_path: Path
) -> None:
    """refresh_container returns early when compose service has no build context (lines 529-530)."""
    compose_dir = tmp_path / "compose"
    # Service has only image:, no build:
    _write_compose(compose_dir, "services:\n  myapp:\n    image: nginx:latest\n")

    socket_client = AsyncMock(spec=DockerSocketClient)
    socket_client.list_containers.return_value = [
        {
            "Names": ["/myapp"],
            "Labels": {"com.docker.compose.service": "myapp"},
            "ImageID": "sha256:img",
        }
    ]

    collector = LocalBuildUpdateCollector(
        db=repo,
        socket_client=socket_client,
        build_hashes_repo=DockerBuildHashesRepository(repo),
        compose_dir=compose_dir,
    )

    await collector.refresh_container(container_name="myapp")

    row = await DockerBuildHashesRepository(repo).get_by_container("myapp")
    assert row is None  # no upsert


# ---------------------------------------------------------------------------
# Branch 338->345 (ctx=None, context_missing) and 366->384 (ctx=None, exceeded)
# These branches are taken when refresh_container calls _build_upsert_payload
# with ctx=None — the gauge-write block is skipped, jumping past it.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_container_context_missing_no_ctx_branch(
    repo: SqliteRepository, tmp_path: Path
) -> None:
    """Missing build context skips gauge emission (ctx=None, line 338->345)."""
    compose_dir = tmp_path / "compose"
    # Reference a build context path that will not exist on disk
    missing_ctx = tmp_path / "nonexistent-build"
    _write_compose(compose_dir, f"services:\n  myapp:\n    build: {missing_ctx}\n")

    socket_client = AsyncMock(spec=DockerSocketClient)
    socket_client.list_containers.return_value = [
        {
            "Names": ["/myapp"],
            "Labels": {"com.docker.compose.service": "myapp"},
            "ImageID": "sha256:img",
        }
    ]

    collector = LocalBuildUpdateCollector(
        db=repo,
        socket_client=socket_client,
        build_hashes_repo=DockerBuildHashesRepository(repo),
        compose_dir=compose_dir,
    )

    # refresh_container calls _build_upsert_payload(ctx=None, ...) — no gauge, but payload returned
    await collector.refresh_container(container_name="myapp")

    row = await DockerBuildHashesRepository(repo).get_by_container("myapp")
    assert row is not None
    assert row.check_error_reason == "context_missing"


@pytest.mark.asyncio
async def test_refresh_container_oversized_context_no_ctx_branch(
    repo: SqliteRepository, tmp_path: Path
) -> None:
    """refresh_container with oversized context skips gauge emission (ctx=None, line 366->384)."""
    ctx_dir = tmp_path / "ctx"
    ctx_dir.mkdir()
    (ctx_dir / "big.bin").write_bytes(b"x" * 200)

    compose_dir = tmp_path / "compose"
    _write_compose(compose_dir, f"services:\n  myapp:\n    build: {ctx_dir}\n")

    socket_client = AsyncMock(spec=DockerSocketClient)
    socket_client.list_containers.return_value = [
        {
            "Names": ["/myapp"],
            "Labels": {"com.docker.compose.service": "myapp"},
            "ImageID": "sha256:img",
        }
    ]

    tiny_limits = SourceHashLimits(max_file_bytes=10)
    collector = LocalBuildUpdateCollector(
        db=repo,
        socket_client=socket_client,
        build_hashes_repo=DockerBuildHashesRepository(repo),
        compose_dir=compose_dir,
        limits=tiny_limits,
    )

    await collector.refresh_container(container_name="myapp")

    row = await DockerBuildHashesRepository(repo).get_by_container("myapp")
    assert row is not None
    assert row.check_error_reason == "context_too_large"


# ---------------------------------------------------------------------------
# Line 551: refresh_container no-op when payload is None (hash exceeded limits)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_container_noop_when_hash_exceeds_limits(
    repo: SqliteRepository, tmp_path: Path
) -> None:
    """refresh_container returns early when _build_upsert_payload returns None (line 551).

    This cannot happen today because reset_baseline=True never returns None
    (exceeded returns a payload, not None). The only way payload is None is a
    future code path, so we patch _build_upsert_payload to force the branch.
    """
    build_context = tmp_path / "build"
    build_context.mkdir()
    (build_context / "Dockerfile").write_text("FROM alpine\n", encoding="utf-8")

    compose_dir = tmp_path / "compose"
    _write_compose(compose_dir, f"services:\n  myapp:\n    build: {build_context}\n")

    socket_client = AsyncMock(spec=DockerSocketClient)
    socket_client.list_containers.return_value = [
        {
            "Names": ["/myapp"],
            "Labels": {"com.docker.compose.service": "myapp"},
            "ImageID": "sha256:img",
        }
    ]

    collector = LocalBuildUpdateCollector(
        db=repo,
        socket_client=socket_client,
        build_hashes_repo=DockerBuildHashesRepository(repo),
        compose_dir=compose_dir,
    )

    # Force _build_upsert_payload to return None
    with patch.object(collector, "_build_upsert_payload", return_value=None):
        await collector.refresh_container(container_name="myapp")

    row = await DockerBuildHashesRepository(repo).get_by_container("myapp")
    assert row is None  # no upsert when payload is None
