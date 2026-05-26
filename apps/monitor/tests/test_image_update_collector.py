"""Tests for ImageUpdateCollector (STAGE-003-008).

Per-image failure isolation, rate-limit hard-cap, state persistence.
"""

from __future__ import annotations

import contextlib
import importlib
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import structlog

from homelab_monitor.kernel.db.repositories.image_update_state_repository import (
    ImageUpdateStateRepository,
)
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.docker.names import canonicalize_container_name
from homelab_monitor.kernel.docker.registry_digest_client import (
    FetchedDigest,
    FetchError,
    RegistryDigestClient,
)
from homelab_monitor.kernel.docker.socket_client import DockerSocketClient
from homelab_monitor.kernel.metrics import image_update_collector as iuc_module
from homelab_monitor.kernel.metrics.image_update_collector import (
    _DEFAULT_INTERVAL_SECONDS,  # pyright: ignore[reportPrivateUsage]
    ImageUpdateCollector,
    _resolve_interval_seconds,  # pyright: ignore[reportPrivateUsage]
)
from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.io import InMemoryLogsWriter, MemoryRetainingMetricsWriter
from homelab_monitor.kernel.plugins.types import CollectorConfig

_EXPECTED_REMAINING_LOW = 5
_EXPECTED_HARD_CAP = 10
_EXPECTED_REMAINING_HIGH = 100
_EXPECTED_CALL_COUNT_ONE = 1
_EXPECTED_CALL_COUNT_TWO = 2
_EXPECTED_SKIPPED_COUNT_ONE = 1
_EXPECTED_SKIPPED_COUNT_ZERO = 0
_EXPECTED_REGISTRY_COUNT = 2


def _ctx(
    writer: MemoryRetainingMetricsWriter,
    repo: SqliteRepository,
) -> CollectorContext:
    """Minimal CollectorContext for ImageUpdateCollector."""
    return CollectorContext(
        config=CollectorConfig(name="image_update_checker"),
        db=repo,
        vm=writer,
        vl=InMemoryLogsWriter(),
        http=AsyncMock(),
        ssh=None,  # pyright: ignore[reportArgumentType]
        secrets=None,  # pyright: ignore[reportArgumentType]
        log=structlog.get_logger().bind(collector="image_update_checker"),  # pyright: ignore[reportArgumentType]
        ha=None,
    )


@pytest.mark.asyncio
async def test_run_returns_error_when_dependencies_unwired(repo: SqliteRepository) -> None:
    """run() returns ok=False when dependencies are None."""
    in_memory_writer = MemoryRetainingMetricsWriter()
    ctx = _ctx(in_memory_writer, repo)

    # No dependencies wired
    collector = ImageUpdateCollector()
    result = await collector.run(ctx)

    assert result.ok is False
    assert "dependencies_unwired" in result.errors
    assert result.metrics_emitted == _EXPECTED_CALL_COUNT_ONE  # self-metric


@pytest.mark.asyncio
async def test_run_skips_unparseable_image_ref(repo: SqliteRepository) -> None:
    """run() skips containers with unparseable image refs (<none>, sha256-only)."""
    in_memory_writer = MemoryRetainingMetricsWriter()
    ctx = _ctx(in_memory_writer, repo)

    socket_client = AsyncMock(spec=DockerSocketClient)
    socket_client.list_containers.return_value = [
        {
            "Id": "c1",
            "Names": ["/skip-none"],
            "Image": "<none>",
            "ImageID": "sha256:abc",
            "State": "running",
            "Status": "Up 1h",
            "Labels": {},
        },
        {
            "Id": "c2",
            "Names": ["/skip-bare-digest"],
            "Image": "sha256:def123",
            "ImageID": "sha256:def123",
            "State": "running",
            "Status": "Up 1h",
            "Labels": {},
        },
    ]

    registry_client = AsyncMock(spec=RegistryDigestClient)
    collector = ImageUpdateCollector(
        db=repo,
        socket_client=socket_client,
        registry_client=registry_client,
        image_update_state_repo=ImageUpdateStateRepository(repo),
    )

    result = await collector.run(ctx)

    # Both containers skipped -> no registry calls
    assert registry_client.fetch_latest_digest.call_count == _EXPECTED_SKIPPED_COUNT_ZERO
    # One self-metric
    assert result.metrics_emitted == _EXPECTED_CALL_COUNT_ONE


