"""Tests for DockerSocketCollector.

STAGE-003-004: Tick algorithm, healthcheck normalization, VM merge, error handling.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import structlog

from homelab_monitor.kernel.db.repositories.targets_repository import TargetsRepository
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.docker.socket_client import (
    DockerSocketClient,
    DockerSocketConnectionError,
    DockerSocketProtocolError,
)
from homelab_monitor.kernel.metrics.docker_socket_collector import (
    DockerSocketCollector,
)
from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.io import InMemoryLogsWriter, MemoryRetainingMetricsWriter
from homelab_monitor.kernel.plugins.types import CollectorConfig

EXPECTED_METRICS_PER_CONTAINER = 3  # status, restart_count, exit_code
EXPECTED_CPU_PCT = 1.5
EXPECTED_MEM_MIB = 128.0
EXPECTED_HC_METRIC_COUNT = 4


def _ctx(
    writer: MemoryRetainingMetricsWriter,
    repo: SqliteRepository,
    http_client: AsyncMock,
) -> CollectorContext:
    """Minimal CollectorContext for DockerSocketCollector."""
    return CollectorContext(
        config=CollectorConfig(name="docker_socket"),
        db=repo,
        vm=writer,
        vl=InMemoryLogsWriter(),
        http=http_client,
        ssh=None,  # pyright: ignore[reportArgumentType]
        secrets=None,  # pyright: ignore[reportArgumentType]
        log=structlog.get_logger().bind(collector="docker_socket"),  # pyright: ignore[reportArgumentType]
        ha=None,
    )


@pytest.mark.asyncio
async def test_tick_happy_path_upserts_and_emits_metrics(repo: SqliteRepository) -> None:
    """One container -> targets + targets_docker rows + 3 gauges (no healthcheck)."""
    in_memory_writer = MemoryRetainingMetricsWriter()

    # Mock client
    client = AsyncMock(spec=DockerSocketClient)
    client.list_containers.return_value = [
        {
            "Id": "abc123def456",
            "Names": ["/test-container"],
            "Image": "img:1.0",
            "ImageID": "sha:xxxx",
            "State": "running",
            "Status": "Up 1h",
            "Labels": {},
        }
    ]
    client.inspect_container.return_value = {
        "Id": "abc123def456",
        "Name": "/test-container",
        "Image": "img:1.0",
        "State": {
            "Status": "running",
            "Running": True,
            "ExitCode": 0,
        },
        "RestartCount": 0,
        "HostConfig": {"NetworkMode": "bridge"},
    }

    # Mock HTTP client for VM query (empty result)
    http_client = AsyncMock()
    http_client.get.return_value.json = MagicMock(
        return_value={"data": {"resultType": "vector", "result": []}}
    )

    ctx = _ctx(in_memory_writer, repo, http_client)

    collector = DockerSocketCollector(client=client, vm_url="http://vm:8428")
    result = await collector.run(ctx)

    assert result.ok is True
    assert result.metrics_emitted == EXPECTED_METRICS_PER_CONTAINER
    assert len(result.errors) == 0

    # Verify DB rows
    targets_repo = TargetsRepository(repo)
    rows = await targets_repo.list_docker_containers()
    assert len(rows) == 1
    assert rows[0].name == "test-container"
    assert rows[0].status == "running"
    assert rows[0].restart_count == 0


@pytest.mark.asyncio
async def test_tick_marks_missing_container(repo: SqliteRepository) -> None:
    """Container present in tick N, absent in tick N+1 -> status='missing'."""
    in_memory_writer = MemoryRetainingMetricsWriter()

    # Tick 1: Insert container
    client = AsyncMock(spec=DockerSocketClient)
    client.list_containers.return_value = [
        {
            "Id": "abc",
            "Names": ["/foo"],
            "Image": "img:1",
            "ImageID": "sha:x",
            "State": "running",
            "Status": "Up 1h",
            "Labels": {},
        }
    ]
    client.inspect_container.return_value = {
        "Id": "abc",
        "Name": "/foo",
        "Image": "img:1",
        "State": {"Status": "running", "ExitCode": 0},
        "RestartCount": 0,
        "HostConfig": {"NetworkMode": "bridge"},
    }

    http_client = AsyncMock()
    http_client.get.return_value.json = MagicMock(
        return_value={"data": {"resultType": "vector", "result": []}}
    )

    ctx = _ctx(in_memory_writer, repo, http_client)
    collector = DockerSocketCollector(client=client, vm_url="http://vm:8428")
    result1 = await collector.run(ctx)
    assert result1.ok is True

    # Verify container is in DB
    targets_repo = TargetsRepository(repo)
    rows = await targets_repo.list_docker_containers()
    assert len(rows) == 1
    assert rows[0].status == "running"

    # Tick 2: Container disappears
    client.list_containers.return_value = []
    result2 = await collector.run(ctx)
    assert result2.ok is True

    # Verify container marked missing
    rows = await targets_repo.list_docker_containers()
    assert len(rows) == 1
    assert rows[0].status == "missing"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "health_input,expected",
    [
        ("healthy", "healthy"),
        ("unhealthy", "unhealthy"),
        ("starting", "starting"),
        ("none", None),
        (None, None),
        ("weird", None),
    ],
)
async def test_healthcheck_normalization(
    health_input: str | None,
    expected: str | None,
) -> None:
    """Health.Status normalization."""
    log = structlog.get_logger().bind(test="healthcheck")  # pyright: ignore[reportArgumentType]
    result = DockerSocketCollector._normalize_healthcheck(health_input, log)  # pyright: ignore[reportPrivateUsage]
    assert result == expected


@pytest.mark.asyncio
async def test_tick_extracts_labels(repo: SqliteRepository) -> None:
    """entry['Labels'] flows into DB labels JSON."""
    in_memory_writer = MemoryRetainingMetricsWriter()

    client = AsyncMock(spec=DockerSocketClient)
    client.list_containers.return_value = [
        {
            "Id": "abc",
            "Names": ["/foo"],
            "Image": "img:1",
            "ImageID": "sha:x",
            "State": "running",
            "Status": "Up 1h",
            "Labels": {"homelab-monitor.probe": "http://x", "custom": "value"},
        }
    ]
    client.inspect_container.return_value = {
        "Id": "abc",
        "Name": "/foo",
        "Image": "img:1",
        "State": {"Status": "running", "ExitCode": 0},
        "RestartCount": 0,
        "HostConfig": {"NetworkMode": "bridge"},
    }

    http_client = AsyncMock()
    http_client.get.return_value.json = MagicMock(
        return_value={"data": {"resultType": "vector", "result": []}}
    )

    ctx = _ctx(in_memory_writer, repo, http_client)
    collector = DockerSocketCollector(client=client, vm_url="http://vm:8428")
    await collector.run(ctx)

    targets_repo = TargetsRepository(repo)
    rows = await targets_repo.list_docker_containers()
    assert len(rows) == 1
    assert rows[0].labels == {"homelab-monitor.probe": "http://x", "custom": "value"}


@pytest.mark.asyncio
async def test_tick_vm_merge_happy_path(repo: SqliteRepository) -> None:
    """VM responds with cpu/mem -> cached into DB."""
    in_memory_writer = MemoryRetainingMetricsWriter()

    client = AsyncMock(spec=DockerSocketClient)
    client.list_containers.return_value = [
        {
            "Id": "abc",
            "Names": ["/foo"],
            "Image": "img:1",
            "ImageID": "sha:x",
            "State": "running",
            "Status": "Up 1h",
            "Labels": {},
        }
    ]
    client.inspect_container.return_value = {
        "Id": "abc",
        "Name": "/foo",
        "Image": "img:1",
        "State": {"Status": "running", "ExitCode": 0},
        "RestartCount": 0,
        "HostConfig": {"NetworkMode": "bridge"},
    }

    # Mock VM response with cpu and mem
    http_client = AsyncMock()
    responses: list[dict[str, Any]] = [
        {
            "data": {
                "resultType": "vector",
                "result": [
                    {
                        "metric": {"name": "foo"},
                        "value": [1234, "1.5"],
                    }
                ],
            }
        },
        {
            "data": {
                "resultType": "vector",
                "result": [
                    {
                        "metric": {"name": "foo"},
                        "value": [1234, "128.0"],
                    }
                ],
            }
        },
        {
            "data": {
                "resultType": "vector",
                "result": [],
            }
        },
    ]
    http_client.get.return_value.json = MagicMock(side_effect=responses)

    ctx = _ctx(in_memory_writer, repo, http_client)
    collector = DockerSocketCollector(client=client, vm_url="http://vm:8428")
    result = await collector.run(ctx)
    assert result.ok is True

    targets_repo = TargetsRepository(repo)
    rows = await targets_repo.list_docker_containers()
    assert len(rows) == 1
    assert rows[0].cpu_pct_cached == EXPECTED_CPU_PCT
    assert rows[0].mem_mib_cached == EXPECTED_MEM_MIB


@pytest.mark.asyncio
async def test_tick_vm_unreachable_keeps_stale(repo: SqliteRepository) -> None:
    """VM query error -> tick still ok=True, cpu/mem stay NULL."""
    in_memory_writer = MemoryRetainingMetricsWriter()

    client = AsyncMock(spec=DockerSocketClient)
    client.list_containers.return_value = [
        {
            "Id": "abc",
            "Names": ["/foo"],
            "Image": "img:1",
            "ImageID": "sha:x",
            "State": "running",
            "Status": "Up 1h",
            "Labels": {},
        }
    ]
    client.inspect_container.return_value = {
        "Id": "abc",
        "Name": "/foo",
        "Image": "img:1",
        "State": {"Status": "running", "ExitCode": 0},
        "RestartCount": 0,
        "HostConfig": {"NetworkMode": "bridge"},
    }

    # Mock HTTP client that raises
    http_client = AsyncMock()
    http_client.get.side_effect = httpx.ConnectError("Connection refused")

    ctx = _ctx(in_memory_writer, repo, http_client)
    collector = DockerSocketCollector(client=client, vm_url="http://vm:8428")
    result = await collector.run(ctx)

    # Tick succeeds despite VM failure
    assert result.ok is True

    targets_repo = TargetsRepository(repo)
    rows = await targets_repo.list_docker_containers()
    assert len(rows) == 1
    assert rows[0].cpu_pct_cached is None
    assert rows[0].mem_mib_cached is None


@pytest.mark.asyncio
async def test_tick_inspect_failure_skips_container(repo: SqliteRepository) -> None:
    """list returns [abc, def]; inspect(abc) fails; inspect(def) succeeds."""
    in_memory_writer = MemoryRetainingMetricsWriter()

    client = AsyncMock(spec=DockerSocketClient)
    client.list_containers.return_value = [
        {
            "Id": "abc",
            "Names": ["/foo"],
            "Image": "img:1",
            "ImageID": "sha:x",
            "State": "running",
            "Status": "Up 1h",
            "Labels": {},
        },
        {
            "Id": "def",
            "Names": ["/bar"],
            "Image": "img:2",
            "ImageID": "sha:y",
            "State": "running",
            "Status": "Up 2h",
            "Labels": {},
        },
    ]

    # First inspect fails, second succeeds
    client.inspect_container.side_effect = [
        DockerSocketProtocolError("malformed response"),
        {
            "Id": "def",
            "Name": "/bar",
            "Image": "img:2",
            "State": {"Status": "running", "ExitCode": 0},
            "RestartCount": 0,
            "HostConfig": {"NetworkMode": "bridge"},
        },
    ]

    http_client = AsyncMock()
    http_client.get.return_value.json = MagicMock(
        return_value={"data": {"resultType": "vector", "result": []}}
    )

    ctx = _ctx(in_memory_writer, repo, http_client)
    collector = DockerSocketCollector(client=client, vm_url="http://vm:8428")
    result = await collector.run(ctx)

    # ok=False because abc failed
    assert result.ok is False
    assert any("inspect_failed" in e for e in result.errors)

    # But def still in DB
    targets_repo = TargetsRepository(repo)
    rows = await targets_repo.list_docker_containers()
    assert len(rows) == 1
    assert rows[0].name == "bar"


@pytest.mark.asyncio
async def test_tick_socket_unreachable_returns_failure(repo: SqliteRepository) -> None:
    """client.list_containers raises DockerSocketConnectionError."""
    in_memory_writer = MemoryRetainingMetricsWriter()

    client = AsyncMock(spec=DockerSocketClient)
    client.list_containers.side_effect = DockerSocketConnectionError(
        "socket missing: /var/run/docker.sock"
    )

    http_client = AsyncMock()

    ctx = _ctx(in_memory_writer, repo, http_client)
    collector = DockerSocketCollector(client=client, vm_url="http://vm:8428")
    result = await collector.run(ctx)

    assert result.ok is False
    assert any("list_failed" in e for e in result.errors)

    # No rows should be in DB
    targets_repo = TargetsRepository(repo)
    rows = await targets_repo.list_docker_containers()
    assert len(rows) == 0


@pytest.mark.asyncio
async def test_tick_client_unconfigured_returns_failure(repo: SqliteRepository) -> None:
    """client=None -> immediate failure."""
    in_memory_writer = MemoryRetainingMetricsWriter()
    http_client = AsyncMock()

    ctx = _ctx(in_memory_writer, repo, http_client)
    collector = DockerSocketCollector(client=None, vm_url="http://vm:8428")
    result = await collector.run(ctx)

    assert result.ok is False
    assert "client_unconfigured" in result.errors


@pytest.mark.asyncio
async def test_tick_container_with_healthcheck_emits_4_metrics(repo: SqliteRepository) -> None:
    """Container with healthcheck yields 4 metrics (status, restart, exit, hc)."""
    in_memory_writer = MemoryRetainingMetricsWriter()

    client = AsyncMock(spec=DockerSocketClient)
    client.list_containers.return_value = [
        {
            "Id": "abc123def456",
            "Names": ["/healthy-container"],
            "Image": "img:1.0",
            "ImageID": "sha:xxxx",
            "State": "running",
            "Status": "Up 1h",
            "Labels": {},
        }
    ]
    client.inspect_container.return_value = {
        "Id": "abc123def456",
        "Name": "/healthy-container",
        "Image": "img:1.0",
        "State": {
            "Status": "running",
            "Running": True,
            "ExitCode": 0,
            "Health": {
                "Status": "healthy",
            },
        },
        "RestartCount": 0,
        "HostConfig": {"NetworkMode": "bridge"},
    }

    http_client = AsyncMock()
    http_client.get.return_value.json = MagicMock(
        return_value={"data": {"resultType": "vector", "result": []}}
    )

    ctx = _ctx(in_memory_writer, repo, http_client)
    collector = DockerSocketCollector(client=client, vm_url="http://vm:8428")
    result = await collector.run(ctx)

    assert result.ok is True
    assert result.metrics_emitted == EXPECTED_HC_METRIC_COUNT


def test_strip_name_no_leading_slash() -> None:
    """_strip_name('mycontainer') == 'mycontainer' (no slash to remove)."""
    result = DockerSocketCollector._strip_name("mycontainer")  # pyright: ignore[reportPrivateUsage]
    assert result == "mycontainer"


def test_strip_name_nested_path_preserved_after_first_slash() -> None:
    """_strip_name only strips the leading slash, preserves nested paths."""
    result = DockerSocketCollector._strip_name("/foo/bar")  # pyright: ignore[reportPrivateUsage]
    assert result == "foo/bar"


def test_extract_state_non_string_status_returns_unknown() -> None:
    """_extract_state with Status=None -> 'unknown'."""
    result = DockerSocketCollector._extract_state({"State": {"Status": None}})  # pyright: ignore[reportPrivateUsage, reportArgumentType]
    assert result == "unknown"


@pytest.mark.asyncio
async def test_query_vm_cpu_mem_no_vm_url_returns_empty(repo: SqliteRepository) -> None:
    """vm_url=None -> returns {}."""
    in_memory_writer = MemoryRetainingMetricsWriter()
    http_client = AsyncMock()

    ctx = _ctx(in_memory_writer, repo, http_client)
    collector = DockerSocketCollector(client=AsyncMock(spec=DockerSocketClient), vm_url=None)

    result = await collector._query_vm_cpu_mem(ctx, ["test-container"])  # pyright: ignore[reportPrivateUsage]
    assert result == {}


@pytest.mark.asyncio
async def test_query_vm_cpu_mem_merges_cpu_and_mem(repo: SqliteRepository) -> None:
    """VM returns cpu then mem -> merged into (1.5, 128.0)."""
    in_memory_writer = MemoryRetainingMetricsWriter()

    http_client = AsyncMock()
    responses = [
        {
            "data": {
                "resultType": "vector",
                "result": [
                    {
                        "metric": {"name": "test-container"},
                        "value": [1234, "1.5"],
                    }
                ],
            }
        },
        {
            "data": {
                "resultType": "vector",
                "result": [
                    {
                        "metric": {"name": "test-container"},
                        "value": [1234, "128.0"],
                    }
                ],
            }
        },
    ]
    http_client.get.return_value.json = MagicMock(side_effect=responses)

    ctx = _ctx(in_memory_writer, repo, http_client)
    collector = DockerSocketCollector(
        client=AsyncMock(spec=DockerSocketClient), vm_url="http://vm:8428"
    )

    result = await collector._query_vm_cpu_mem(ctx, ["test-container"])  # pyright: ignore[reportPrivateUsage]
    assert result == {"test-container": (1.5, 128.0)}


@pytest.mark.asyncio
async def test_query_prometheus_non_vector_result_returns_empty(repo: SqliteRepository) -> None:
    """Prometheus with resultType='matrix' -> returns []."""
    in_memory_writer = MemoryRetainingMetricsWriter()

    http_client = AsyncMock()
    http_client.get.return_value.json = MagicMock(
        return_value={"data": {"resultType": "matrix", "result": []}}
    )

    ctx = _ctx(in_memory_writer, repo, http_client)
    collector = DockerSocketCollector(
        client=AsyncMock(spec=DockerSocketClient), vm_url="http://vm:8428"
    )

    result = await collector._query_prometheus(ctx, "cpu_query")  # pyright: ignore[reportPrivateUsage]
    assert result == []


@pytest.mark.asyncio
async def test_query_prometheus_no_vm_url_returns_empty(repo: SqliteRepository) -> None:
    """_query_prometheus with vm_url=None returns [] immediately."""
    in_memory_writer = MemoryRetainingMetricsWriter()

    http_client = AsyncMock()

    ctx = _ctx(in_memory_writer, repo, http_client)
    collector = DockerSocketCollector(client=AsyncMock(spec=DockerSocketClient), vm_url=None)

    result = await collector._query_prometheus(ctx, "cpu_query")  # pyright: ignore[reportPrivateUsage]
    assert result == []
    # Verify http client was never called
    http_client.get.assert_not_called()


@pytest.mark.asyncio
async def test_query_vm_cpu_mem_empty_url_returns_empty(repo: SqliteRepository) -> None:
    """_query_vm_cpu_mem returns {} when vm_url is empty string."""
    writer = MemoryRetainingMetricsWriter()
    http_client = AsyncMock(spec=httpx.AsyncClient)
    client = AsyncMock(spec=DockerSocketClient)
    collector = DockerSocketCollector(client=client, vm_url="")  # empty string also falsy
    ctx = _ctx(writer, repo, http_client)

    result = await collector._query_vm_cpu_mem(ctx, ["mycontainer"])  # pyright: ignore[reportPrivateUsage]
    assert result == {}


@pytest.mark.asyncio
async def test_query_vm_cpu_mem_cpu_entry_missing_name_skipped(repo: SqliteRepository) -> None:
    """When CPU entry has no 'name' label, it is silently skipped (guard False branch)."""
    writer = MemoryRetainingMetricsWriter()
    client = AsyncMock(spec=DockerSocketClient)

    cpu_response = MagicMock()
    cpu_response.raise_for_status = MagicMock()
    cpu_response.json = MagicMock(
        return_value={
            "data": {"resultType": "vector", "result": [{"metric": {}, "value": [0, "12.5"]}]}
        }
    )
    mem_response = MagicMock()
    mem_response.raise_for_status = MagicMock()
    mem_response.json = MagicMock(return_value={"data": {"resultType": "vector", "result": []}})

    http_client = AsyncMock(spec=httpx.AsyncClient)
    http_client.get.side_effect = [cpu_response, mem_response]

    collector = DockerSocketCollector(client=client, vm_url="http://vm:8428")
    ctx = _ctx(writer, repo, http_client)

    result = await collector._query_vm_cpu_mem(ctx, ["mycontainer"])  # pyright: ignore[reportPrivateUsage]
    assert result == {}


@pytest.mark.asyncio
async def test_query_vm_cpu_mem_mem_only_uses_else_branch(repo: SqliteRepository) -> None:
    """When mem result has a name not in cpu result, mem-only tuple is created."""
    writer = MemoryRetainingMetricsWriter()
    client = AsyncMock(spec=DockerSocketClient)

    cpu_response = MagicMock()
    cpu_response.raise_for_status = MagicMock()
    cpu_response.json = MagicMock(return_value={"data": {"resultType": "vector", "result": []}})
    mem_response = MagicMock()
    mem_response.raise_for_status = MagicMock()
    mem_response.json = MagicMock(
        return_value={
            "data": {
                "resultType": "vector",
                "result": [{"metric": {"name": "x"}, "value": [0, "256.0"]}],
            }
        }
    )

    http_client = AsyncMock(spec=httpx.AsyncClient)
    http_client.get.side_effect = [cpu_response, mem_response]

    collector = DockerSocketCollector(client=client, vm_url="http://vm:8428")
    ctx = _ctx(writer, repo, http_client)

    result = await collector._query_vm_cpu_mem(ctx, ["x"])  # pyright: ignore[reportPrivateUsage]
    assert result == {"x": (None, 256.0)}


@pytest.mark.asyncio
async def test_query_vm_cpu_mem_mem_non_float_swallowed(repo: SqliteRepository) -> None:
    """Memory value that can't be float'd is silently dropped (contextlib.suppress branch)."""
    writer = MemoryRetainingMetricsWriter()
    client = AsyncMock(spec=DockerSocketClient)

    cpu_response = MagicMock()
    cpu_response.raise_for_status = MagicMock()
    cpu_response.json = MagicMock(return_value={"data": {"resultType": "vector", "result": []}})
    mem_response = MagicMock()
    mem_response.raise_for_status = MagicMock()
    mem_response.json = MagicMock(
        return_value={
            "data": {
                "resultType": "vector",
                "result": [{"metric": {"name": "x"}, "value": [0, "not-a-number"]}],
            }
        }
    )

    http_client = AsyncMock(spec=httpx.AsyncClient)
    http_client.get.side_effect = [cpu_response, mem_response]

    collector = DockerSocketCollector(client=client, vm_url="http://vm:8428")
    ctx = _ctx(writer, repo, http_client)

    result = await collector._query_vm_cpu_mem(ctx, ["x"])  # pyright: ignore[reportPrivateUsage]
    assert result == {}


@pytest.mark.asyncio
async def test_query_vm_cpu_mem_mem_none_value_skipped(repo: SqliteRepository) -> None:
    """Memory entry with None as value_str is silently skipped (guard False)."""
    writer = MemoryRetainingMetricsWriter()
    client = AsyncMock(spec=DockerSocketClient)

    cpu_response = MagicMock()
    cpu_response.raise_for_status = MagicMock()
    cpu_response.json = MagicMock(return_value={"data": {"resultType": "vector", "result": []}})
    mem_response = MagicMock()
    mem_response.raise_for_status = MagicMock()
    mem_response.json = MagicMock(
        return_value={
            "data": {
                "resultType": "vector",
                "result": [{"metric": {"name": "x"}, "value": [0, None]}],
            }
        }
    )

    http_client = AsyncMock(spec=httpx.AsyncClient)
    http_client.get.side_effect = [cpu_response, mem_response]

    collector = DockerSocketCollector(client=client, vm_url="http://vm:8428")
    ctx = _ctx(writer, repo, http_client)

    result = await collector._query_vm_cpu_mem(ctx, ["x"])  # pyright: ignore[reportPrivateUsage]
    assert result == {}


@pytest.mark.asyncio
async def test_query_vm_restart_count_24h_no_vm_url_returns_empty(repo: SqliteRepository) -> None:
    """vm_url=None -> _query_vm_restart_count_24h returns {} immediately."""
    writer = MemoryRetainingMetricsWriter()
    http_client = AsyncMock(spec=httpx.AsyncClient)
    collector = DockerSocketCollector(client=AsyncMock(spec=DockerSocketClient), vm_url=None)
    ctx = _ctx(writer, repo, http_client)

    result = await collector._query_vm_restart_count_24h(ctx)  # pyright: ignore[reportPrivateUsage]
    assert result == {}
    http_client.get.assert_not_called()


@pytest.mark.asyncio
async def test_query_vm_restart_count_24h_returns_parsed_counts(repo: SqliteRepository) -> None:
    """VM returns restart count entry -> parsed as int."""
    writer = MemoryRetainingMetricsWriter()
    client = AsyncMock(spec=DockerSocketClient)

    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json = MagicMock(
        return_value={
            "data": {
                "resultType": "vector",
                "result": [{"metric": {"name": "foo"}, "value": [0, "3.0"]}],
            }
        }
    )

    http_client = AsyncMock(spec=httpx.AsyncClient)
    http_client.get.return_value = response

    collector = DockerSocketCollector(client=client, vm_url="http://vm:8428")
    ctx = _ctx(writer, repo, http_client)

    result = await collector._query_vm_restart_count_24h(ctx)  # pyright: ignore[reportPrivateUsage]
    assert result == {"foo": 3}


@pytest.mark.asyncio
async def test_query_vm_restart_count_24h_skips_malformed_entries(
    repo: SqliteRepository,
) -> None:
    """Entries missing 'name' label or 'value' field are skipped (defensive branch)."""
    writer = MemoryRetainingMetricsWriter()
    client = AsyncMock(spec=DockerSocketClient)

    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json = MagicMock(
        return_value={
            "data": {
                "resultType": "vector",
                "result": [
                    # Missing 'name' label → branch (366→361) taken
                    {"metric": {}, "value": [0, "5.0"]},
                    # Missing 'value' field → also skipped
                    {"metric": {"name": "no_value"}, "value": [0, None]},
                    # Valid entry to confirm the function still parses others
                    {"metric": {"name": "valid"}, "value": [0, "7.0"]},
                ],
            }
        }
    )

    http_client = AsyncMock(spec=httpx.AsyncClient)
    http_client.get.return_value = response

    collector = DockerSocketCollector(client=client, vm_url="http://vm:8428")
    ctx = _ctx(writer, repo, http_client)

    result = await collector._query_vm_restart_count_24h(ctx)  # pyright: ignore[reportPrivateUsage]
    assert result == {"valid": 7}
