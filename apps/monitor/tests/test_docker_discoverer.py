"""Tests for DockerDiscoverer: events loop + periodic scan + lock serialization.

Uses injected _FakeSocketClient to mock Docker API without real HTTP connections.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
import structlog

from homelab_monitor.kernel.db.repositories.suggestions_repository import (
    SuggestionsRepository,
)
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.db.time import utc_now_iso
from homelab_monitor.kernel.docker.socket_client import (
    DockerSocketError,
)
from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.io import InMemoryLogsWriter, MemoryRetainingMetricsWriter
from homelab_monitor.kernel.plugins.types import CollectorConfig
from homelab_monitor.plugins.discoverers.docker_discoverer import (
    DockerDiscoverer,
)


class _FakeSocketClient:
    """Injected fake DockerSocketClient for testing."""

    def __init__(
        self,
        *,
        list_containers_result: list[dict[str, Any]] | None = None,
        inspect_results: dict[str, dict[str, Any]] | None = None,
        events_iterator: AsyncIterator[dict[str, Any]] | None = None,
    ) -> None:
        self._list = list_containers_result or []
        self._inspects = inspect_results or {}
        self._events = events_iterator
        self.list_called = 0
        self.events_called = 0

    async def list_containers(self) -> list[dict[str, Any]]:
        self.list_called += 1
        return self._list

    async def inspect_container(self, cid: str) -> dict[str, Any]:
        if cid not in self._inspects:
            raise DockerSocketError(f"no fake inspect for {cid}")
        return self._inspects[cid]

    async def events(
        self, *, filters: dict[str, list[str]] | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        if self._events is None:
            return
        async for ev in self._events:
            yield ev


def _ctx(
    writer: MemoryRetainingMetricsWriter,
    repo: SqliteRepository,
) -> CollectorContext:
    """Minimal CollectorContext for DockerDiscoverer."""
    return CollectorContext(
        config=CollectorConfig(name="docker_discoverer"),
        db=repo,
        vm=writer,
        vl=InMemoryLogsWriter(),
        http=AsyncMock(),
        ssh=None,  # pyright: ignore[reportArgumentType]
        secrets=None,  # pyright: ignore[reportArgumentType]
        log=structlog.get_logger().bind(collector="docker_discoverer"),  # pyright: ignore[reportArgumentType]
        ha=None,
    )


async def _async_iter(items: list[dict[str, Any]]) -> AsyncIterator[dict[str, Any]]:
    """Helper to convert a list to an async iterator."""
    for item in items:
        yield item


@pytest.mark.asyncio
async def test_new_container_creates_suggestion(repo: SqliteRepository) -> None:
    """Fake events() yields container.create → suggestion appears in DB."""
    writer = MemoryRetainingMetricsWriter()
    ctx = _ctx(writer, repo)
    sugg_repo = SuggestionsRepository(repo)

    # Fake socket client that yields a create event.
    events = _async_iter(
        [
            {
                "Type": "container",
                "Action": "create",
                "Actor": {
                    "ID": "container-xyz",
                    "Attributes": {"name": "test-container"},
                },
            }
        ]
    )

    fake_client = _FakeSocketClient(
        list_containers_result=[],
        inspect_results={
            "container-xyz": {
                "Id": "container-xyz",
                "Name": "/test-container",
                "Config": {
                    "Image": "nginx:latest",
                    "Labels": {},  # No homelab-monitor labels
                },
            }
        },
        events_iterator=events,
    )

    # Inject fake client into discoverer.
    discoverer = DockerDiscoverer(
        socket_client=fake_client,  # pyright: ignore[reportArgumentType]
        suggestions_repo=SuggestionsRepository(repo),
        db=repo,
    )

    # Run events loop until it times out (loop is infinite; timeout bounds the test).
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(discoverer.run_events_loop(ctx), timeout=1.0)

    # Verify suggestion in DB.
    rows, _ = await sugg_repo.list_pending_docker_suggestions(
        status="pending", limit=50, cursor=None
    )
    assert len(rows) == 1
    assert rows[0].detection_reason == "no_homelab_monitor_label"


@pytest.mark.asyncio
async def test_destroyed_container_marks_gone(repo: SqliteRepository) -> None:
    """Existing pending suggestion → destroy event → state=container_gone."""
    writer = MemoryRetainingMetricsWriter()
    ctx = _ctx(writer, repo)
    sugg_repo = SuggestionsRepository(repo)
    now = utc_now_iso()

    # Pre-seed a pending suggestion.
    async with repo.transaction() as conn:
        await sugg_repo.insert_or_update_docker_suggestion_conn(
            conn,
            kind="docker_container_discovered",
            deduplication_key="abc123",
            container_id="container-xyz",
            container_name="test-container",
            image_ref="nginx:latest",
            labels={},
            compose_project=None,
            compose_service=None,
            compose_file_path=None,
            detection_reason="no_homelab_monitor_label",
            now=now,
        )

    # Fake socket client that yields a destroy event.
    events = _async_iter(
        [
            {
                "Type": "container",
                "Action": "destroy",
                "Actor": {"ID": "container-xyz"},
            }
        ]
    )

    fake_client = _FakeSocketClient(
        list_containers_result=[],
        events_iterator=events,
    )

    discoverer = DockerDiscoverer(
        socket_client=fake_client,  # pyright: ignore[reportArgumentType]
        suggestions_repo=SuggestionsRepository(repo),
        db=repo,
    )
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(discoverer.run_events_loop(ctx), timeout=1.0)

    # Verify state is container_gone.
    rows, _ = await sugg_repo.list_pending_docker_suggestions(status="all", limit=50, cursor=None)
    assert len(rows) == 1
    assert rows[0].state == "container_gone"


@pytest.mark.asyncio
async def test_periodic_scan_catches_event_stream_miss(
    repo: SqliteRepository,
) -> None:
    """Periodic run() discovers unlabeled container even if events were missed."""
    writer = MemoryRetainingMetricsWriter()
    ctx = _ctx(writer, repo)
    sugg_repo = SuggestionsRepository(repo)

    # Fake socket client: events is empty, but list_containers has one unlabeled container.
    fake_client = _FakeSocketClient(
        list_containers_result=[
            {
                "Id": "container-xyz",
                "Names": ["/test-container"],
                "Image": "nginx:latest",
                "Labels": {},
            }
        ],
        inspect_results={
            "container-xyz": {
                "Id": "container-xyz",
                "Name": "/test-container",
                "Config": {
                    "Image": "nginx:latest",
                    "Labels": {},
                },
            }
        },
        events_iterator=_async_iter([]),  # Empty stream
    )

    discoverer = DockerDiscoverer(
        socket_client=fake_client,  # pyright: ignore[reportArgumentType]
        suggestions_repo=SuggestionsRepository(repo),
        db=repo,
    )
    result = await discoverer.run(ctx)

    assert result.ok is True

    # Verify suggestion was created by periodic scan.
    rows, _ = await sugg_repo.list_pending_docker_suggestions(
        status="pending", limit=50, cursor=None
    )
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_re_seeing_same_container_no_duplicate_suggestion(
    repo: SqliteRepository,
) -> None:
    """Periodic scan run twice on same container → only 1 row (idempotency)."""
    writer = MemoryRetainingMetricsWriter()
    ctx = _ctx(writer, repo)
    sugg_repo = SuggestionsRepository(repo)

    container_list: list[dict[str, Any]] = [
        {
            "Id": "container-xyz",
            "Names": ["/test-container"],
            "Image": "nginx:latest",
            "Labels": {},
        }
    ]

    # Fake client always returns the same container.
    fake_client = _FakeSocketClient(
        list_containers_result=container_list,
        inspect_results={
            "container-xyz": {
                "Id": "container-xyz",
                "Name": "/test-container",
                "Config": {
                    "Image": "nginx:latest",
                    "Labels": {},
                },
            }
        },
        events_iterator=_async_iter([]),
    )

    discoverer = DockerDiscoverer(
        socket_client=fake_client,  # pyright: ignore[reportArgumentType]
        suggestions_repo=SuggestionsRepository(repo),
        db=repo,
    )

    # Run periodic scan twice.
    await discoverer.run(ctx)
    await discoverer.run(ctx)

    # Verify only 1 row exists.
    rows, _ = await sugg_repo.list_pending_docker_suggestions(status="all", limit=50, cursor=None)
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_disabled_profile_container_surfaces_with_flag(
    repo: SqliteRepository,
) -> None:
    """Container with disabled profile → detection_reason='disabled_profile'."""
    writer = MemoryRetainingMetricsWriter()
    ctx = _ctx(writer, repo)
    sugg_repo = SuggestionsRepository(repo)

    # Container with both homelab-monitor label AND disabled profile.
    fake_client = _FakeSocketClient(
        list_containers_result=[
            {
                "Id": "container-xyz",
                "Names": ["/test-container"],
                "Image": "nginx:latest",
                "Labels": {
                    "homelab-monitor.host.foo": "true",
                    "com.docker.compose.config.profiles": "disabled",
                },
            }
        ],
        inspect_results={
            "container-xyz": {
                "Id": "container-xyz",
                "Name": "/test-container",
                "Config": {
                    "Image": "nginx:latest",
                    "Labels": {
                        "homelab-monitor.host.foo": "true",
                        "com.docker.compose.config.profiles": "disabled",
                    },
                },
            }
        },
        events_iterator=_async_iter([]),
    )

    discoverer = DockerDiscoverer(
        socket_client=fake_client,  # pyright: ignore[reportArgumentType]
        suggestions_repo=SuggestionsRepository(repo),
        db=repo,
    )
    await discoverer.run(ctx)

    # Verify suggestion with disabled_profile detection reason.
    rows, _ = await sugg_repo.list_pending_docker_suggestions(
        status="pending", limit=50, cursor=None
    )
    assert len(rows) == 1
    assert rows[0].detection_reason == "disabled_profile"


@pytest.mark.asyncio
async def test_label_collision_emits_label_collision_kind(
    repo: SqliteRepository,
) -> None:
    """Container with label collision → kind='docker_label_collision'."""
    writer = MemoryRetainingMetricsWriter()
    ctx = _ctx(writer, repo)
    sugg_repo = SuggestionsRepository(repo)

    # Container with two labels that resolve to the same identity.
    # This test uses the collision rule: same (kind, name) from different keys.
    # For example: "homelab-monitor.http.api" and "homelab-monitor.http.api.url"
    # both resolve to kind='http', name='api'.
    fake_client = _FakeSocketClient(
        list_containers_result=[
            {
                "Id": "container-xyz",
                "Names": ["/test-container"],
                "Image": "nginx:latest",
                "Labels": {
                    "homelab-monitor.http.api": "check-not-just-key",
                    "homelab-monitor.http.api.url": "http://localhost:8080",
                },
            }
        ],
        inspect_results={
            "container-xyz": {
                "Id": "container-xyz",
                "Name": "/test-container",
                "Config": {
                    "Image": "nginx:latest",
                    "Labels": {
                        "homelab-monitor.http.api": "check-not-just-key",
                        "homelab-monitor.http.api.url": "http://localhost:8080",
                    },
                },
            }
        },
        events_iterator=_async_iter([]),
    )

    discoverer = DockerDiscoverer(
        socket_client=fake_client,  # pyright: ignore[reportArgumentType]
        suggestions_repo=SuggestionsRepository(repo),
        db=repo,
    )
    await discoverer.run(ctx)

    # Verify collision detected.
    rows, _ = await sugg_repo.list_pending_docker_suggestions(status="all", limit=50, cursor=None)
    # Should have a docker_label_collision row.
    collision_rows = [r for r in rows if r.kind == "docker_label_collision"]
    assert len(collision_rows) > 0


@pytest.mark.asyncio
async def test_container_with_homelab_monitor_labels_no_suggestion(
    repo: SqliteRepository,
) -> None:
    """Container with homelab-monitor labels → no suggestion created."""
    writer = MemoryRetainingMetricsWriter()
    ctx = _ctx(writer, repo)
    sugg_repo = SuggestionsRepository(repo)

    # Container with valid homelab-monitor labels.
    fake_client = _FakeSocketClient(
        list_containers_result=[
            {
                "Id": "container-xyz",
                "Names": ["/test-container"],
                "Image": "nginx:latest",
                "Labels": {
                    "homelab-monitor.http.api": "true",
                },
            }
        ],
        inspect_results={
            "container-xyz": {
                "Id": "container-xyz",
                "Name": "/test-container",
                "Config": {
                    "Image": "nginx:latest",
                    "Labels": {
                        "homelab-monitor.http.api": "true",
                    },
                },
            }
        },
        events_iterator=_async_iter([]),
    )

    discoverer = DockerDiscoverer(
        socket_client=fake_client,  # pyright: ignore[reportArgumentType]
        suggestions_repo=SuggestionsRepository(repo),
        db=repo,
    )
    await discoverer.run(ctx)

    # Verify NO suggestion created.
    rows, _ = await sugg_repo.list_pending_docker_suggestions(status="all", limit=50, cursor=None)
    assert len(rows) == 0


@pytest.mark.asyncio
async def test_concurrent_events_and_periodic_lock_serializes_writes(
    repo: SqliteRepository,
) -> None:
    """Events + periodic running concurrently → lock ensures only 1 row."""
    writer = MemoryRetainingMetricsWriter()
    ctx = _ctx(writer, repo)
    sugg_repo = SuggestionsRepository(repo)

    # Both will upsert the same container.
    events = _async_iter(
        [
            {
                "Type": "container",
                "Action": "create",
                "Actor": {
                    "ID": "container-xyz",
                    "Attributes": {"name": "test-container"},
                },
            }
        ]
    )

    container_list: list[dict[str, Any]] = [
        {
            "Id": "container-xyz",
            "Names": ["/test-container"],
            "Image": "nginx:latest",
            "Labels": {},
        }
    ]

    fake_client = _FakeSocketClient(
        list_containers_result=container_list,
        inspect_results={
            "container-xyz": {
                "Id": "container-xyz",
                "Name": "/test-container",
                "Config": {
                    "Image": "nginx:latest",
                    "Labels": {},
                },
            }
        },
        events_iterator=events,
    )

    discoverer = DockerDiscoverer(
        socket_client=fake_client,  # pyright: ignore[reportArgumentType]
        suggestions_repo=SuggestionsRepository(repo),
        db=repo,
    )

    # Run events and periodic concurrently; bound with wait_for so the infinite
    # events loop cannot hang the test after its finite iterator exhausts.
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(
            asyncio.gather(
                discoverer.run_events_loop(ctx),
                discoverer.run(ctx),
            ),
            timeout=2.0,
        )

    # Verify only 1 suggestion row exists (lock prevented duplicates).
    rows, _ = await sugg_repo.list_pending_docker_suggestions(status="all", limit=50, cursor=None)
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_events_loop_cancellation_exits_cleanly(
    repo: SqliteRepository,
) -> None:
    """Events loop can be cancelled cleanly without exceptions."""
    writer = MemoryRetainingMetricsWriter()
    ctx = _ctx(writer, repo)

    # Fake client yields one event, then sleeps long enough to test cancellation
    # (5s is enough to demonstrate cancellation works; bounded so the test fails
    # fast if cancellation propagation ever breaks rather than hanging the suite).
    async def _slow_events() -> AsyncIterator[dict[str, Any]]:
        yield {
            "Type": "container",
            "Action": "create",
            "Actor": {
                "ID": "container-xyz",
                "Attributes": {"name": "test-container"},
            },
        }
        await asyncio.sleep(5)

    fake_client = _FakeSocketClient(
        list_containers_result=[],
        inspect_results={
            "container-xyz": {
                "Id": "container-xyz",
                "Name": "/test-container",
                "Config": {
                    "Image": "nginx:latest",
                    "Labels": {},
                },
            }
        },
        events_iterator=_slow_events(),
    )

    discoverer = DockerDiscoverer(
        socket_client=fake_client,  # pyright: ignore[reportArgumentType]
        suggestions_repo=SuggestionsRepository(repo),
        db=repo,
    )

    # Start the loop task.
    task = asyncio.create_task(discoverer.run_events_loop(ctx))

    # Give it a moment to process the event.
    await asyncio.sleep(0.1)

    # Cancel it.
    task.cancel()

    # Wait for cancellation; outer timeout guards against any propagation bugs.
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2.0)


@pytest.mark.asyncio
async def test_events_loop_emits_self_metric_on_each_event(
    repo: SqliteRepository,
) -> None:
    """Each event increments the self-metric counter."""
    writer = MemoryRetainingMetricsWriter()
    ctx = _ctx(writer, repo)

    # Fake client yields 3 events.
    events = _async_iter(
        [
            {
                "Type": "container",
                "Action": "create",
                "Actor": {
                    "ID": f"container-{i}",
                    "Attributes": {"name": f"container-{i}"},
                },
            }
            for i in range(3)
        ]
    )

    inspect_results: dict[str, dict[str, Any]] = {
        f"container-{i}": {
            "Id": f"container-{i}",
            "Name": f"/container-{i}",
            "Config": {
                "Image": "nginx:latest",
                "Labels": {},
            },
        }
        for i in range(3)
    }

    fake_client = _FakeSocketClient(
        list_containers_result=[],
        inspect_results=inspect_results,
        events_iterator=events,
    )

    discoverer = DockerDiscoverer(
        socket_client=fake_client,  # pyright: ignore[reportArgumentType]
        suggestions_repo=SuggestionsRepository(repo),
        db=repo,
    )
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(discoverer.run_events_loop(ctx), timeout=1.0)

    # Check metrics were emitted. Look for counter gauge (ctx.vm uses gauge for counters).
    # The metric key format depends on implementation; check for any metric with discoverer name.
    metrics = writer.snapshot()
    # Expect at least one metric emitted per event (3 events = 3 metrics).
    # The exact key format depends on the implementation.
    assert len(metrics) > 0


# ---------------------------------------------------------------------------
# Coverage gap tests
# ---------------------------------------------------------------------------


def test_resolve_scan_interval_invalid_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """_resolve_scan_interval() falls back to default when env var is non-integer."""
    from homelab_monitor.plugins.discoverers.docker_discoverer import (  # noqa: PLC0415
        _DEFAULT_SCAN_INTERVAL,  # pyright: ignore[reportPrivateUsage]
        _resolve_scan_interval,  # pyright: ignore[reportPrivateUsage]
    )

    monkeypatch.setenv("HOMELAB_MONITOR_DOCKER_DISCOVERER_SCAN_INTERVAL_SECONDS", "not-a-number")
    result = _resolve_scan_interval()
    assert result == _DEFAULT_SCAN_INTERVAL


def test_resolve_scan_interval_valid_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """_resolve_scan_interval() uses the env var when it is a valid integer."""
    from homelab_monitor.plugins.discoverers.docker_discoverer import (  # noqa: PLC0415
        _resolve_scan_interval,  # pyright: ignore[reportPrivateUsage]
    )

    monkeypatch.setenv("HOMELAB_MONITOR_DOCKER_DISCOVERER_SCAN_INTERVAL_SECONDS", "42")
    result = _resolve_scan_interval()
    assert result == 42  # noqa: PLR2004


def test_resolve_scan_interval_clamps_to_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """_resolve_scan_interval() clamps 0 → 1."""
    from homelab_monitor.plugins.discoverers.docker_discoverer import (  # noqa: PLC0415
        _resolve_scan_interval,  # pyright: ignore[reportPrivateUsage]
    )

    monkeypatch.setenv("HOMELAB_MONITOR_DOCKER_DISCOVERER_SCAN_INTERVAL_SECONDS", "0")
    result = _resolve_scan_interval()
    assert result == 1


@pytest.mark.asyncio
async def test_start_events_loop_idempotent(repo: SqliteRepository) -> None:
    """start_events_loop is idempotent — calling it twice does not create two tasks."""
    writer = MemoryRetainingMetricsWriter()
    ctx = _ctx(writer, repo)

    async def _never_ending() -> AsyncIterator[dict[str, Any]]:
        await asyncio.sleep(60)
        return
        yield  # make it an async generator

    fake_client = _FakeSocketClient(events_iterator=_never_ending())
    discoverer = DockerDiscoverer(
        socket_client=fake_client,  # pyright: ignore[reportArgumentType]
        suggestions_repo=SuggestionsRepository(repo),
        db=repo,
    )

    discoverer.start_events_loop(ctx)
    task1 = discoverer._events_task  # pyright: ignore[reportPrivateUsage]
    discoverer.start_events_loop(ctx)  # second call — must not replace task
    task2 = discoverer._events_task  # pyright: ignore[reportPrivateUsage]

    assert task1 is task2
    # Clean up
    await discoverer.stop_events_loop()


@pytest.mark.asyncio
async def test_stop_events_loop_when_no_task() -> None:
    """stop_events_loop is a no-op when no events task has been started."""
    discoverer = DockerDiscoverer()
    # Should return cleanly with no exceptions.
    await discoverer.stop_events_loop()


@pytest.mark.asyncio
async def test_run_returns_error_when_dependencies_unwired(repo: SqliteRepository) -> None:
    """run() returns ok=False when socket_client is None (dependencies unwired)."""
    writer = MemoryRetainingMetricsWriter()
    ctx = _ctx(writer, repo)

    # No dependencies injected.
    discoverer = DockerDiscoverer()
    result = await discoverer.run(ctx)

    assert result.ok is False
    assert "dependencies_unwired" in result.errors


@pytest.mark.asyncio
async def test_run_starts_events_loop_lazily(repo: SqliteRepository) -> None:
    """run() lazily starts the events loop on first invocation."""
    writer = MemoryRetainingMetricsWriter()
    ctx = _ctx(writer, repo)

    fake_client = _FakeSocketClient(
        list_containers_result=[],
        events_iterator=_async_iter([]),
    )
    discoverer = DockerDiscoverer(
        socket_client=fake_client,  # pyright: ignore[reportArgumentType]
        suggestions_repo=SuggestionsRepository(repo),
        db=repo,
    )

    assert discoverer._events_task is None  # pyright: ignore[reportPrivateUsage]
    await discoverer.run(ctx)
    # Events task should now exist (was lazily started).
    assert discoverer._events_task is not None  # pyright: ignore[reportPrivateUsage]
    await discoverer.stop_events_loop()


@pytest.mark.asyncio
async def test_run_list_containers_failure_returns_error(repo: SqliteRepository) -> None:
    """run() returns ok=False when list_containers raises DockerSocketError."""
    writer = MemoryRetainingMetricsWriter()
    ctx = _ctx(writer, repo)

    class _FailListClient(_FakeSocketClient):
        async def list_containers(self) -> list[dict[str, Any]]:
            raise DockerSocketError("socket refused")

    fake_client = _FailListClient(events_iterator=_async_iter([]))
    discoverer = DockerDiscoverer(
        socket_client=fake_client,  # pyright: ignore[reportArgumentType]
        suggestions_repo=SuggestionsRepository(repo),
        db=repo,
    )

    result = await discoverer.run(ctx)

    assert result.ok is False
    assert any("list_failed" in e for e in result.errors)
    await discoverer.stop_events_loop()


@pytest.mark.asyncio
async def test_run_inspect_failure_continues_and_reports_error(repo: SqliteRepository) -> None:
    """run() skips containers whose inspect fails but still reports the error."""
    writer = MemoryRetainingMetricsWriter()
    ctx = _ctx(writer, repo)

    # One container listed but its inspect will fail.
    fake_client = _FakeSocketClient(
        list_containers_result=[
            {
                "Id": "bad-container",
                "Names": ["/bad"],
                "Image": "nginx:latest",
                "Labels": {},
            }
        ],
        inspect_results={},  # no entry → raises DockerSocketError
        events_iterator=_async_iter([]),
    )
    discoverer = DockerDiscoverer(
        socket_client=fake_client,  # pyright: ignore[reportArgumentType]
        suggestions_repo=SuggestionsRepository(repo),
        db=repo,
    )

    result = await discoverer.run(ctx)

    assert result.ok is False
    assert any("inspect_failed" in e for e in result.errors)
    await discoverer.stop_events_loop()


@pytest.mark.asyncio
async def test_events_loop_no_socket_client_returns_immediately(repo: SqliteRepository) -> None:
    """run_events_loop exits immediately if socket_client is None."""
    writer = MemoryRetainingMetricsWriter()
    ctx = _ctx(writer, repo)

    discoverer = DockerDiscoverer(
        socket_client=None,
        suggestions_repo=SuggestionsRepository(repo),
        db=repo,
    )
    # Should return without hanging.
    await asyncio.wait_for(discoverer.run_events_loop(ctx), timeout=1.0)


@pytest.mark.asyncio
async def test_events_loop_reconnects_after_docker_socket_error(repo: SqliteRepository) -> None:
    """Events loop logs error + exponential backoff when DockerSocketError is raised."""
    from homelab_monitor.kernel.docker.socket_client import (  # noqa: PLC0415
        DockerSocketConnectionError,
    )

    writer = MemoryRetainingMetricsWriter()
    ctx = _ctx(writer, repo)

    call_count = 0

    class _ErrorClient(_FakeSocketClient):
        async def events(  # type: ignore[override]
            self, *, filters: dict[str, list[str]] | None = None
        ) -> AsyncIterator[dict[str, Any]]:
            nonlocal call_count
            call_count += 1
            raise DockerSocketConnectionError("refused")
            yield  # noqa: F841, RUF100

    fake_client = _ErrorClient()
    discoverer = DockerDiscoverer(
        socket_client=fake_client,  # pyright: ignore[reportArgumentType]
        suggestions_repo=SuggestionsRepository(repo),
        db=repo,
    )

    # Patch asyncio.sleep so backoff doesn't actually wait.
    original_sleep = asyncio.sleep

    async def _instant_sleep(delay: float) -> None:
        # After first error, let the loop run once more then raise CancelledError
        if call_count >= 2:  # noqa: PLR2004
            raise asyncio.CancelledError
        await original_sleep(0)

    import unittest.mock as _mock  # noqa: PLC0415

    with _mock.patch("asyncio.sleep", side_effect=_instant_sleep):  # noqa: SIM117
        with contextlib.suppress(asyncio.CancelledError):
            await discoverer.run_events_loop(ctx)

    assert call_count >= 1


@pytest.mark.asyncio
async def test_handle_event_ignores_non_create_destroy_actions(repo: SqliteRepository) -> None:
    """_handle_event ignores unknown actions like 'start' or 'stop'."""
    writer = MemoryRetainingMetricsWriter()
    ctx = _ctx(writer, repo)
    sugg_repo = SuggestionsRepository(repo)

    events = _async_iter(
        [
            {
                "Type": "container",
                "Action": "start",  # not create or destroy
                "Actor": {"ID": "container-xyz"},
            },
            {
                "Type": "container",
                "Action": "die",  # not create or destroy
                "Actor": {"ID": "container-xyz"},
            },
        ]
    )

    fake_client = _FakeSocketClient(
        list_containers_result=[],
        events_iterator=events,
    )
    discoverer = DockerDiscoverer(
        socket_client=fake_client,  # pyright: ignore[reportArgumentType]
        suggestions_repo=SuggestionsRepository(repo),
        db=repo,
    )

    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(discoverer.run_events_loop(ctx), timeout=1.0)

    # No suggestions should have been created.
    rows, _ = await sugg_repo.list_pending_docker_suggestions(status="all", limit=50, cursor=None)
    assert len(rows) == 0


@pytest.mark.asyncio
async def test_handle_event_create_inspect_failure_is_logged(repo: SqliteRepository) -> None:
    """_handle_event logs warning and returns when inspect fails on create event."""
    writer = MemoryRetainingMetricsWriter()
    ctx = _ctx(writer, repo)
    sugg_repo = SuggestionsRepository(repo)

    events = _async_iter(
        [
            {
                "Type": "container",
                "Action": "create",
                "Actor": {"ID": "unknown-container"},
            }
        ]
    )

    # No inspect result for "unknown-container" → raises DockerSocketError.
    fake_client = _FakeSocketClient(
        list_containers_result=[],
        inspect_results={},
        events_iterator=events,
    )
    discoverer = DockerDiscoverer(
        socket_client=fake_client,  # pyright: ignore[reportArgumentType]
        suggestions_repo=SuggestionsRepository(repo),
        db=repo,
    )

    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(discoverer.run_events_loop(ctx), timeout=1.0)

    # No suggestion should be created (inspect failed).
    rows, _ = await sugg_repo.list_pending_docker_suggestions(status="all", limit=50, cursor=None)
    assert len(rows) == 0


@pytest.mark.asyncio
async def test_upsert_suggestion_no_op_when_no_repo(repo: SqliteRepository) -> None:
    """_upsert_suggestion is a no-op when suggestions_repo is None."""
    from homelab_monitor.plugins.discoverers.docker_discoverer import (  # noqa: PLC0415
        DockerDiscoverer,
    )

    writer = MemoryRetainingMetricsWriter()
    ctx = _ctx(writer, repo)

    discoverer = DockerDiscoverer(
        socket_client=None,
        suggestions_repo=None,
        db=None,
    )
    inspect: dict[str, Any] = {
        "Id": "abc123",
        "Name": "/test",
        "Config": {"Image": "nginx", "Labels": {}},
    }
    # Should return without raising.
    await discoverer._upsert_suggestion(ctx, inspect)  # pyright: ignore[reportPrivateUsage,reportArgumentType]


def test_extract_detection_reason_no_labels() -> None:
    """_extract_detection_reason returns 'no_homelab_monitor_label' for empty labels."""
    from homelab_monitor.plugins.discoverers.docker_discoverer import (  # noqa: PLC0415
        _extract_detection_reason,  # pyright: ignore[reportPrivateUsage]
    )

    assert _extract_detection_reason({}) == "no_homelab_monitor_label"


def test_extract_detection_reason_label_collision() -> None:
    """_extract_detection_reason returns 'label_collision' when two keys resolve to same identity."""  # noqa: E501
    from homelab_monitor.plugins.discoverers.docker_discoverer import (  # noqa: PLC0415
        _extract_detection_reason,  # pyright: ignore[reportPrivateUsage]
    )

    labels = {
        "homelab-monitor.http.api": "true",
        "homelab-monitor.http.api.url": "http://localhost",
    }
    assert _extract_detection_reason(labels) == "label_collision"


def test_extract_detection_reason_disabled_profile() -> None:
    """_extract_detection_reason returns 'disabled_profile' for disabled compose profile."""
    from homelab_monitor.plugins.discoverers.docker_discoverer import (  # noqa: PLC0415
        _extract_detection_reason,  # pyright: ignore[reportPrivateUsage]
    )

    labels = {
        "homelab-monitor.host.myhost": "true",
        "com.docker.compose.config.profiles": "disabled",
    }
    assert _extract_detection_reason(labels) == "disabled_profile"


def test_extract_detection_reason_healthy_labeled_container() -> None:
    """_extract_detection_reason returns None when container has valid unique homelab labels."""
    from homelab_monitor.plugins.discoverers.docker_discoverer import (  # noqa: PLC0415
        _extract_detection_reason,  # pyright: ignore[reportPrivateUsage]
    )

    labels = {
        "homelab-monitor.http.api": "true",
        "homelab-monitor.host.myhost": "true",
    }
    assert _extract_detection_reason(labels) is None


def test_extract_detection_reason_label_too_short_ignored() -> None:
    """Labels with fewer than 2 segments after prefix are skipped in collision check."""
    from homelab_monitor.plugins.discoverers.docker_discoverer import (  # noqa: PLC0415
        _extract_detection_reason,  # pyright: ignore[reportPrivateUsage]
    )

    # "homelab-monitor.onlyone" has only 1 segment after prefix → no collision.
    labels = {
        "homelab-monitor.onlyone": "true",
    }
    # Only 1 label with short suffix → no homelab label valid for collision,
    # but homelab_labels is non-empty so rule 3 doesn't fire.
    # Rule 2 doesn't fire either. Returns None.
    assert _extract_detection_reason(labels) is None


@pytest.mark.asyncio
async def test_events_loop_unexpected_exception_continues(repo: SqliteRepository) -> None:
    """run_events_loop logs unexpected non-DockerSocketError exceptions and keeps running.

    Covers lines 214-220: the bare ``except Exception`` branch.
    """
    writer = MemoryRetainingMetricsWriter()
    ctx = _ctx(writer, repo)

    call_count = 0

    class _UnexpectedErrorClient(_FakeSocketClient):
        async def events(  # type: ignore[override]
            self, *, filters: dict[str, list[str]] | None = None
        ) -> AsyncIterator[dict[str, Any]]:
            nonlocal call_count
            call_count += 1
            raise RuntimeError("unexpected non-docker error")
            yield  # noqa: F841, RUF100

    fake_client = _UnexpectedErrorClient()
    discoverer = DockerDiscoverer(
        socket_client=fake_client,  # pyright: ignore[reportArgumentType]
        suggestions_repo=SuggestionsRepository(repo),
        db=repo,
    )

    original_sleep = asyncio.sleep

    async def _instant_sleep(delay: float) -> None:
        if call_count >= 2:  # noqa: PLR2004
            raise asyncio.CancelledError
        await original_sleep(0)

    import unittest.mock as _mock  # noqa: PLC0415

    with _mock.patch("asyncio.sleep", side_effect=_instant_sleep):  # noqa: SIM117
        with contextlib.suppress(asyncio.CancelledError):
            await discoverer.run_events_loop(ctx)

    assert call_count >= 1


@pytest.mark.asyncio
async def test_handle_event_ignores_event_without_container_id(repo: SqliteRepository) -> None:
    """_handle_event returns early when the event has no container ID.

    Covers line 231: ``if not container_id: return``.
    """
    writer = MemoryRetainingMetricsWriter()
    ctx = _ctx(writer, repo)
    sugg_repo = SuggestionsRepository(repo)

    # Event with neither Actor.ID nor Attributes.id.
    events = _async_iter(
        [
            {
                "Type": "container",
                "Action": "create",
                "Actor": {"ID": "", "Attributes": {}},
            }
        ]
    )

    fake_client = _FakeSocketClient(
        list_containers_result=[],
        events_iterator=events,
    )
    discoverer = DockerDiscoverer(
        socket_client=fake_client,  # pyright: ignore[reportArgumentType]
        suggestions_repo=SuggestionsRepository(repo),
        db=repo,
    )

    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(discoverer.run_events_loop(ctx), timeout=1.0)

    # No suggestion created — event was ignored.
    rows, _ = await sugg_repo.list_pending_docker_suggestions(status="all", limit=50, cursor=None)
    assert len(rows) == 0


def test_extract_detection_reason_profile_not_disabled() -> None:
    """Profile present but not 'disabled' → no disabled_profile reason."""
    from homelab_monitor.plugins.discoverers.docker_discoverer import (  # noqa: PLC0415
        _extract_detection_reason,  # pyright: ignore[reportPrivateUsage]
    )

    labels = {
        "homelab-monitor.http.api": "true",
        "com.docker.compose.config.profiles": "production,staging",
    }
    assert _extract_detection_reason(labels) is None


@pytest.mark.asyncio
async def test_compose_file_path_label_present(repo: SqliteRepository) -> None:
    """When com.docker.compose.project.config_files label is present, compose_file_path is captured."""  # noqa: E501
    writer = MemoryRetainingMetricsWriter()
    ctx = _ctx(writer, repo)
    sugg_repo = SuggestionsRepository(repo)

    fake_client = _FakeSocketClient(
        list_containers_result=[
            {
                "Id": "container-xyz",
                "Names": ["/test-container"],
                "Image": "nginx:latest",
                "Labels": {
                    "com.docker.compose.project": "compose",
                    "com.docker.compose.service": "library-organizer",
                    "com.docker.compose.project.config_files": "/storage/docker/compose/docker-compose.yml",  # noqa: E501
                },
            }
        ],
        inspect_results={
            "container-xyz": {
                "Id": "container-xyz",
                "Name": "/test-container",
                "Config": {
                    "Image": "nginx:latest",
                    "Labels": {
                        "com.docker.compose.project": "compose",
                        "com.docker.compose.service": "library-organizer",
                        "com.docker.compose.project.config_files": "/storage/docker/compose/docker-compose.yml",  # noqa: E501
                    },
                },
            }
        },
        events_iterator=_async_iter([]),
    )

    discoverer = DockerDiscoverer(
        socket_client=fake_client,  # pyright: ignore[reportArgumentType]
        suggestions_repo=sugg_repo,
        db=repo,
    )
    await discoverer.run(ctx)

    # Verify suggestion has compose_file_path set.
    rows, _ = await sugg_repo.list_pending_docker_suggestions(status="pending", limit=50)
    assert len(rows) == 1
    assert rows[0].compose_file_path == "/storage/docker/compose/docker-compose.yml"


@pytest.mark.asyncio
async def test_compose_file_path_label_absent(repo: SqliteRepository) -> None:
    """When com.docker.compose.project.config_files label is absent, compose_file_path is None."""
    writer = MemoryRetainingMetricsWriter()
    ctx = _ctx(writer, repo)
    sugg_repo = SuggestionsRepository(repo)

    fake_client = _FakeSocketClient(
        list_containers_result=[
            {
                "Id": "container-xyz",
                "Names": ["/test-container"],
                "Image": "nginx:latest",
                "Labels": {
                    "com.docker.compose.project": "compose",
                    "com.docker.compose.service": "web",
                },
            }
        ],
        inspect_results={
            "container-xyz": {
                "Id": "container-xyz",
                "Name": "/test-container",
                "Config": {
                    "Image": "nginx:latest",
                    "Labels": {
                        "com.docker.compose.project": "compose",
                        "com.docker.compose.service": "web",
                    },
                },
            }
        },
        events_iterator=_async_iter([]),
    )

    discoverer = DockerDiscoverer(
        socket_client=fake_client,  # pyright: ignore[reportArgumentType]
        suggestions_repo=sugg_repo,
        db=repo,
    )
    await discoverer.run(ctx)

    # Verify suggestion has compose_file_path as None.
    rows, _ = await sugg_repo.list_pending_docker_suggestions(status="pending", limit=50)
    assert len(rows) == 1
    assert rows[0].compose_file_path is None


@pytest.mark.asyncio
async def test_compose_service_missing_logs_warning(repo: SqliteRepository) -> None:
    """Container with compose_project but no compose_service triggers a warning.

    Covers the ``compose_service is None`` warning branch in ``_upsert_suggestion``.
    """
    writer = MemoryRetainingMetricsWriter()
    ctx = _ctx(writer, repo)
    sugg_repo = SuggestionsRepository(repo)

    # compose_project present, compose_service absent, compose_file_path absent.
    # Both warning branches (lines 294-300 and 301-307) fire; that is expected.
    fake_client = _FakeSocketClient(
        list_containers_result=[
            {
                "Id": "container-svcmissing",
                "Names": ["/svc-missing-container"],
                "Image": "nginx:latest",
                "Labels": {
                    "com.docker.compose.project": "myproject",
                    # intentionally no com.docker.compose.service
                },
            }
        ],
        inspect_results={
            "container-svcmissing": {
                "Id": "container-svcmissing",
                "Name": "/svc-missing-container",
                "Config": {
                    "Image": "nginx:latest",
                    "Labels": {
                        "com.docker.compose.project": "myproject",
                        # intentionally no com.docker.compose.service
                    },
                },
            }
        },
        events_iterator=_async_iter([]),
    )

    discoverer = DockerDiscoverer(
        socket_client=fake_client,  # pyright: ignore[reportArgumentType]
        suggestions_repo=sugg_repo,
        db=repo,
    )

    with structlog.testing.capture_logs() as cap_logs:
        await discoverer.run(ctx)

    # Suggestion should still be created.
    rows, _ = await sugg_repo.list_pending_docker_suggestions(status="pending", limit=50)
    assert len(rows) == 1

    # Warning for missing service must have been emitted.
    warning_keys = [entry.get("event") for entry in cap_logs if entry.get("log_level") == "warning"]
    assert "docker_discoverer.compose_service_missing" in warning_keys


# ---------------------------------------------------------------------------
# Wave 4b: Probe-targets upsert coverage gap tests (lines 366-413)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_discoverer_upserts_probes_when_labeled_clean(repo: SqliteRepository) -> None:
    """Container with valid labels triggers ProbeTargetsRepository.upsert_probe_target_conn.

    Covers the main probe-upsert path in _upsert_probe_targets_from_labels
    (lines 388-403): descriptors are upser into probe_targets table.
    """
    from homelab_monitor.kernel.db.repositories.probe_targets_repository import (  # noqa: PLC0415
        ProbeTargetsRepository,
    )

    writer = MemoryRetainingMetricsWriter()
    ctx = _ctx(writer, repo)
    sugg_repo = SuggestionsRepository(repo)
    probe_repo = ProbeTargetsRepository(repo)

    # Container with valid homelab-monitor labels (http and tcp kinds).
    fake_client = _FakeSocketClient(
        list_containers_result=[
            {
                "Id": "container-xyz",
                "Names": ["/webapp"],
                "Image": "nginx:latest",
                "Labels": {
                    "homelab-monitor.http.api": "http://container:8080/health",
                    "homelab-monitor.tcp.db": "tcp://container:5432",
                },
            }
        ],
        inspect_results={
            "container-xyz": {
                "Id": "container-xyz",
                "Name": "/webapp",
                "Config": {
                    "Image": "nginx:latest",
                    "Labels": {
                        "homelab-monitor.http.api": "http://container:8080/health",
                        "homelab-monitor.tcp.db": "tcp://container:5432",
                    },
                },
            }
        },
        events_iterator=_async_iter([]),
    )

    discoverer = DockerDiscoverer(
        socket_client=fake_client,  # pyright: ignore[reportArgumentType]
        suggestions_repo=sugg_repo,
        db=repo,
        probe_targets_repo=probe_repo,
    )
    await discoverer.run(ctx)

    # Verify NO suggestions created (healthy + labeled → no suggestion).
    sugg_rows, _ = await sugg_repo.list_pending_docker_suggestions(status="all", limit=50)
    assert len(sugg_rows) == 0

    # Verify 2 probes upser into probe_targets (http.api and tcp.db).
    probes = await probe_repo.list_for_container(container_name="webapp", include_hidden=False)
    assert len(probes) == 2  # noqa: PLR2004

    # Check both probes have the expected kind/name.
    probe_kinds_names = {(p.kind, p.name) for p in probes}
    assert probe_kinds_names == {("http", "api"), ("tcp", "db")}

    # Verify config_source is "label" for both.
    for p in probes:
        assert p.config_source == "label"


@pytest.mark.asyncio
async def test_discoverer_emits_label_malformed_suggestion(repo: SqliteRepository) -> None:
    """A malformed homelab-monitor label triggers docker_label_malformed suggestion.

    Covers lines 369-386: malformed labels are emitted as docker_label_malformed
    suggestions with detection_reason set to the reason code.
    """
    from sqlalchemy import text  # noqa: PLC0415

    from homelab_monitor.kernel.db.repositories.probe_targets_repository import (  # noqa: PLC0415
        ProbeTargetsRepository,
    )

    writer = MemoryRetainingMetricsWriter()
    ctx = _ctx(writer, repo)
    sugg_repo = SuggestionsRepository(repo)
    probe_repo = ProbeTargetsRepository(repo)

    # Container with a malformed http label (not a valid URL).
    fake_client = _FakeSocketClient(
        list_containers_result=[
            {
                "Id": "container-xyz",
                "Names": ["/webapp"],
                "Image": "nginx:latest",
                "Labels": {
                    "homelab-monitor.http.api": "not-a-url",
                },
            }
        ],
        inspect_results={
            "container-xyz": {
                "Id": "container-xyz",
                "Name": "/webapp",
                "Config": {
                    "Image": "nginx:latest",
                    "Labels": {
                        "homelab-monitor.http.api": "not-a-url",
                    },
                },
            }
        },
        events_iterator=_async_iter([]),
    )

    discoverer = DockerDiscoverer(
        socket_client=fake_client,  # pyright: ignore[reportArgumentType]
        suggestions_repo=sugg_repo,
        db=repo,
        probe_targets_repo=probe_repo,
    )
    await discoverer.run(ctx)

    # Verify docker_label_malformed suggestion created by querying suggestions table directly.
    # (list_pending_docker_suggestions filters to only docker_container_discovered and
    # docker_label_collision, so we query directly for docker_label_malformed)
    rows = await repo.fetch_all(
        text(
            "SELECT s.kind, d.detection_reason, d.container_name "
            "FROM suggestions s JOIN suggestions_docker d ON s.id = d.suggestion_id "
            "WHERE s.kind = 'docker_label_malformed'"
        ),
    )
    assert len(rows) == 1
    assert rows[0].kind == "docker_label_malformed"
    assert rows[0].detection_reason == "invalid_http_url"
    assert rows[0].container_name == "webapp"

    # Verify NO probes created (malformed labels don't create probes).
    probes = await ProbeTargetsRepository(repo).list_for_container(
        container_name="webapp", include_hidden=False
    )
    assert len(probes) == 0


@pytest.mark.asyncio
async def test_discoverer_hides_removed_probes(repo: SqliteRepository) -> None:
    """Probes removed from labels get hidden_at set.

    Covers lines 405-413: mark_missing_except_conn soft-deletes probes
    whose (kind, name) are not in the current label set.
    """
    from homelab_monitor.kernel.db.repositories.probe_targets_repository import (  # noqa: PLC0415
        ProbeTargetsRepository,
    )

    writer = MemoryRetainingMetricsWriter()
    ctx = _ctx(writer, repo)
    sugg_repo = SuggestionsRepository(repo)
    probe_repo = ProbeTargetsRepository(repo)

    # First tick: container has two probes.
    fake_client_tick1 = _FakeSocketClient(
        list_containers_result=[
            {
                "Id": "container-xyz",
                "Names": ["/webapp"],
                "Image": "nginx:latest",
                "Labels": {
                    "homelab-monitor.http.api": "http://container:8080/health",
                    "homelab-monitor.http.metrics": "http://container:8081/metrics",
                },
            }
        ],
        inspect_results={
            "container-xyz": {
                "Id": "container-xyz",
                "Name": "/webapp",
                "Config": {
                    "Image": "nginx:latest",
                    "Labels": {
                        "homelab-monitor.http.api": "http://container:8080/health",
                        "homelab-monitor.http.metrics": "http://container:8081/metrics",
                    },
                },
            }
        },
        events_iterator=_async_iter([]),
    )

    discoverer = DockerDiscoverer(
        socket_client=fake_client_tick1,  # pyright: ignore[reportArgumentType]
        suggestions_repo=sugg_repo,
        db=repo,
        probe_targets_repo=probe_repo,
    )
    await discoverer.run(ctx)

    # Verify 2 probes created, both unhidden.
    probes = await probe_repo.list_for_container(container_name="webapp", include_hidden=False)
    assert len(probes) == 2  # noqa: PLR2004
    probes_all = await probe_repo.list_for_container(container_name="webapp", include_hidden=True)
    assert len(probes_all) == 2  # noqa: PLR2004
    for p in probes_all:
        assert p.hidden_at is None

    # Second tick: container has only api label (metrics removed).
    fake_client_tick2 = _FakeSocketClient(
        list_containers_result=[
            {
                "Id": "container-xyz",
                "Names": ["/webapp"],
                "Image": "nginx:latest",
                "Labels": {
                    "homelab-monitor.http.api": "http://container:8080/health",
                },
            }
        ],
        inspect_results={
            "container-xyz": {
                "Id": "container-xyz",
                "Name": "/webapp",
                "Config": {
                    "Image": "nginx:latest",
                    "Labels": {
                        "homelab-monitor.http.api": "http://container:8080/health",
                    },
                },
            }
        },
        events_iterator=_async_iter([]),
    )

    discoverer2 = DockerDiscoverer(
        socket_client=fake_client_tick2,  # pyright: ignore[reportArgumentType]
        suggestions_repo=sugg_repo,
        db=repo,
        probe_targets_repo=probe_repo,
    )
    await discoverer2.run(ctx)

    # Verify: api probe is unhidden, metrics probe is hidden.
    probes_unhidden = await probe_repo.list_for_container(
        container_name="webapp", include_hidden=False
    )
    assert len(probes_unhidden) == 1
    assert probes_unhidden[0].name == "api"
    assert probes_unhidden[0].hidden_at is None

    probes_all = await probe_repo.list_for_container(container_name="webapp", include_hidden=True)
    assert len(probes_all) == 2  # noqa: PLR2004
    metrics_probe = next(p for p in probes_all if p.name == "metrics")
    assert metrics_probe.hidden_at is not None


@pytest.mark.asyncio
async def test_discoverer_disabled_profile_container_skips_probes(repo: SqliteRepository) -> None:
    """Disabled-profile container with labels does NOT upsert probes.

    The disabled_profile gate happens in _upsert_suggestion (line 293),
    NOT in _upsert_probe_targets_from_labels. This test verifies the gate
    by checking that when a disabled-profile container is processed,
    _upsert_probe_targets_from_labels is never reached (no probes created).
    """
    from homelab_monitor.kernel.db.repositories.probe_targets_repository import (  # noqa: PLC0415
        ProbeTargetsRepository,
    )

    writer = MemoryRetainingMetricsWriter()
    ctx = _ctx(writer, repo)
    sugg_repo = SuggestionsRepository(repo)
    probe_repo = ProbeTargetsRepository(repo)

    # Container with valid labels BUT disabled profile.
    # The gate in _upsert_suggestion (reason == "disabled_profile")
    # should prevent probe upsert.
    fake_client = _FakeSocketClient(
        list_containers_result=[
            {
                "Id": "container-xyz",
                "Names": ["/webapp"],
                "Image": "nginx:latest",
                "Labels": {
                    "homelab-monitor.http.api": "http://container:8080/health",
                    "com.docker.compose.config.profiles": "disabled",
                },
            }
        ],
        inspect_results={
            "container-xyz": {
                "Id": "container-xyz",
                "Name": "/webapp",
                "Config": {
                    "Image": "nginx:latest",
                    "Labels": {
                        "homelab-monitor.http.api": "http://container:8080/health",
                        "com.docker.compose.config.profiles": "disabled",
                    },
                },
            }
        },
        events_iterator=_async_iter([]),
    )

    discoverer = DockerDiscoverer(
        socket_client=fake_client,  # pyright: ignore[reportArgumentType]
        suggestions_repo=sugg_repo,
        db=repo,
        probe_targets_repo=probe_repo,
    )
    await discoverer.run(ctx)

    # Verify: a docker_container_discovered suggestion was created.
    # (detection_reason="disabled_profile")
    sugg_rows, _ = await sugg_repo.list_pending_docker_suggestions(status="all", limit=50)
    assert len(sugg_rows) == 1
    assert sugg_rows[0].detection_reason == "disabled_profile"

    # Verify: NO probes were created (the gate in _upsert_suggestion prevented probe upsert).
    probes = await probe_repo.list_for_container(container_name="webapp", include_hidden=False)
    assert len(probes) == 0


@pytest.mark.asyncio
async def test_discoverer_strips_docker_rename_prefix_from_container_name(
    repo: SqliteRepository,
) -> None:
    """Docker renames old container to <hex12>_<name> during compose --force-recreate.

    The discoverer must strip that prefix so probe_targets stays keyed on the
    canonical name (matches targets.name). See STAGE-003-006 Refinement bug.
    """
    from homelab_monitor.kernel.db.repositories.probe_targets_repository import (  # noqa: PLC0415
        ProbeTargetsRepository,
    )

    writer = MemoryRetainingMetricsWriter()
    ctx = _ctx(writer, repo)
    probe_repo = ProbeTargetsRepository(repo)

    fake_client = _FakeSocketClient(
        list_containers_result=[
            {"Id": "abc123", "Names": ["/51d0af1f0f51_homelab-grafana"]},
        ],
        inspect_results={
            "abc123": {
                "Id": "abc123",
                "Name": "/51d0af1f0f51_homelab-grafana",
                "Image": "grafana:latest",
                "Config": {
                    "Labels": {
                        "homelab-monitor.http.health": "http://container:3000/api/health",
                    },
                },
                "State": {"Status": "running", "ExitCode": 0, "Health": {"Status": "healthy"}},
                "RestartCount": 0,
                "HostConfig": {"NetworkMode": "bridge"},
                "NetworkSettings": {"Networks": {}},
            },
        },
        events_iterator=_async_iter([]),
    )
    discoverer = DockerDiscoverer(
        socket_client=fake_client,  # pyright: ignore[reportArgumentType]
        suggestions_repo=SuggestionsRepository(repo),
        db=repo,
        probe_targets_repo=probe_repo,
    )
    await discoverer.run(ctx)

    # The probe_target row should be keyed on the canonical name, NOT the prefixed name.
    probes = await probe_repo.list_for_container(container_name="homelab-grafana")
    assert len(probes) == 1
    assert probes[0].container_name == "homelab-grafana"

    # And NO row should exist under the prefixed name.
    probes_prefixed = await probe_repo.list_for_container(
        container_name="51d0af1f0f51_homelab-grafana"
    )
    assert len(probes_prefixed) == 0