@pytest.mark.asyncio
async def test_run_emits_update_available_1_when_digests_differ(repo: SqliteRepository) -> None:
    """run() emits homelab_image_update_available=1 when local != registry digests."""
    in_memory_writer = MemoryRetainingMetricsWriter()
    ctx = _ctx(in_memory_writer, repo)

    socket_client = AsyncMock(spec=DockerSocketClient)
    socket_client.list_containers.return_value = [
        {
            "Id": "c1",
            "Names": ["/myapp"],
            "Image": "nginx:latest",
            "ImageID": "sha256:old123",
            "State": "running",
            "Status": "Up 1h",
            "Labels": {},
        }
    ]
    socket_client.image_inspect.return_value = {
        "Id": "sha256:old123",
        "RepoDigests": ["docker.io/library/nginx@sha256:old123digest"],
    }

    registry_client = AsyncMock(spec=RegistryDigestClient)
    registry_client.fetch_latest_digest.return_value = FetchedDigest(
        digest="sha256:new456digest",
        rate_limit_remaining=_EXPECTED_REMAINING_HIGH,
        registry="docker.io",
    )

    collector = ImageUpdateCollector(
        db=repo,
        socket_client=socket_client,
        registry_client=registry_client,
        image_update_state_repo=ImageUpdateStateRepository(repo),
    )

    result = await collector.run(ctx)

    assert result.ok is True
    # Check metrics: homelab_image_update_available + rate_limit + self-metric
    assert in_memory_writer.last_gauge("homelab_image_update_available") == 1.0


@pytest.mark.asyncio
async def test_run_emits_update_available_0_when_digests_match(repo: SqliteRepository) -> None:
    """run() emits homelab_image_update_available=0 when digests match."""
    in_memory_writer = MemoryRetainingMetricsWriter()
    ctx = _ctx(in_memory_writer, repo)

    same_digest = "sha256:same123"

    socket_client = AsyncMock(spec=DockerSocketClient)
    socket_client.list_containers.return_value = [
        {
            "Id": "c1",
            "Names": ["/myapp"],
            "Image": "nginx:latest",
            "ImageID": "sha256:imageid",
            "State": "running",
            "Status": "Up 1h",
            "Labels": {},
        }
    ]
    socket_client.image_inspect.return_value = {
        "Id": "sha256:imageid",
        "RepoDigests": [f"docker.io/library/nginx@{same_digest}"],
    }

    registry_client = AsyncMock(spec=RegistryDigestClient)
    registry_client.fetch_latest_digest.return_value = FetchedDigest(
        digest=same_digest,
        rate_limit_remaining=_EXPECTED_REMAINING_HIGH,
        registry="docker.io",
    )

    collector = ImageUpdateCollector(
        db=repo,
        socket_client=socket_client,
        registry_client=registry_client,
        image_update_state_repo=ImageUpdateStateRepository(repo),
    )

    result = await collector.run(ctx)

    assert result.ok is True
    assert in_memory_writer.last_gauge("homelab_image_update_available") == 0.0


@pytest.mark.asyncio
async def test_run_upserts_state_row_on_success(repo: SqliteRepository) -> None:
    """run() upserts a row to image_update_state on successful check."""
    in_memory_writer = MemoryRetainingMetricsWriter()
    ctx = _ctx(in_memory_writer, repo)

    socket_client = AsyncMock(spec=DockerSocketClient)
    socket_client.list_containers.return_value = [
        {
            "Id": "c1",
            "Names": ["/myapp"],
            "Image": "nginx:latest",
            "ImageID": "sha256:imageid",
            "State": "running",
            "Status": "Up 1h",
            "Labels": {},
        }
    ]
    socket_client.image_inspect.return_value = {
        "Id": "sha256:imageid",
        "RepoDigests": ["docker.io/library/nginx@sha256:local"],
    }

    registry_client = AsyncMock(spec=RegistryDigestClient)
    registry_client.fetch_latest_digest.return_value = FetchedDigest(
        digest="sha256:registry",
        rate_limit_remaining=100,
        registry="docker.io",
    )

    state_repo = ImageUpdateStateRepository(repo)
    collector = ImageUpdateCollector(
        db=repo,
        socket_client=socket_client,
        registry_client=registry_client,
        image_update_state_repo=state_repo,
    )

    result = await collector.run(ctx)

    assert result.ok is True
    # Query the state repo to verify the row was upserted
    rows = await state_repo.list_all()
    assert len(rows) == 1
    row = rows[0]
    assert row.container_name == "myapp"
    assert row.last_image_ref == "nginx:latest"
    assert row.check_error_reason is None


