"""Tests for ComposeActionRunner (STAGE-003-010)."""

from __future__ import annotations

import asyncio
import textwrap
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import structlog
from prometheus_client import CollectorRegistry

from homelab_monitor.kernel.db.repositories.compose_actions_repository import (
    ComposeActionsRepository,
)
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.docker.build_sources_loader import BuildSourcesLoader
from homelab_monitor.kernel.docker.build_sources_schema import (
    BuildSourcesConfig,
    ComposeFileEntry,
)
from homelab_monitor.kernel.docker.compose_action_runner import (
    OUTPUT_MAX_CHARS,
    TRUNCATION_MARKER,
    ComposeActionRunner,
    get_or_create_counter,
    resolve_timeout_seconds,
    truncate,
)
from homelab_monitor.kernel.docker.compose_reader import ComposeReadError
from homelab_monitor.kernel.docker.socket_client import DockerSocketConnectionError

if TYPE_CHECKING:
    pass

DEFAULT_TIMEOUT_SECONDS = 300.0
CUSTOM_TIMEOUT_SECONDS = 60.0


def _make_log() -> structlog.stdlib.BoundLogger:
    return structlog.get_logger().bind(component="test")  # pyright: ignore[reportReturnType]


def _make_socket_client() -> MagicMock:
    """Create a mock socket_client."""
    return MagicMock()


def _make_runner(
    repo: SqliteRepository,
    *,
    timeout_seconds: float = 5.0,
    loader: BuildSourcesLoader | None = None,
    socket_client: MagicMock | None = None,
) -> ComposeActionRunner:
    actions_repo = ComposeActionsRepository(repo)
    if loader is None:
        loader = BuildSourcesLoader(config_path=Path("/nonexistent.yaml"), log=_make_log())
    if socket_client is None:
        socket_client = _make_socket_client()
    return ComposeActionRunner(
        repo=repo,
        actions_repo=actions_repo,
        build_sources_loader=loader,
        socket_client=socket_client,
        prom_registry=CollectorRegistry(),
        log=_make_log(),
        timeout_seconds=timeout_seconds,
    )