@pytest.mark.asyncio
async def test_run_handles_fetch_error_emits_zero_gauge_and_persists_reason(
    repo: SqliteRepository,
) -> None:
    """run() handles FetchError: emits gauge=0 and persists error reason."""
    in_memory_writer = MemoryRetainingMetricsWriter()
    ctx = _ctx(in_memory_writer, repo)

    socket_client = AsyncMock(spec=DockerSocketClient)
    socket_client.list_containers.return_value = [
        {
            "Id": "c1",
            "Names": ["/myapp"],
            "Image": "nginx:latest",
            "ImageID": "sha256:imageid",
            "State": "running",
            "Status": "Up 1h",
            "Labels": {},
        }
    ]
    socket_client.image_inspect.return_value = {
        "Id": "sha256:imageid",
        "RepoDigests": ["docker.io/library/nginx@sha256:local"],
    }

    registry_client = AsyncMock(spec=RegistryDigestClient)
    registry_client.fetch_latest_digest.return_value = FetchError(
        reason="not_found",
        message="image not in registry",
        registry="docker.io",
    )

    state_repo = ImageUpdateStateRepository(repo)
    collector = ImageUpdateCollector(
        db=repo,
        socket_client=socket_client,
        registry_client=registry_client,
        image_update_state_repo=state_repo,
    )

    result = await collector.run(ctx)

    assert result.ok is True
    assert in_memory_writer.last_gauge("homelab_image_update_available") == 0.0

    # Check state persistence
    rows = await state_repo.list_all()
    assert len(rows) == 1
    assert rows[0].check_error_reason == "not_found"


@pytest.mark.asyncio
async def test_run_per_container_failure_isolation(repo: SqliteRepository) -> None:
    """run() continues when one container's _process_one_container raises."""
    in_memory_writer = MemoryRetainingMetricsWriter()
    ctx = _ctx(in_memory_writer, repo)

    socket_client = AsyncMock(spec=DockerSocketClient)
    socket_client.list_containers.return_value = [
        {
            "Id": "c1",
            "Names": ["/app1"],
            "Image": "nginx:latest",
            "ImageID": "sha256:id1",
            "State": "running",
            "Status": "Up 1h",
            "Labels": {},
        },
        {
            "Id": "c2",
            "Names": ["/app2"],
            "Image": "postgres:15",
            "ImageID": "sha256:id2",
            "State": "running",
            "Status": "Up 1h",
            "Labels": {},
        },
    ]
    # Make image_inspect raise for c2
    socket_client.image_inspect.side_effect = [
        {"RepoDigests": ["docker.io/library/nginx@sha256:d1"]},  # c1 OK
        RuntimeError("socket error"),  # c2 raises
    ]

    registry_client = AsyncMock(spec=RegistryDigestClient)
    registry_client.fetch_latest_digest.return_value = FetchedDigest(
        digest="sha256:new",
        rate_limit_remaining=_EXPECTED_REMAINING_HIGH,
        registry="docker.io",
    )

    collector = ImageUpdateCollector(
        db=repo,
        socket_client=socket_client,
        registry_client=registry_client,
        image_update_state_repo=ImageUpdateStateRepository(repo),
    )

    result = await collector.run(ctx)

    # Should succeed overall despite c2 failure
    assert result.ok is True
    # c1 should have been processed (1 metric)
    # c2 should have caused image_inspect to fail but not crash the collector
    assert registry_client.fetch_latest_digest.call_count == _EXPECTED_REGISTRY_COUNT


@pytest.mark.asyncio
async def test_run_rate_limit_hard_cap_skips_remaining_for_registry(
    repo: SqliteRepository,
) -> None:
    """run() skips remaining checks when rate_limit_remaining < hard_cap."""
    in_memory_writer = MemoryRetainingMetricsWriter()
    ctx = _ctx(in_memory_writer, repo)

    socket_client = AsyncMock(spec=DockerSocketClient)
    socket_client.list_containers.return_value = [
        {
            "Id": "c1",
            "Names": ["/app1"],
            "Image": "nginx:latest",
            "ImageID": "sha256:id1",
            "State": "running",
            "Status": "Up 1h",
            "Labels": {},
        },
        {
            "Id": "c2",
            "Names": ["/app2"],
            "Image": "nginx:1.0",
            "ImageID": "sha256:id2",
            "State": "running",
            "Status": "Up 1h",
            "Labels": {},
        },
    ]
    socket_client.image_inspect.return_value = {
        "RepoDigests": ["docker.io/library/nginx@sha256:d1"]
    }

    registry_client = AsyncMock(spec=RegistryDigestClient)
    # First container: normal response with low remaining
    registry_client.fetch_latest_digest.side_effect = [
        FetchedDigest(
            digest="sha256:new1",
            rate_limit_remaining=_EXPECTED_REMAINING_LOW,  # < hard_cap (_EXPECTED_HARD_CAP)
            registry="docker.io",
        ),
        # Second container would be skipped due to rate limit hard cap
    ]

    collector = ImageUpdateCollector(
        db=repo,
        socket_client=socket_client,
        registry_client=registry_client,
        image_update_state_repo=ImageUpdateStateRepository(repo),
        hard_cap_remaining=_EXPECTED_HARD_CAP,
    )

    result = await collector.run(ctx)

    assert result.ok is True
    # Only first container called fetch; second should be skipped
    assert registry_client.fetch_latest_digest.call_count == _EXPECTED_CALL_COUNT_ONE
    assert in_memory_writer.last_gauge("homelab_image_update_check_skipped") == 1.0
    assert collector.current_skipped_count() == _EXPECTED_SKIPPED_COUNT_ONE


@pytest.mark.asyncio
async def test_run_rate_limit_hard_cap_does_not_affect_other_registries(
    repo: SqliteRepository,
) -> None:
    """run() hard cap applies per registry, not globally."""
    in_memory_writer = MemoryRetainingMetricsWriter()
    ctx = _ctx(in_memory_writer, repo)

    socket_client = AsyncMock(spec=DockerSocketClient)
    socket_client.list_containers.return_value = [
        {
            "Id": "c1",
            "Names": ["/docker-app"],
            "Image": "nginx:latest",
            "ImageID": "sha256:id1",
            "State": "running",
            "Status": "Up 1h",
            "Labels": {},
        },
        {
            "Id": "c2",
            "Names": ["/ghcr-app"],
            "Image": "ghcr.io/org/image:tag",
            "ImageID": "sha256:id2",
            "State": "running",
            "Status": "Up 1h",
            "Labels": {},
        },
    ]
    socket_client.image_inspect.return_value = {
        "RepoDigests": ["docker.io/library/nginx@sha256:d1"]
    }

    registry_client = AsyncMock(spec=RegistryDigestClient)
    registry_client.fetch_latest_digest.side_effect = [
        FetchedDigest(
            digest="sha256:new1",
            rate_limit_remaining=_EXPECTED_REMAINING_LOW,  # Low for docker.io
            registry="docker.io",
        ),
        FetchedDigest(
            digest="sha256:new2",
            rate_limit_remaining=_EXPECTED_REMAINING_HIGH,  # High for ghcr.io
            registry="ghcr.io",
        ),
    ]

    collector = ImageUpdateCollector(
        db=repo,
        socket_client=socket_client,
        registry_client=registry_client,
        image_update_state_repo=ImageUpdateStateRepository(repo),
        hard_cap_remaining=_EXPECTED_HARD_CAP,
    )

    result = await collector.run(ctx)

    assert result.ok is True
    # Both registries should be queried (cap doesn't carry between registries)
    assert registry_client.fetch_latest_digest.call_count == _EXPECTED_CALL_COUNT_TWO


@pytest.mark.asyncio
async def test_run_emits_rate_limit_remaining_gauge_per_registry(repo: SqliteRepository) -> None:
    """run() emits homelab_registry_rate_limit_remaining for each registry."""
    in_memory_writer = MemoryRetainingMetricsWriter()
    ctx = _ctx(in_memory_writer, repo)

    socket_client = AsyncMock(spec=DockerSocketClient)
    socket_client.list_containers.return_value = [
        {
            "Id": "c1",
            "Names": ["/app1"],
            "Image": "nginx:latest",
            "ImageID": "sha256:id1",
            "State": "running",
            "Status": "Up 1h",
            "Labels": {},
        }
    ]
    socket_client.image_inspect.return_value = {
        "RepoDigests": ["docker.io/library/nginx@sha256:d1"]
    }

    registry_client = AsyncMock(spec=RegistryDigestClient)
    registry_client.fetch_latest_digest.return_value = FetchedDigest(
        digest="sha256:new",
        rate_limit_remaining=_EXPECTED_REMAINING_HIGH,
        registry="docker.io",
    )

    collector = ImageUpdateCollector(
        db=repo,
        socket_client=socket_client,
        registry_client=registry_client,
        image_update_state_repo=ImageUpdateStateRepository(repo),
    )

    result = await collector.run(ctx)

    assert result.ok is True
    # Should emit rate limit gauge
    gauge_value = in_memory_writer.last_gauge("homelab_registry_rate_limit_remaining")
    assert gauge_value == float(_EXPECTED_REMAINING_HIGH)