def _write_compose(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "docker-compose.yml"
    p.write_text(content, encoding="utf-8")
    return p


def _loader_with(config: BuildSourcesConfig) -> BuildSourcesLoader:
    loader = BuildSourcesLoader(config_path=Path("/nonexistent.yaml"), log=_make_log())
    # Force-set the loaded config without going through refresh.
    loader._current_config = config  # pyright: ignore[reportPrivateUsage]
    loader._current_error = None  # pyright: ignore[reportPrivateUsage]
    return loader


def test_truncate_under_cap_passthrough() -> None:
    assert truncate("hello") == "hello"


def test_truncate_over_cap_marks() -> None:
    s = "x" * (OUTPUT_MAX_CHARS + 100)
    out = truncate(s)
    assert out.endswith(TRUNCATION_MARKER)
    assert len(out) == OUTPUT_MAX_CHARS + len(TRUNCATION_MARKER)


def test_resolve_timeout_seconds_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HOMELAB_MONITOR_COMPOSE_ACTION_TIMEOUT_SECONDS", raising=False)
    assert resolve_timeout_seconds() == DEFAULT_TIMEOUT_SECONDS


def test_resolve_timeout_seconds_custom(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOMELAB_MONITOR_COMPOSE_ACTION_TIMEOUT_SECONDS", "60")
    assert resolve_timeout_seconds() == CUSTOM_TIMEOUT_SECONDS


def test_resolve_timeout_seconds_invalid_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOMELAB_MONITOR_COMPOSE_ACTION_TIMEOUT_SECONDS", "abc")
    assert resolve_timeout_seconds() == DEFAULT_TIMEOUT_SECONDS


def test_resolve_timeout_seconds_zero_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOMELAB_MONITOR_COMPOSE_ACTION_TIMEOUT_SECONDS", "0")
    assert resolve_timeout_seconds() == DEFAULT_TIMEOUT_SECONDS


@pytest.mark.asyncio
async def test_resolve_compose_uses_compose_service_label(
    repo: SqliteRepository, tmp_path: Path
) -> None:
    """resolve_compose reads the container's com.docker.compose.service label."""
    compose_path = _write_compose(
        tmp_path,
        "services:\n  caddy:\n    image: caddy:latest\n",
    )
    config = BuildSourcesConfig(
        compose_files=[
            ComposeFileEntry(host_path=str(compose_path), container_path=str(compose_path))
        ]
    )
    socket_client = _make_socket_client()
    socket_client.inspect_container = AsyncMock(
        return_value={
            "Id": "abc123",
            "Name": "/caddy",
            "Config": {
                "Labels": {
                    "com.docker.compose.service": "caddy",
                    "com.docker.compose.project": "myproject",
                }
            },
        }
    )
    runner = _make_runner(repo, loader=_loader_with(config), socket_client=socket_client)
    resolved = await runner.resolve_compose("caddy")
    assert resolved is not None
    assert resolved.compose_service == "caddy"
    assert resolved.compose_file_path == str(compose_path.resolve())
    assert resolved.compose_project == "myproject"
    socket_client.inspect_container.assert_called_once_with("caddy")


@pytest.mark.asyncio
async def test_resolve_compose_fails_when_no_compose_label(
    repo: SqliteRepository, tmp_path: Path
) -> None:
    """resolve_compose returns None when container has no compose.service label."""
    compose_path = _write_compose(
        tmp_path,
        "services:\n  caddy:\n    image: caddy:latest\n",
    )
    config = BuildSourcesConfig(
        compose_files=[
            ComposeFileEntry(host_path=str(compose_path), container_path=str(compose_path))
        ]
    )
    socket_client = _make_socket_client()
    socket_client.inspect_container = AsyncMock(
        return_value={
            "Id": "abc123",
            "Name": "/ghost",
            "Config": {"Labels": {}},  # No com.docker.compose.service label
        }
    )
    runner = _make_runner(repo, loader=_loader_with(config), socket_client=socket_client)
    assert await runner.resolve_compose("ghost") is None


@pytest.mark.asyncio
async def test_resolve_compose_fails_when_service_not_in_file(
    repo: SqliteRepository, tmp_path: Path
) -> None:
    """resolve_compose returns None when labeled service not in compose file."""
    compose_path = _write_compose(
        tmp_path,
        "services:\n  caddy:\n    image: caddy:latest\n",
    )
    config = BuildSourcesConfig(
        compose_files=[
            ComposeFileEntry(host_path=str(compose_path), container_path=str(compose_path))
        ]
    )
    socket_client = _make_socket_client()
    socket_client.inspect_container = AsyncMock(
        return_value={
            "Id": "abc123",
            "Name": "/nginx",
            "Config": {
                "Labels": {"com.docker.compose.service": "nginx"}  # nginx not in compose
            },
        }
    )
    runner = _make_runner(repo, loader=_loader_with(config), socket_client=socket_client)
    assert await runner.resolve_compose("nginx") is None


@pytest.mark.asyncio
async def test_resolve_compose_socket_error_returns_none(repo: SqliteRepository) -> None:
    """resolve_compose returns None when socket_client raises error."""
    socket_client = _make_socket_client()
    socket_client.inspect_container = AsyncMock(
        side_effect=DockerSocketConnectionError("socket unreachable")
    )
    runner = _make_runner(repo, socket_client=socket_client)
    assert await runner.resolve_compose("ghost") is None


@pytest.mark.asyncio
async def test_trigger_unresolvable_inserts_failed_row(
    repo: SqliteRepository,
) -> None:
    socket_client = _make_socket_client()
    socket_client.inspect_container = AsyncMock(
        return_value={
            "Id": "abc123",
            "Name": "/ghost",
            "Config": {"Labels": {}},  # No label
        }
    )
    runner = _make_runner(repo, socket_client=socket_client)
    action_id = await runner.trigger_pull_and_restart(
        container_name="ghost", who="op", client_ip="127.0.0.1"
    )
    row = await ComposeActionsRepository(repo).get_by_id(action_id)
    assert row is not None
    assert row.state == "failed"
    assert row.error_reason == "container_not_managed_by_compose"
    assert row.who == "op"


@pytest.mark.asyncio
async def test_trigger_happy_path_runs_subprocess(repo: SqliteRepository, tmp_path: Path) -> None:
    compose_path = _write_compose(
        tmp_path,
        "services:\n  caddy:\n    image: caddy:latest\n",
    )
    config = BuildSourcesConfig(
        compose_files=[
            ComposeFileEntry(host_path=str(compose_path), container_path=str(compose_path))
        ]
    )
    socket_client = _make_socket_client()
    socket_client.inspect_container = AsyncMock(
        return_value={
            "Id": "abc123",
            "Name": "/caddy",
            "Config": {"Labels": {"com.docker.compose.service": "caddy"}},
        }
    )
    runner = _make_runner(repo, loader=_loader_with(config), socket_client=socket_client)

    # Mock asyncio.create_subprocess_exec to return a fake successful process.
    fake_proc = MagicMock()
    fake_proc.communicate = AsyncMock(return_value=(b"pulled\n", b""))
    fake_proc.returncode = 0
    with patch(
        "homelab_monitor.kernel.docker.compose_action_runner.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=fake_proc),
    ):
        action_id = await runner.trigger_pull_and_restart(
            container_name="caddy", who="alice", client_ip="10.0.0.1"
        )
        # Wait for background task.
        await asyncio.gather(*runner._active_tasks, return_exceptions=True)  # pyright: ignore[reportPrivateUsage]

    row = await ComposeActionsRepository(repo).get_by_id(action_id)
    assert row is not None
    assert row.state == "success"
    assert row.exit_code == 0
    assert "pulled" in (row.stdout or "")


@pytest.mark.asyncio
async def test_trigger_pull_failure_aborts_up(repo: SqliteRepository, tmp_path: Path) -> None:
    compose_path = _write_compose(tmp_path, "services:\n  caddy:\n    image: caddy:latest\n")
    config = BuildSourcesConfig(
        compose_files=[
            ComposeFileEntry(host_path=str(compose_path), container_path=str(compose_path))
        ]
    )
    socket_client = _make_socket_client()
    socket_client.inspect_container = AsyncMock(
        return_value={
            "Id": "abc123",
            "Name": "/caddy",
            "Config": {"Labels": {"com.docker.compose.service": "caddy"}},
        }
    )
    runner = _make_runner(repo, loader=_loader_with(config), socket_client=socket_client)

    fake_proc = MagicMock()
    fake_proc.communicate = AsyncMock(return_value=(b"", b"pull failed"))
    fake_proc.returncode = 1
    with patch(
        "homelab_monitor.kernel.docker.compose_action_runner.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=fake_proc),
    ) as mock_spawn:
        action_id = await runner.trigger_pull_and_restart(
            container_name="caddy", who="op", client_ip=None
        )
        await asyncio.gather(*runner._active_tasks, return_exceptions=True)  # pyright: ignore[reportPrivateUsage]

    row = await ComposeActionsRepository(repo).get_by_id(action_id)
    assert row is not None
    assert row.state == "failed"
    assert row.error_reason == "exit_nonzero"
    # Only the pull subprocess should have been spawned; up was skipped.
    assert mock_spawn.call_count == 1


@pytest.mark.asyncio
async def test_up_subprocess_uses_force_recreate(repo: SqliteRepository, tmp_path: Path) -> None:
    """The `up` subprocess must include --force-recreate to handle already-running containers."""
    compose_path = _write_compose(tmp_path, "services:\n  caddy:\n    image: caddy:latest\n")
    config = BuildSourcesConfig(
        compose_files=[
            ComposeFileEntry(host_path=str(compose_path), container_path=str(compose_path))
        ]
    )
    socket_client = _make_socket_client()
    socket_client.inspect_container = AsyncMock(
        return_value={
            "Id": "abc123",
            "Name": "/caddy",
            "Config": {"Labels": {"com.docker.compose.service": "caddy"}},
        }
    )
    runner = _make_runner(repo, loader=_loader_with(config), socket_client=socket_client)

    fake_proc = MagicMock()
    fake_proc.communicate = AsyncMock(return_value=(b"done\n", b""))
    fake_proc.returncode = 0
    with patch(
        "homelab_monitor.kernel.docker.compose_action_runner.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=fake_proc),
    ) as mock_spawn:
        _ = await runner.trigger_pull_and_restart(
            container_name="caddy", who="alice", client_ip="10.0.0.1"
        )
        await asyncio.gather(*runner._active_tasks, return_exceptions=True)  # pyright: ignore[reportPrivateUsage]

    # Two subprocess calls: pull, then up. Check the up call (index 1).
    expected_subprocess_calls = 2
    assert mock_spawn.call_count == expected_subprocess_calls
    up_call_args = mock_spawn.call_args_list[1]
    # call_args_list[1] is the `up` invocation; args are positional to create_subprocess_exec.
    spawned_argv = list(up_call_args.args)
    assert "--force-recreate" in spawned_argv


@pytest.mark.asyncio
async def test_trigger_subprocess_timeout_records_timeout_state(
    repo: SqliteRepository, tmp_path: Path
) -> None:
    compose_path = _write_compose(tmp_path, "services:\n  caddy:\n    image: caddy:latest\n")
    config = BuildSourcesConfig(
        compose_files=[
            ComposeFileEntry(host_path=str(compose_path), container_path=str(compose_path))
        ]
    )
    socket_client = _make_socket_client()
    socket_client.inspect_container = AsyncMock(
        return_value={
            "Id": "abc123",
            "Name": "/caddy",
            "Config": {"Labels": {"com.docker.compose.service": "caddy"}},
        }
    )
    runner = _make_runner(
        repo, loader=_loader_with(config), socket_client=socket_client, timeout_seconds=0.05
    )

    async def _hang(*_a: object, **_kw: object) -> tuple[bytes, bytes]:
        await asyncio.sleep(5)
        return (b"", b"")  # pragma: no cover -- never reached

    fake_proc = MagicMock()
    fake_proc.communicate = AsyncMock(side_effect=_hang)
    fake_proc.returncode = None
    fake_proc.send_signal = MagicMock()
    fake_proc.kill = MagicMock()
    # After SIGTERM grace, communicate returns empty bytes.
    fake_proc.communicate.side_effect = [
        TimeoutError(),  # first wait
        (b"", b"killed by sigterm"),  # after sigterm grace
    ]
    with patch(
        "homelab_monitor.kernel.docker.compose_action_runner.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=fake_proc),
    ):
        # Patch asyncio.wait_for to raise TimeoutError on first call (the main
        # timeout) and return on the SIGTERM-grace call.
        original_wait_for = asyncio.wait_for
        call_count = {"n": 0}

        async def fake_wait_for(awaitable: object, timeout: object) -> object:
            call_count["n"] += 1
            if call_count["n"] == 1:
                # Cancel the awaitable & raise TimeoutError to simulate the
                # subprocess hanging past the configured timeout.
                if hasattr(awaitable, "close"):
                    awaitable.close()  # pyright: ignore[reportUnknownMemberType, reportAttributeAccessIssue]
                raise TimeoutError
            return await original_wait_for(awaitable, timeout)  # pyright: ignore[reportArgumentType, reportUnknownVariableType]

        with patch(
            "homelab_monitor.kernel.docker.compose_action_runner.asyncio.wait_for",
            new=fake_wait_for,
        ):
            action_id = await runner.trigger_pull_and_restart(
                container_name="caddy", who="op", client_ip=None
            )
            await asyncio.gather(
                *runner._active_tasks,  # pyright: ignore[reportPrivateUsage]
                return_exceptions=True,
            )

    row = await ComposeActionsRepository(repo).get_by_id(action_id)
    assert row is not None
    assert row.state == "timeout"
    assert row.error_reason == "timeout"


@pytest.mark.asyncio
async def test_trigger_docker_cli_missing(repo: SqliteRepository, tmp_path: Path) -> None:
    compose_path = _write_compose(tmp_path, "services:\n  caddy:\n    image: caddy:latest\n")
    config = BuildSourcesConfig(
        compose_files=[
            ComposeFileEntry(host_path=str(compose_path), container_path=str(compose_path))
        ]
    )
    socket_client = _make_socket_client()
    socket_client.inspect_container = AsyncMock(
        return_value={
            "Id": "abc123",
            "Name": "/caddy",
            "Config": {"Labels": {"com.docker.compose.service": "caddy"}},
        }
    )
    runner = _make_runner(repo, loader=_loader_with(config), socket_client=socket_client)
    with patch(
        "homelab_monitor.kernel.docker.compose_action_runner.asyncio.create_subprocess_exec",
        new=AsyncMock(side_effect=FileNotFoundError("docker")),
    ):
        action_id = await runner.trigger_pull_and_restart(
            container_name="caddy", who="op", client_ip=None
        )
        await asyncio.gather(*runner._active_tasks, return_exceptions=True)  # pyright: ignore[reportPrivateUsage]
    row = await ComposeActionsRepository(repo).get_by_id(action_id)
    assert row is not None
    assert row.state == "failed"
    assert row.error_reason == "docker_cli_missing"


@pytest.mark.asyncio
async def test_audit_row_written_on_success(repo: SqliteRepository, tmp_path: Path) -> None:
    """An audit_log row should exist with what='docker.compose.pull_and_restart'."""
    compose_path = _write_compose(tmp_path, "services:\n  caddy:\n    image: caddy:latest\n")
    config = BuildSourcesConfig(
        compose_files=[
            ComposeFileEntry(host_path=str(compose_path), container_path=str(compose_path))
        ]
    )
    socket_client = _make_socket_client()
    socket_client.inspect_container = AsyncMock(
        return_value={
            "Id": "abc123",
            "Name": "/caddy",
            "Config": {"Labels": {"com.docker.compose.service": "caddy"}},
        }
    )
    runner = _make_runner(repo, loader=_loader_with(config), socket_client=socket_client)
    fake_proc = MagicMock()
    fake_proc.communicate = AsyncMock(return_value=(b"", b""))
    fake_proc.returncode = 0
    with patch(
        "homelab_monitor.kernel.docker.compose_action_runner.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=fake_proc),
    ):
        await runner.trigger_pull_and_restart(
            container_name="caddy", who="alice", client_ip="10.0.0.5"
        )
        await asyncio.gather(*runner._active_tasks, return_exceptions=True)  # pyright: ignore[reportPrivateUsage]
    from sqlalchemy import text  # noqa: PLC0415

    rows = await repo.fetch_all(
        text("SELECT who, what, ip FROM audit_log WHERE what = 'docker.compose.pull_and_restart'")
    )
    assert len(rows) >= 1
    assert rows[0].who == "alice"
    assert rows[0].ip == "10.0.0.5"


@pytest.mark.asyncio
async def test_per_container_lock_serializes_same_container(
    repo: SqliteRepository, tmp_path: Path
) -> None:
    """Two triggers on the same container run sequentially, not concurrently."""
    compose_path = _write_compose(tmp_path, "services:\n  caddy:\n    image: caddy:latest\n")
    config = BuildSourcesConfig(
        compose_files=[
            ComposeFileEntry(host_path=str(compose_path), container_path=str(compose_path))
        ]
    )
    socket_client = _make_socket_client()
    socket_client.inspect_container = AsyncMock(
        return_value={
            "Id": "abc123",
            "Name": "/caddy",
            "Config": {"Labels": {"com.docker.compose.service": "caddy"}},
        }
    )
    runner = _make_runner(repo, loader=_loader_with(config), socket_client=socket_client)
    overlap_count = {"max": 0, "cur": 0}

    async def _slow_communicate() -> tuple[bytes, bytes]:
        overlap_count["cur"] += 1
        overlap_count["max"] = max(overlap_count["max"], overlap_count["cur"])
        await asyncio.sleep(0.05)
        overlap_count["cur"] -= 1
        return (b"", b"")

    def _make_proc() -> MagicMock:
        p = MagicMock()
        p.communicate = AsyncMock(side_effect=_slow_communicate)
        p.returncode = 0
        return p

    def _make_proc_side_effect(*_a: object, **_kw: object) -> MagicMock:
        return _make_proc()

    with patch(
        "homelab_monitor.kernel.docker.compose_action_runner.asyncio.create_subprocess_exec",
        new=AsyncMock(side_effect=_make_proc_side_effect),
    ):
        await runner.trigger_pull_and_restart(container_name="caddy", who="op", client_ip=None)
        await runner.trigger_pull_and_restart(container_name="caddy", who="op", client_ip=None)
        await asyncio.gather(*runner._active_tasks, return_exceptions=True)  # pyright: ignore[reportPrivateUsage]
    assert overlap_count["max"] == 1


@pytest.mark.asyncio
async def test_metrics_success_counter_increments(repo: SqliteRepository, tmp_path: Path) -> None:
    compose_path = _write_compose(tmp_path, "services:\n  caddy:\n    image: caddy:latest\n")
    config = BuildSourcesConfig(
        compose_files=[
            ComposeFileEntry(host_path=str(compose_path), container_path=str(compose_path))
        ]
    )
    socket_client = _make_socket_client()
    socket_client.inspect_container = AsyncMock(
        return_value={
            "Id": "abc123",
            "Name": "/caddy",
            "Config": {"Labels": {"com.docker.compose.service": "caddy"}},
        }
    )
    runner = _make_runner(repo, loader=_loader_with(config), socket_client=socket_client)
    fake_proc = MagicMock()
    fake_proc.communicate = AsyncMock(return_value=(b"", b""))
    fake_proc.returncode = 0
    with patch(
        "homelab_monitor.kernel.docker.compose_action_runner.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=fake_proc),
    ):
        await runner.trigger_pull_and_restart(container_name="caddy", who="op", client_ip=None)
        await asyncio.gather(*runner._active_tasks, return_exceptions=True)  # pyright: ignore[reportPrivateUsage]
    counter = runner._success_total.labels(  # pyright: ignore[reportPrivateUsage]
        container="caddy", action="pull_and_restart"
    )
    val = float(counter._value.get())  # type: ignore[attr-defined]  # pyright: ignore[reportUnknownArgumentType]
    assert val == 1.0


@pytest.mark.asyncio
async def test_shutdown_cancels_active_tasks(repo: SqliteRepository, tmp_path: Path) -> None:
    compose_path = _write_compose(tmp_path, "services:\n  caddy:\n    image: caddy:latest\n")
    config = BuildSourcesConfig(
        compose_files=[
            ComposeFileEntry(host_path=str(compose_path), container_path=str(compose_path))
        ]
    )
    socket_client = _make_socket_client()
    socket_client.inspect_container = AsyncMock(
        return_value={
            "Id": "abc123",
            "Name": "/caddy",
            "Config": {"Labels": {"com.docker.compose.service": "caddy"}},
        }
    )
    runner = _make_runner(repo, loader=_loader_with(config), socket_client=socket_client)

    async def _block_forever() -> tuple[bytes, bytes]:
        await asyncio.sleep(60)
        return (b"", b"")  # pragma: no cover -- cancelled

    fake_proc = MagicMock()
    fake_proc.communicate = AsyncMock(side_effect=_block_forever)
    fake_proc.returncode = None
    with patch(
        "homelab_monitor.kernel.docker.compose_action_runner.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=fake_proc),
    ):
        await runner.trigger_pull_and_restart(container_name="caddy", who="op", client_ip=None)
        await runner.shutdown()
    assert len(runner._active_tasks) == 0  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_resolve_compose_returns_none_when_no_build_config(
    repo: SqliteRepository,
) -> None:
    """Return None when BuildSourcesLoader has no current_config (lines 197-201)."""
    socket_client = _make_socket_client()
    socket_client.inspect_container = AsyncMock(
        return_value={
            "Id": "abc123",
            "Name": "/caddy",
            "Config": {"Labels": {"com.docker.compose.service": "caddy"}},
        }
    )
    # Default loader has current_config = None (no YAML loaded).
    runner = _make_runner(repo, socket_client=socket_client)
    result = await runner.resolve_compose("caddy")
    assert result is None


@pytest.mark.asyncio
async def test_resolve_compose_returns_none_when_compose_read_fails(
    repo: SqliteRepository, tmp_path: Path
) -> None:
    """Return None when read_compose_set raises ComposeReadError (lines 212-219)."""
    compose_path = _write_compose(
        tmp_path,
        "services:\n  caddy:\n    image: caddy:latest\n",
    )
    config = BuildSourcesConfig(
        compose_files=[
            ComposeFileEntry(host_path=str(compose_path), container_path=str(compose_path))
        ]
    )
    socket_client = _make_socket_client()
    socket_client.inspect_container = AsyncMock(
        return_value={
            "Id": "abc123",
            "Name": "/caddy",
            "Config": {"Labels": {"com.docker.compose.service": "caddy"}},
        }
    )
    runner = _make_runner(repo, loader=_loader_with(config), socket_client=socket_client)
    with patch(
        "homelab_monitor.kernel.docker.compose_action_runner.read_compose_set",
        side_effect=ComposeReadError("bad YAML", reason="malformed_yaml"),
    ):
        result = await runner.resolve_compose("caddy")
    assert result is None


@pytest.mark.asyncio
async def test_trigger_unresolvable_socket_error_on_second_inspect(
    repo: SqliteRepository,
) -> None:
    """Second inspect_container raises DockerSocketError; silently passes (lines 276-284)."""
    # First call (inside resolve_compose): raises DockerSocketError → resolved=None.
    # Second call (inside trigger_pull_and_restart error-reason probe): also raises.
    socket_client = _make_socket_client()
    socket_client.inspect_container = AsyncMock(
        side_effect=DockerSocketConnectionError("socket unreachable")
    )
    runner = _make_runner(repo, socket_client=socket_client)
    action_id = await runner.trigger_pull_and_restart(
        container_name="ghost", who="op", client_ip=None
    )
    row = await ComposeActionsRepository(repo).get_by_id(action_id)
    assert row is not None
    assert row.state == "failed"
    # When the second inspect also fails, error_reason stays "container_not_resolvable".
    assert row.error_reason == "container_not_resolvable"


@pytest.mark.asyncio
async def test_trigger_unresolvable_compose_service_in_file_error_reason(
    repo: SqliteRepository,
) -> None:
    """Second inspect has compose label -> error_reason='compose_service_not_in_file' (line 282)."""
    # First call (resolve_compose step 1): returns no compose label → resolve_compose returns None.
    # Second call (error-reason probe): returns the compose label → line 282 branch.
    call_count = {"n": 0}

    async def _side_effect(container_name: str) -> dict[str, object]:
        call_count["n"] += 1
        if call_count["n"] == 1:
            # resolve_compose inspect — no label → returns None via no_compose_service_label
            return {"Id": "abc", "Config": {"Labels": {}}}
        # error-reason probe — label present → compose_service_not_in_file
        return {"Id": "abc", "Config": {"Labels": {"com.docker.compose.service": "caddy"}}}

    socket_client = _make_socket_client()
    socket_client.inspect_container = AsyncMock(side_effect=_side_effect)
    runner = _make_runner(repo, socket_client=socket_client)
    action_id = await runner.trigger_pull_and_restart(
        container_name="caddy", who="op", client_ip=None
    )
    row = await ComposeActionsRepository(repo).get_by_id(action_id)
    assert row is not None
    assert row.state == "failed"
    assert row.error_reason == "compose_service_not_in_file"


@pytest.mark.asyncio
async def test_trigger_unresolvable_config_not_dict(
    repo: SqliteRepository,
) -> None:
    """trigger: second inspect Config is not a dict → skips inner block (branch 276->286)."""
    call_count = {"n": 0}

    async def _side_effect(container_name: str) -> dict[str, object]:
        call_count["n"] += 1
        if call_count["n"] == 1:
            return {"Id": "abc", "Config": {"Labels": {}}}  # first: no label → None
        return {"Id": "abc", "Config": None}  # second: Config is None (not dict)

    socket_client = _make_socket_client()
    socket_client.inspect_container = AsyncMock(side_effect=_side_effect)
    runner = _make_runner(repo, socket_client=socket_client)
    action_id = await runner.trigger_pull_and_restart(
        container_name="caddy", who="op", client_ip=None
    )
    row = await ComposeActionsRepository(repo).get_by_id(action_id)
    assert row is not None
    assert row.state == "failed"
    assert row.error_reason == "container_not_resolvable"


@pytest.mark.asyncio
async def test_trigger_unresolvable_labels_not_dict(
    repo: SqliteRepository,
) -> None:
    """trigger: second inspect Labels is not a dict → skips inner block (branch 278->286)."""
    call_count = {"n": 0}

    async def _side_effect(container_name: str) -> dict[str, object]:
        call_count["n"] += 1
        if call_count["n"] == 1:
            return {"Id": "abc", "Config": {"Labels": {}}}  # first: no label → None
        return {"Id": "abc", "Config": {"Labels": "not-a-dict"}}  # second: Labels not a dict

    socket_client = _make_socket_client()
    socket_client.inspect_container = AsyncMock(side_effect=_side_effect)
    runner = _make_runner(repo, socket_client=socket_client)
    action_id = await runner.trigger_pull_and_restart(
        container_name="caddy", who="op", client_ip=None
    )
    row = await ComposeActionsRepository(repo).get_by_id(action_id)
    assert row is not None
    assert row.state == "failed"
    assert row.error_reason == "container_not_resolvable"


@pytest.mark.asyncio
async def test_shutdown_no_op_when_no_active_tasks(repo: SqliteRepository) -> None:
    """shutdown returns immediately when there are no active tasks (line 344)."""
    runner = _make_runner(repo)
    # Should not raise and should return immediately.
    await runner.shutdown()
    assert len(runner._active_tasks) == 0  # pyright: ignore[reportPrivateUsage]


def test_get_or_create_counter_returns_existing(repo: SqliteRepository) -> None:
    """get_or_create_counter returns the existing counter on the second call (line 602)."""
    registry = CollectorRegistry()
    c1 = get_or_create_counter(registry, "test_counter_dedup", "docs", ["label"])
    c2 = get_or_create_counter(registry, "test_counter_dedup", "docs", ["label"])
    assert c1 is c2


@pytest.mark.asyncio
async def test_trigger_image_recheck_inspect_socket_error_does_not_raise(
    repo: SqliteRepository, tmp_path: Path
) -> None:
    """_trigger_image_recheck logs a warning and returns when inspect raises DockerSocketError."""
    from homelab_monitor.kernel.docker.socket_client import DockerSocketError  # noqa: PLC0415

    compose_path = _write_compose(tmp_path, "services:\n  caddy:\n    image: caddy:latest\n")
    config = BuildSourcesConfig(
        compose_files=[
            ComposeFileEntry(host_path=str(compose_path), container_path=str(compose_path))
        ]
    )
    # First call (resolve_compose): returns valid labels.
    # Second call (_trigger_image_recheck): raises DockerSocketError.
    inspect_responses: list[object] = [
        {
            "Id": "abc123",
            "Name": "/caddy",
            "Config": {"Labels": {"com.docker.compose.service": "caddy"}},
        },
        DockerSocketError("socket gone"),
    ]

    async def _inspect_side_effect(name: str) -> object:
        resp = inspect_responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return resp

    socket_client = _make_socket_client()
    socket_client.inspect_container = AsyncMock(side_effect=_inspect_side_effect)
    runner = _make_runner(repo, loader=_loader_with(config), socket_client=socket_client)
    # Wire a refresher so the branch is entered.
    refresher_called = {"n": 0}

    async def _fake_refresher(*, container_name: str, image_ref: str, image_id: str) -> None:
        refresher_called["n"] += 1  # pragma: no cover -- should not reach here

    runner.set_image_update_refresher(_fake_refresher)

    fake_proc = MagicMock()
    fake_proc.communicate = AsyncMock(return_value=(b"", b""))
    fake_proc.returncode = 0
    with patch(
        "homelab_monitor.kernel.docker.compose_action_runner.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=fake_proc),
    ):
        action_id = await runner.trigger_pull_and_restart(
            container_name="caddy", who="op", client_ip=None
        )
        await asyncio.gather(*runner._active_tasks, return_exceptions=True)  # pyright: ignore[reportPrivateUsage]

    # The action completes successfully; the recheck failure does not abort the action.
    row = await ComposeActionsRepository(repo).get_by_id(action_id)
    assert row is not None
    assert row.state == "success"
    # Refresher was NOT called because inspect failed before reaching it.
    assert refresher_called["n"] == 0


@pytest.mark.asyncio
async def test_trigger_image_recheck_config_not_dict_passes_empty_image_ref(
    repo: SqliteRepository, tmp_path: Path
) -> None:
    """_trigger_image_recheck calls refresher with empty image_ref when Config is not a dict."""
    compose_path = _write_compose(tmp_path, "services:\n  caddy:\n    image: caddy:latest\n")
    config = BuildSourcesConfig(
        compose_files=[
            ComposeFileEntry(host_path=str(compose_path), container_path=str(compose_path))
        ]
    )
    # First call (resolve_compose): normal labels.
    # Second call (_trigger_image_recheck): Config missing → image_ref stays "".
    inspect_responses: list[object] = [
        {
            "Id": "abc123",
            "Name": "/caddy",
            "Config": {"Labels": {"com.docker.compose.service": "caddy"}},
        },
        {
            "Id": "abc123",
            "Image": "sha256:newid",
            # Config intentionally absent → isinstance(config, dict) is False
        },
    ]

    async def _inspect_side_effect(name: str) -> object:
        return inspect_responses.pop(0)

    socket_client = _make_socket_client()
    socket_client.inspect_container = AsyncMock(side_effect=_inspect_side_effect)
    runner = _make_runner(repo, loader=_loader_with(config), socket_client=socket_client)

    refresher_args: dict[str, str] = {}

    async def _capture_refresher(*, container_name: str, image_ref: str, image_id: str) -> None:
        refresher_args["image_ref"] = image_ref
        refresher_args["image_id"] = image_id

    runner.set_image_update_refresher(_capture_refresher)

    fake_proc = MagicMock()
    fake_proc.communicate = AsyncMock(return_value=(b"", b""))
    fake_proc.returncode = 0
    with patch(
        "homelab_monitor.kernel.docker.compose_action_runner.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=fake_proc),
    ):
        await runner.trigger_pull_and_restart(container_name="caddy", who="op", client_ip=None)
        await asyncio.gather(*runner._active_tasks, return_exceptions=True)  # pyright: ignore[reportPrivateUsage]

    # image_ref is empty string when Config absent; image_id comes from top-level "Image".
    assert refresher_args.get("image_ref") == ""
    assert refresher_args.get("image_id") == "sha256:newid"


@pytest.mark.asyncio
async def test_trigger_image_recheck_extracts_image_ref_from_config(
    repo: SqliteRepository, tmp_path: Path
) -> None:
    """_trigger_image_recheck extracts image_ref from Config.Image when it is a string."""
    compose_path = _write_compose(tmp_path, "services:\n  nginx:\n    image: nginx:latest\n")
    config = BuildSourcesConfig(
        compose_files=[
            ComposeFileEntry(host_path=str(compose_path), container_path=str(compose_path))
        ]
    )
    # First call (resolve_compose): provides labels.
    # Second call (_trigger_image_recheck): Config.Image is a string → image_ref = "nginx:latest".
    inspect_responses: list[object] = [
        {
            "Id": "abc123",
            "Name": "/nginx",
            "Config": {"Labels": {"com.docker.compose.service": "nginx"}},
        },
        {
            "Id": "abc123",
            "Image": "sha256:somelongid",
            "Config": {"Image": "nginx:latest"},
        },
    ]

    async def _inspect_side_effect(name: str) -> object:
        return inspect_responses.pop(0)

    socket_client = _make_socket_client()
    socket_client.inspect_container = AsyncMock(side_effect=_inspect_side_effect)
    runner = _make_runner(repo, loader=_loader_with(config), socket_client=socket_client)

    refresher_args: dict[str, str] = {}

    async def _capture_refresher(*, container_name: str, image_ref: str, image_id: str) -> None:
        refresher_args["container_name"] = container_name
        refresher_args["image_ref"] = image_ref
        refresher_args["image_id"] = image_id

    runner.set_image_update_refresher(_capture_refresher)

    fake_proc = MagicMock()
    fake_proc.communicate = AsyncMock(return_value=(b"", b""))
    fake_proc.returncode = 0
    with patch(
        "homelab_monitor.kernel.docker.compose_action_runner.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=fake_proc),
    ):
        await runner.trigger_pull_and_restart(container_name="nginx", who="op", client_ip=None)
        await asyncio.gather(*runner._active_tasks, return_exceptions=True)  # pyright: ignore[reportPrivateUsage]

    assert refresher_args.get("container_name") == "nginx"
    assert refresher_args.get("image_ref") == "nginx:latest"
    assert refresher_args.get("image_id") == "sha256:somelongid"


@pytest.mark.asyncio
async def test_resolve_compose_extracts_project(repo: SqliteRepository, tmp_path: Path) -> None:
    """resolve_compose captures com.docker.compose.project label."""
    compose_path = _write_compose(
        tmp_path,
        "services:\n  foundry:\n    image: felddy/foundryvtt:latest\n",
    )
    config = BuildSourcesConfig(
        compose_files=[
            ComposeFileEntry(host_path=str(compose_path), container_path=str(compose_path))
        ]
    )
    socket_client = _make_socket_client()
    socket_client.inspect_container = AsyncMock(
        return_value={
            "Id": "def456",
            "Name": "/foundry",
            "Config": {
                "Labels": {
                    "com.docker.compose.service": "foundry",
                    "com.docker.compose.project": "compose",
                }
            },
        }
    )
    runner = _make_runner(repo, loader=_loader_with(config), socket_client=socket_client)
    resolved = await runner.resolve_compose("foundry")
    assert resolved is not None
    assert resolved.compose_project == "compose"


@pytest.mark.asyncio
async def test_resolve_compose_project_defaults_to_empty_string(
    repo: SqliteRepository, tmp_path: Path
) -> None:
    """resolve_compose sets compose_project='' when label is absent."""
    compose_path = _write_compose(
        tmp_path,
        "services:\n  caddy:\n    image: caddy:latest\n",
    )
    config = BuildSourcesConfig(
        compose_files=[
            ComposeFileEntry(host_path=str(compose_path), container_path=str(compose_path))
        ]
    )
    socket_client = _make_socket_client()
    socket_client.inspect_container = AsyncMock(
        return_value={
            "Id": "abc123",
            "Name": "/caddy",
            "Config": {"Labels": {"com.docker.compose.service": "caddy"}},
        }
    )
    runner = _make_runner(repo, loader=_loader_with(config), socket_client=socket_client)
    resolved = await runner.resolve_compose("caddy")
    assert resolved is not None
    assert resolved.compose_project == ""


@pytest.mark.asyncio
async def test_subprocess_uses_p_flag_when_project_present(
    repo: SqliteRepository, tmp_path: Path
) -> None:
    """When compose_project is set, -p <project> is passed to both subprocesses."""
    compose_path = _write_compose(
        tmp_path,
        "services:\n  foundry:\n    image: felddy/foundryvtt:latest\n",
    )
    config = BuildSourcesConfig(
        compose_files=[
            ComposeFileEntry(host_path=str(compose_path), container_path=str(compose_path))
        ]
    )
    socket_client = _make_socket_client()
    socket_client.inspect_container = AsyncMock(
        return_value={
            "Id": "def456",
            "Name": "/foundry",
            "Config": {
                "Labels": {
                    "com.docker.compose.service": "foundry",
                    "com.docker.compose.project": "compose",
                }
            },
        }
    )
    runner = _make_runner(repo, loader=_loader_with(config), socket_client=socket_client)

    fake_proc = MagicMock()
    fake_proc.communicate = AsyncMock(return_value=(b"ok\n", b""))
    fake_proc.returncode = 0
    with patch(
        "homelab_monitor.kernel.docker.compose_action_runner.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=fake_proc),
    ) as mock_spawn:
        action_id = await runner.trigger_pull_and_restart(
            container_name="foundry", who="alice", client_ip=None
        )
        await asyncio.gather(*runner._active_tasks, return_exceptions=True)  # pyright: ignore[reportPrivateUsage]

    expected_subprocess_calls = 2
    assert mock_spawn.call_count == expected_subprocess_calls
    pull_args: tuple[str, ...] = mock_spawn.call_args_list[0].args
    up_args: tuple[str, ...] = mock_spawn.call_args_list[1].args
    # Both calls must include -p compose immediately after 'compose'.
    assert list(pull_args[:4]) == ["docker", "compose", "-p", "compose"]
    assert list(up_args[:4]) == ["docker", "compose", "-p", "compose"]

    # Audit command string also includes -p flag.
    row = await ComposeActionsRepository(repo).get_by_id(action_id)
    assert row is not None
    assert "-p compose" in (row.command or "")


@pytest.mark.asyncio
async def test_subprocess_omits_p_flag_when_project_empty(
    repo: SqliteRepository, tmp_path: Path
) -> None:
    """When compose_project is '', no -p flag appears in subprocess argv."""
    compose_path = _write_compose(
        tmp_path,
        "services:\n  caddy:\n    image: caddy:latest\n",
    )
    config = BuildSourcesConfig(
        compose_files=[
            ComposeFileEntry(host_path=str(compose_path), container_path=str(compose_path))
        ]
    )
    socket_client = _make_socket_client()
    socket_client.inspect_container = AsyncMock(
        return_value={
            "Id": "abc123",
            "Name": "/caddy",
            "Config": {"Labels": {"com.docker.compose.service": "caddy"}},
        }
    )
    runner = _make_runner(repo, loader=_loader_with(config), socket_client=socket_client)

    fake_proc = MagicMock()
    fake_proc.communicate = AsyncMock(return_value=(b"ok\n", b""))
    fake_proc.returncode = 0
    with patch(
        "homelab_monitor.kernel.docker.compose_action_runner.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=fake_proc),
    ) as mock_spawn:
        await runner.trigger_pull_and_restart(container_name="caddy", who="bob", client_ip=None)
        await asyncio.gather(*runner._active_tasks, return_exceptions=True)  # pyright: ignore[reportPrivateUsage]

    pull_args: tuple[str, ...] = mock_spawn.call_args_list[0].args
    assert "-p" not in pull_args


@pytest.mark.asyncio
async def test_resolve_compose_marks_local_build(repo: SqliteRepository, tmp_path: Path) -> None:
    """resolve_compose returns is_local_build=True when service has build_context."""
    compose_path = _write_compose(
        tmp_path,
        "services:\n  myapp:\n    build:\n      context: ./app\n",
    )
    config = BuildSourcesConfig(
        compose_files=[
            ComposeFileEntry(host_path=str(compose_path), container_path=str(compose_path))
        ]
    )
    socket_client = _make_socket_client()
    socket_client.inspect_container = AsyncMock(
        return_value={
            "Id": "abc123",
            "Name": "/myapp",
            "Config": {
                "Labels": {
                    "com.docker.compose.service": "myapp",
                    "com.docker.compose.project": "test",
                }
            },
        }
    )
    runner = _make_runner(repo, loader=_loader_with(config), socket_client=socket_client)
    resolved = await runner.resolve_compose("myapp")
    assert resolved is not None
    assert resolved.is_local_build is True


@pytest.mark.asyncio
async def test_resolve_compose_marks_remote(repo: SqliteRepository, tmp_path: Path) -> None:
    """resolve_compose returns is_local_build=False when service has image."""
    compose_path = _write_compose(
        tmp_path,
        "services:\n  caddy:\n    image: caddy:latest\n",
    )
    config = BuildSourcesConfig(
        compose_files=[
            ComposeFileEntry(host_path=str(compose_path), container_path=str(compose_path))
        ]
    )
    socket_client = _make_socket_client()
    socket_client.inspect_container = AsyncMock(
        return_value={
            "Id": "abc123",
            "Name": "/caddy",
            "Config": {
                "Labels": {
                    "com.docker.compose.service": "caddy",
                }
            },
        }
    )
    runner = _make_runner(repo, loader=_loader_with(config), socket_client=socket_client)
    resolved = await runner.resolve_compose("caddy")
    assert resolved is not None
    assert resolved.is_local_build is False


@pytest.mark.asyncio
async def test_first_then_up_uses_build_for_local(repo: SqliteRepository, tmp_path: Path) -> None:
    """_run_first_then_up uses 'build' verb when is_local_build=True."""
    compose_path = _write_compose(
        tmp_path,
        "services:\n  myapp:\n    build:\n      context: ./app\n",
    )
    config = BuildSourcesConfig(
        compose_files=[
            ComposeFileEntry(host_path=str(compose_path), container_path=str(compose_path))
        ]
    )
    socket_client = _make_socket_client()
    socket_client.inspect_container = AsyncMock(
        return_value={
            "Id": "abc123",
            "Name": "/myapp",
            "Config": {"Labels": {"com.docker.compose.service": "myapp"}},
        }
    )
    runner = _make_runner(repo, loader=_loader_with(config), socket_client=socket_client)

    fake_proc = MagicMock()
    fake_proc.communicate = AsyncMock(return_value=(b"built\n", b""))
    fake_proc.returncode = 0
    with patch(
        "homelab_monitor.kernel.docker.compose_action_runner.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=fake_proc),
    ) as mock_spawn:
        action_id = await runner.trigger_pull_and_restart(
            container_name="myapp", who="alice", client_ip="10.0.0.1"
        )
        await asyncio.gather(*runner._active_tasks, return_exceptions=True)  # pyright: ignore[reportPrivateUsage]

    # Check that 'build' was used instead of 'pull'
    expected_subprocess_calls = 2
    assert mock_spawn.call_count == expected_subprocess_calls
    build_args: tuple[str, ...] = mock_spawn.call_args_list[0].args
    assert "build" in build_args
    assert "pull" not in build_args

    # Check that initial state was "building"
    row = await ComposeActionsRepository(repo).get_by_id(action_id)
    assert row is not None
    assert row.state == "success"


@pytest.mark.asyncio
async def test_first_then_up_uses_pull_for_remote(repo: SqliteRepository, tmp_path: Path) -> None:
    """_run_first_then_up uses 'pull' verb when is_local_build=False."""
    compose_path = _write_compose(
        tmp_path,
        "services:\n  caddy:\n    image: caddy:latest\n",
    )
    config = BuildSourcesConfig(
        compose_files=[
            ComposeFileEntry(host_path=str(compose_path), container_path=str(compose_path))
        ]
    )
    socket_client = _make_socket_client()
    socket_client.inspect_container = AsyncMock(
        return_value={
            "Id": "abc123",
            "Name": "/caddy",
            "Config": {"Labels": {"com.docker.compose.service": "caddy"}},
        }
    )
    runner = _make_runner(repo, loader=_loader_with(config), socket_client=socket_client)

    fake_proc = MagicMock()
    fake_proc.communicate = AsyncMock(return_value=(b"pulled\n", b""))
    fake_proc.returncode = 0
    with patch(
        "homelab_monitor.kernel.docker.compose_action_runner.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=fake_proc),
    ) as mock_spawn:
        await runner.trigger_pull_and_restart(
            container_name="caddy", who="bob", client_ip="10.0.0.2"
        )
        await asyncio.gather(*runner._active_tasks, return_exceptions=True)  # pyright: ignore[reportPrivateUsage]

    # Check that 'pull' was used
    expected_subprocess_calls = 2
    assert mock_spawn.call_count == expected_subprocess_calls
    pull_args: tuple[str, ...] = mock_spawn.call_args_list[0].args
    assert "pull" in pull_args
    assert "build" not in pull_args


@pytest.mark.asyncio
async def test_calls_local_build_refresher_on_success_when_is_local_build_true(
    repo: SqliteRepository, tmp_path: Path
) -> None:
    """T4: calls local_build_refresher on success when is_local_build=True."""
    compose_path = _write_compose(
        tmp_path,
        textwrap.dedent("""
            services:
              myapp:
                build: .
        """),
    )
    config = BuildSourcesConfig(
        compose_files=[
            ComposeFileEntry(host_path=str(compose_path), container_path=str(compose_path))
        ]
    )
    socket_client = _make_socket_client()
    socket_client.inspect_container = AsyncMock(
        return_value={
            "Id": "abc123",
            "Name": "/myapp",
            "Config": {"Labels": {"com.docker.compose.service": "myapp"}},
        }
    )
    runner = _make_runner(repo, loader=_loader_with(config), socket_client=socket_client)

    # Mock the local_build_refresher
    mock_local_build_refresher = AsyncMock()
    runner.set_local_build_refresher(mock_local_build_refresher)

    # Also set image_update_refresher to verify it's NOT called
    mock_image_refresher = AsyncMock()
    runner.set_image_update_refresher(mock_image_refresher)

    fake_proc = MagicMock()
    fake_proc.communicate = AsyncMock(return_value=(b"built\n", b""))
    fake_proc.returncode = 0
    with patch(
        "homelab_monitor.kernel.docker.compose_action_runner.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=fake_proc),
    ):
        await runner.trigger_pull_and_restart(
            container_name="myapp", who="bob", client_ip="10.0.0.2"
        )
        await asyncio.gather(*runner._active_tasks, return_exceptions=True)  # pyright: ignore[reportPrivateUsage]

    # Assert: local_build_refresher was called, image_update_refresher was NOT
    mock_local_build_refresher.assert_called_once_with(container_name="myapp")
    mock_image_refresher.assert_not_called()


@pytest.mark.asyncio
async def test_calls_image_update_refresher_on_success_when_is_local_build_false(
    repo: SqliteRepository, tmp_path: Path
) -> None:
    """T5: calls image_update_refresher on success when is_local_build=False."""
    compose_path = _write_compose(
        tmp_path,
        "services:\n  caddy:\n    image: caddy:latest\n",
    )
    config = BuildSourcesConfig(
        compose_files=[
            ComposeFileEntry(host_path=str(compose_path), container_path=str(compose_path))
        ]
    )
    socket_client = _make_socket_client()
    socket_client.inspect_container = AsyncMock(
        return_value={
            "Id": "abc123",
            "Name": "/caddy",
            "Config": {
                "Labels": {"com.docker.compose.service": "caddy"},
                "Image": "caddy:latest",
            },
            "Image": "sha256:imageid",
        }
    )
    runner = _make_runner(repo, loader=_loader_with(config), socket_client=socket_client)

    # Mock both refreshers
    mock_local_build_refresher = AsyncMock()
    runner.set_local_build_refresher(mock_local_build_refresher)

    mock_image_refresher = AsyncMock()
    runner.set_image_update_refresher(mock_image_refresher)

    fake_proc = MagicMock()
    fake_proc.communicate = AsyncMock(return_value=(b"pulled\n", b""))
    fake_proc.returncode = 0
    with patch(
        "homelab_monitor.kernel.docker.compose_action_runner.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=fake_proc),
    ):
        await runner.trigger_pull_and_restart(
            container_name="caddy", who="bob", client_ip="10.0.0.2"
        )
        await asyncio.gather(*runner._active_tasks, return_exceptions=True)  # pyright: ignore[reportPrivateUsage]

    # Assert: image_update_refresher was called, local_build_refresher was NOT
    mock_image_refresher.assert_called_once()
    mock_local_build_refresher.assert_not_called()


@pytest.mark.asyncio
async def test_neither_refresher_called_on_failure(repo: SqliteRepository, tmp_path: Path) -> None:
    """T6: neither refresher called on failure."""
    compose_path = _write_compose(
        tmp_path,
        textwrap.dedent("""
            services:
              myapp:
                build: .
        """),
    )
    config = BuildSourcesConfig(
        compose_files=[
            ComposeFileEntry(host_path=str(compose_path), container_path=str(compose_path))
        ]
    )
    socket_client = _make_socket_client()
    socket_client.inspect_container = AsyncMock(
        return_value={
            "Id": "abc123",
            "Name": "/myapp",
            "Config": {"Labels": {"com.docker.compose.service": "myapp"}},
        }
    )
    runner = _make_runner(repo, loader=_loader_with(config), socket_client=socket_client)

    # Mock both refreshers
    mock_local_build_refresher = AsyncMock()
    runner.set_local_build_refresher(mock_local_build_refresher)

    mock_image_refresher = AsyncMock()
    runner.set_image_update_refresher(mock_image_refresher)

    fake_proc = MagicMock()
    fake_proc.communicate = AsyncMock(return_value=(b"", b"build failed\n"))
    fake_proc.returncode = 1  # Failure
    with patch(
        "homelab_monitor.kernel.docker.compose_action_runner.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=fake_proc),
    ):
        await runner.trigger_pull_and_restart(
            container_name="myapp", who="bob", client_ip="10.0.0.2"
        )
        await asyncio.gather(*runner._active_tasks, return_exceptions=True)  # pyright: ignore[reportPrivateUsage]

    # Assert: neither refresher was called
    mock_local_build_refresher.assert_not_called()
    mock_image_refresher.assert_not_called()