@pytest.mark.asyncio
async def test_run_emits_image_update_check_skipped_with_reason_rate_limit(
    repo: SqliteRepository,
) -> None:
    """run() emits homelab_image_update_check_skipped{reason='rate_limit'} on hard cap."""
    in_memory_writer = MemoryRetainingMetricsWriter()
    ctx = _ctx(in_memory_writer, repo)

    socket_client = AsyncMock(spec=DockerSocketClient)
    socket_client.list_containers.return_value = [
        {
            "Id": "c1",
            "Names": ["/app1"],
            "Image": "nginx:latest",
            "ImageID": "sha256:id1",
            "State": "running",
            "Status": "Up 1h",
            "Labels": {},
        },
        {
            "Id": "c2",
            "Names": ["/app2"],
            "Image": "nginx:1.0",
            "ImageID": "sha256:id2",
            "State": "running",
            "Status": "Up 1h",
            "Labels": {},
        },
    ]
    socket_client.image_inspect.return_value = {
        "RepoDigests": ["docker.io/library/nginx@sha256:d1"]
    }

    registry_client = AsyncMock(spec=RegistryDigestClient)
    registry_client.fetch_latest_digest.return_value = FetchedDigest(
        digest="sha256:new",
        rate_limit_remaining=5,
        registry="docker.io",
    )

    collector = ImageUpdateCollector(
        db=repo,
        socket_client=socket_client,
        registry_client=registry_client,
        image_update_state_repo=ImageUpdateStateRepository(repo),
        hard_cap_remaining=10,
    )

    result = await collector.run(ctx)

    assert result.ok is True
    # Check that skipped metric was emitted with reason='rate_limit'
    gauges = in_memory_writer.gauges
    skipped_metrics = [g for g in gauges if g[0] == "homelab_image_update_check_skipped"]
    assert len(skipped_metrics) > 0
    assert any(g[2].get("reason") == "rate_limit" for g in skipped_metrics)


@pytest.mark.asyncio
async def test_run_emits_self_metric_phase_tick_result_ok(repo: SqliteRepository) -> None:
    """run() emits homelab_collector_run_image_update_checker{phase='tick', result='ok'}."""
    in_memory_writer = MemoryRetainingMetricsWriter()
    ctx = _ctx(in_memory_writer, repo)

    socket_client = AsyncMock(spec=DockerSocketClient)
    socket_client.list_containers.return_value = []

    registry_client = AsyncMock(spec=RegistryDigestClient)

    collector = ImageUpdateCollector(
        db=repo,
        socket_client=socket_client,
        registry_client=registry_client,
        image_update_state_repo=ImageUpdateStateRepository(repo),
    )

    result = await collector.run(ctx)

    assert result.ok is True
    gauges = in_memory_writer.gauges
    self_metrics = [g for g in gauges if g[0] == "homelab_collector_run_image_update_checker"]
    assert len(self_metrics) > 0
    assert any(g[2].get("phase") == "tick" and g[2].get("result") == "ok" for g in self_metrics)


@pytest.mark.asyncio
async def test_run_emits_self_metric_phase_tick_result_error_on_list_failure(
    repo: SqliteRepository,
) -> None:
    """run() emits self-metric result='error' when list_containers fails."""
    in_memory_writer = MemoryRetainingMetricsWriter()
    ctx = _ctx(in_memory_writer, repo)

    socket_client = AsyncMock(spec=DockerSocketClient)
    socket_client.list_containers.side_effect = RuntimeError("socket error")

    registry_client = AsyncMock(spec=RegistryDigestClient)

    collector = ImageUpdateCollector(
        db=repo,
        socket_client=socket_client,
        registry_client=registry_client,
        image_update_state_repo=ImageUpdateStateRepository(repo),
    )

    result = await collector.run(ctx)

    assert result.ok is False
    gauges = in_memory_writer.gauges
    self_metrics = [g for g in gauges if g[0] == "homelab_collector_run_image_update_checker"]
    assert len(self_metrics) > 0
    assert any(g[2].get("phase") == "tick" and g[2].get("result") == "error" for g in self_metrics)


def testcanonicalize_container_name_strips_slash() -> None:
    """canonicalize_container_name strips leading '/'."""
    assert canonicalize_container_name("/myapp") == "myapp"
    assert canonicalize_container_name("myapp") == "myapp"


def testcanonicalize_container_name_strips_12hex_prefix() -> None:
    """canonicalize_container_name strips <12hex>_ prefix."""
    # 12 hex chars + underscore
    assert canonicalize_container_name("/abc123def456_myapp") == "myapp"
    assert canonicalize_container_name("abc123def456_myapp") == "myapp"


def testcanonicalize_container_name_passthrough_when_no_prefix() -> None:
    """canonicalize_container_name passes through when no prefix."""
    assert canonicalize_container_name("/my-app-123") == "my-app-123"
    assert canonicalize_container_name("my-app-123") == "my-app-123"


@pytest.mark.asyncio
async def test_current_rate_limit_remaining_returns_frozen_view(repo: SqliteRepository) -> None:
    """current_rate_limit_remaining() returns a frozen MappingProxyType."""
    socket_client = AsyncMock(spec=DockerSocketClient)
    socket_client.list_containers.return_value = [
        {
            "Id": "c1",
            "Names": ["/app"],
            "Image": "nginx:latest",
            "ImageID": "sha256:id",
            "State": "running",
            "Status": "Up 1h",
            "Labels": {},
        }
    ]
    socket_client.image_inspect.return_value = {"RepoDigests": ["docker.io/library/nginx@sha256:d"]}

    registry_client = AsyncMock(spec=RegistryDigestClient)
    registry_client.fetch_latest_digest.return_value = FetchedDigest(
        digest="sha256:new",
        rate_limit_remaining=100,
        registry="docker.io",
    )

    in_memory_writer = MemoryRetainingMetricsWriter()
    ctx = _ctx(in_memory_writer, repo)

    collector = ImageUpdateCollector(
        db=repo,
        socket_client=socket_client,
        registry_client=registry_client,
        image_update_state_repo=ImageUpdateStateRepository(repo),
    )

    await collector.run(ctx)

    # Get the view and verify it's frozen
    view = collector.current_rate_limit_remaining()
    assert view["docker.io"] == _EXPECTED_REMAINING_HIGH

    # Verify mutation doesn't affect internal state
    with contextlib.suppress(TypeError):
        # Expected — MappingProxyType is read-only
        view["docker.io"] = 999  # type: ignore[index]

    # Check that internal state wasn't modified
    assert collector.current_rate_limit_remaining()["docker.io"] == _EXPECTED_REMAINING_HIGH


@pytest.mark.asyncio
async def test_current_skipped_count_resets_per_tick(repo: SqliteRepository) -> None:
    """current_skipped_count() resets to 0 at the start of each tick."""
    in_memory_writer = MemoryRetainingMetricsWriter()
    ctx = _ctx(in_memory_writer, repo)

    socket_client = AsyncMock(spec=DockerSocketClient)
    socket_client.list_containers.return_value = [
        {
            "Id": "c1",
            "Names": ["/app1"],
            "Image": "nginx:latest",
            "ImageID": "sha256:id1",
            "State": "running",
            "Status": "Up 1h",
            "Labels": {},
        },
        {
            "Id": "c2",
            "Names": ["/app2"],
            "Image": "nginx:1.0",
            "ImageID": "sha256:id2",
            "State": "running",
            "Status": "Up 1h",
            "Labels": {},
        },
    ]
    socket_client.image_inspect.return_value = {"RepoDigests": ["docker.io/library/nginx@sha256:d"]}

    registry_client = AsyncMock(spec=RegistryDigestClient)
    registry_client.fetch_latest_digest.return_value = FetchedDigest(
        digest="sha256:new",
        rate_limit_remaining=_EXPECTED_REMAINING_LOW,
        registry="docker.io",
    )

    collector = ImageUpdateCollector(
        db=repo,
        socket_client=socket_client,
        registry_client=registry_client,
        image_update_state_repo=ImageUpdateStateRepository(repo),
        hard_cap_remaining=_EXPECTED_HARD_CAP,
    )

    # First tick: c1 fetches, c2 skipped
    result1 = await collector.run(ctx)
    assert result1.ok is True
    assert collector.current_skipped_count() == _EXPECTED_SKIPPED_COUNT_ONE

    # Clear persisted rate-limit state so second tick starts fresh (no carryover)
    collector._last_rate_limit_remaining.clear()  # pyright: ignore[reportPrivateUsage]

    # Reset for second tick
    socket_client.list_containers.return_value = [
        {
            "Id": "c3",
            "Names": ["/app3"],
            "Image": "nginx:2.0",
            "ImageID": "sha256:id3",
            "State": "running",
            "Status": "Up 1h",
            "Labels": {},
        }
    ]

    # Second tick: reset should happen at start
    result2 = await collector.run(ctx)
    assert result2.ok is True
    # Should have reset to 0 (no skips in second tick)
    assert collector.current_skipped_count() == _EXPECTED_SKIPPED_COUNT_ZERO


@pytest.mark.asyncio
async def test_image_inspect_failure_falls_back_to_no_local_digest(repo: SqliteRepository) -> None:
    """run() falls back to local_digest=None when image_inspect fails."""
    in_memory_writer = MemoryRetainingMetricsWriter()
    ctx = _ctx(in_memory_writer, repo)

    socket_client = AsyncMock(spec=DockerSocketClient)
    socket_client.list_containers.return_value = [
        {
            "Id": "c1",
            "Names": ["/app"],
            "Image": "nginx:latest",
            "ImageID": "sha256:imageid",
            "State": "running",
            "Status": "Up 1h",
            "Labels": {},
        }
    ]
    socket_client.image_inspect.side_effect = RuntimeError("socket error")

    registry_client = AsyncMock(spec=RegistryDigestClient)
    registry_client.fetch_latest_digest.return_value = FetchedDigest(
        digest="sha256:registry",
        rate_limit_remaining=100,
        registry="docker.io",
    )

    state_repo = ImageUpdateStateRepository(repo)
    collector = ImageUpdateCollector(
        db=repo,
        socket_client=socket_client,
        registry_client=registry_client,
        image_update_state_repo=state_repo,
    )

    result = await collector.run(ctx)

    assert result.ok is True
    # Check state: current_digest should be None (fallback)
    rows = await state_repo.list_all()
    assert len(rows) == 1
    assert rows[0].last_local_digest is None


@pytest.mark.asyncio
async def test_image_inspect_no_repo_digests_treats_local_as_none(repo: SqliteRepository) -> None:
    """run() treats missing RepoDigests as local_digest=None."""
    in_memory_writer = MemoryRetainingMetricsWriter()
    ctx = _ctx(in_memory_writer, repo)

    socket_client = AsyncMock(spec=DockerSocketClient)
    socket_client.list_containers.return_value = [
        {
            "Id": "c1",
            "Names": ["/app"],
            "Image": "local-build:latest",
            "ImageID": "sha256:imageid",
            "State": "running",
            "Status": "Up 1h",
            "Labels": {},
        }
    ]
    socket_client.image_inspect.return_value = {
        "Id": "sha256:imageid",
        # No RepoDigests (local build image)
    }

    registry_client = AsyncMock(spec=RegistryDigestClient)
    registry_client.fetch_latest_digest.return_value = FetchedDigest(
        digest="sha256:registry",
        rate_limit_remaining=100,
        registry="docker.io",
    )

    state_repo = ImageUpdateStateRepository(repo)
    collector = ImageUpdateCollector(
        db=repo,
        socket_client=socket_client,
        registry_client=registry_client,
        image_update_state_repo=state_repo,
    )

    result = await collector.run(ctx)

    assert result.ok is True
    rows = await state_repo.list_all()
    assert len(rows) == 1
    assert rows[0].last_local_digest is None


def test_interval_resolution_from_env_var() -> None:
    """_resolve_interval_seconds() reads from HOMELAB_MONITOR_IMAGE_UPDATE_INTERVAL_SECONDS."""
    expected_env_interval = 3600
    with patch.dict(
        os.environ,
        {"HOMELAB_MONITOR_IMAGE_UPDATE_INTERVAL_SECONDS": str(expected_env_interval)},
    ):
        # Need to reload to pick up env var
        importlib.reload(iuc_module)
        result = iuc_module._resolve_interval_seconds()  # pyright: ignore[reportPrivateUsage]
        assert result == expected_env_interval


def test_interval_resolution_uses_default_when_env_unset() -> None:
    """_resolve_interval_seconds() returns default (21600) when env unset."""
    # Ensure env is unset
    with patch.dict(os.environ, {}, clear=False):
        if "HOMELAB_MONITOR_IMAGE_UPDATE_INTERVAL_SECONDS" in os.environ:
            del os.environ["HOMELAB_MONITOR_IMAGE_UPDATE_INTERVAL_SECONDS"]
        result = _resolve_interval_seconds()
        assert result == _DEFAULT_INTERVAL_SECONDS


def test_interval_resolution_uses_default_when_env_malformed() -> None:
    """_resolve_interval_seconds() returns default when env is not an int."""
    with patch.dict(os.environ, {"HOMELAB_MONITOR_IMAGE_UPDATE_INTERVAL_SECONDS": "not-a-number"}):
        result = _resolve_interval_seconds()
        assert result == _DEFAULT_INTERVAL_SECONDS


def test_resolve_interval_seconds_returns_default_when_env_non_numeric() -> None:
    """_resolve_interval_seconds() returns default on non-numeric env (covers line 59)."""
    with patch.dict(
        os.environ,
        {"HOMELAB_MONITOR_IMAGE_UPDATE_INTERVAL_SECONDS": "not_a_number"},
    ):
        importlib.reload(iuc_module)
        result = iuc_module._resolve_interval_seconds()  # pyright: ignore[reportPrivateUsage]
        assert result == _DEFAULT_INTERVAL_SECONDS


def test_resolve_interval_seconds_returns_default_when_env_below_one() -> None:
    """_resolve_interval_seconds() returns default when env is < 1 (covers line 59 branch)."""
    with patch.dict(os.environ, {"HOMELAB_MONITOR_IMAGE_UPDATE_INTERVAL_SECONDS": "0"}):
        importlib.reload(iuc_module)
        result = iuc_module._resolve_interval_seconds()  # pyright: ignore[reportPrivateUsage]
        assert result == _DEFAULT_INTERVAL_SECONDS


@pytest.mark.asyncio
async def test_run_skips_container_with_empty_names(repo: SqliteRepository) -> None:
    """run() skips container entries where Names is empty (covers line 141)."""
    in_memory_writer = MemoryRetainingMetricsWriter()
    ctx = _ctx(in_memory_writer, repo)

    socket_client = AsyncMock(spec=DockerSocketClient)
    socket_client.list_containers.return_value = [
        {
            "Id": "c1",
            "Names": [],
            "Image": "nginx:latest",
            "ImageID": "sha256:abc",
            "State": "running",
            "Status": "Up 1h",
            "Labels": {},
        },
    ]
    registry_client = AsyncMock(spec=RegistryDigestClient)
    collector = ImageUpdateCollector(
        db=repo,
        socket_client=socket_client,
        registry_client=registry_client,
        image_update_state_repo=ImageUpdateStateRepository(repo),
    )
    result = await collector.run(ctx)
    assert result.ok is True
    # Container with empty Names was skipped — no registry calls
    assert registry_client.fetch_latest_digest.call_count == 0


def testcanonicalize_container_name_with_non_hex_12char_prefix() -> None:
    """Non-hex 12-char prefix is left intact (covers branch 334->336)."""
    # "abcdefghijkl" contains 'g'-'l' which are NOT hex characters
    result = canonicalize_container_name("/abcdefghijkl_mycontainer")
    # Should NOT strip the prefix; should return the full name without leading slash
    assert result == "abcdefghijkl_mycontainer"


def testcanonicalize_container_name_with_short_name() -> None:
    """Short name (< 13 chars) is passed through unchanged."""
    # This covers the len(name) < 13 branch (line 23 condition false)
    assert canonicalize_container_name("/short") == "short"
    assert canonicalize_container_name("short") == "short"
    assert canonicalize_container_name("/app") == "app"


@pytest.mark.asyncio
async def test_refresh_container_upserts_state_when_deps_wired(
    repo: SqliteRepository,
) -> None:
    """refresh_container upserts a state row when all dependencies are wired."""
    socket_client = AsyncMock(spec=DockerSocketClient)
    socket_client.image_inspect.return_value = {
        "Id": "sha256:localid",
        "RepoDigests": ["docker.io/library/nginx@sha256:localdigest"],
    }

    registry_client = AsyncMock(spec=RegistryDigestClient)
    registry_client.fetch_latest_digest.return_value = FetchedDigest(
        digest="sha256:registrydigest",
        rate_limit_remaining=_EXPECTED_REMAINING_HIGH,
        registry="docker.io",
    )
    # cooldown_until_for must exist on the mock (used in _process_one_container path).
    registry_client.cooldown_until_for = AsyncMock(return_value=None)

    state_repo = ImageUpdateStateRepository(repo)
    collector = ImageUpdateCollector(
        db=repo,
        socket_client=socket_client,
        registry_client=registry_client,
        image_update_state_repo=state_repo,
    )

    await collector.refresh_container(
        container_name="nginx",
        image_ref="nginx:latest",
        image_id="sha256:localid",
    )

    rows = await state_repo.list_all()
    assert len(rows) == 1
    assert rows[0].container_name == "nginx"
    assert rows[0].last_image_ref == "nginx:latest"


@pytest.mark.asyncio
async def test_process_one_container_returns_zero_when_fetch_digest_payload_none(
    repo: SqliteRepository,
) -> None:
    """_process_one_container returns (0, None) when _fetch_digest_payload returns None."""
    in_memory_writer = MemoryRetainingMetricsWriter()
    ctx = _ctx(in_memory_writer, repo)

    socket_client = AsyncMock(spec=DockerSocketClient)
    # image_inspect returns data with no RepoDigests and registry fetch returns FetchError
    # that causes _fetch_digest_payload to return a payload (FetchError payload is non-None).
    # To get payload=None we need ImageRefParseError from _fetch_digest_payload — use a
    # container whose image_ref is parseable at the outer level (so it passes the outer
    # parse check in _process_one_container) but causes _fetch_digest_payload's inner
    # parse to return None. Since both use parse_image_ref on the same ref, the simpler
    # approach: mock _fetch_digest_payload directly on the collector instance.
    registry_client = AsyncMock(spec=RegistryDigestClient)
    registry_client.cooldown_until_for = MagicMock(return_value=None)

    socket_client.list_containers.return_value = [
        {
            "Id": "c1",
            "Names": ["/myapp"],
            "Image": "nginx:latest",
            "ImageID": "sha256:abc",
            "State": "running",
            "Status": "Up 1h",
            "Labels": {},
        }
    ]

    state_repo = ImageUpdateStateRepository(repo)
    collector = ImageUpdateCollector(
        db=repo,
        socket_client=socket_client,
        registry_client=registry_client,
        image_update_state_repo=state_repo,
    )

    # Patch _fetch_digest_payload to return None so line 368→369 is hit.
    async def _null_fetch(*_a: object, **_kw: object) -> None:
        return None

    with patch.object(collector, "_fetch_digest_payload", new=_null_fetch):
        result = await collector.run(ctx)

    assert result.ok is True
    # No state row was upserted.
    rows = await state_repo.list_all()
    assert len(rows) == 0
