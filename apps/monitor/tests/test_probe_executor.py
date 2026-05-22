"""Tests for probe executor functions."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import httpx
import pytest
from pytest_httpx import HTTPXMock

from homelab_monitor.kernel.docker.probe_executor import (
    execute_exec,
    execute_http,
    execute_metrics,
    execute_resolved_probe,
    execute_tcp,
)
from homelab_monitor.kernel.docker.probe_resolver import ResolvedProbe
from homelab_monitor.kernel.docker.socket_client import (
    DockerSocketClient,
    DockerSocketError,
)

# ============================================================================
# execute_http tests
# ============================================================================


@pytest.mark.asyncio
async def test_execute_http_200_ok(httpx_mock: HTTPXMock) -> None:
    """HTTP 200 → up=True, error=None."""
    httpx_mock.add_response(method="GET", status_code=200)
    async with httpx.AsyncClient() as client:
        result = await execute_http(client, "http://example.com/health", 5.0)
    assert result.up is True
    assert result.error is None
    assert result.duration_seconds >= 0.0


@pytest.mark.asyncio
async def test_execute_http_299_ok(httpx_mock: HTTPXMock) -> None:
    """HTTP 299 → up=True."""
    httpx_mock.add_response(method="GET", status_code=299)
    async with httpx.AsyncClient() as client:
        result = await execute_http(client, "http://example.com/health", 5.0)
    assert result.up is True


@pytest.mark.asyncio
async def test_execute_http_301_ok(httpx_mock: HTTPXMock) -> None:
    """HTTP 301 (redirect) → up=True."""
    httpx_mock.add_response(method="GET", status_code=301)
    async with httpx.AsyncClient() as client:
        result = await execute_http(client, "http://example.com/health", 5.0)
    assert result.up is True


@pytest.mark.asyncio
async def test_execute_http_399_ok(httpx_mock: HTTPXMock) -> None:
    """HTTP 399 → up=True."""
    httpx_mock.add_response(method="GET", status_code=399)
    async with httpx.AsyncClient() as client:
        result = await execute_http(client, "http://example.com/health", 5.0)
    assert result.up is True


@pytest.mark.asyncio
async def test_execute_http_400_fail(httpx_mock: HTTPXMock) -> None:
    """HTTP 400 → up=False, error contains status."""
    httpx_mock.add_response(method="GET", status_code=400)
    async with httpx.AsyncClient() as client:
        result = await execute_http(client, "http://example.com/health", 5.0)
    assert result.up is False
    assert result.error is not None
    assert "http_status_400" in result.error


@pytest.mark.asyncio
async def test_execute_http_500_fail(httpx_mock: HTTPXMock) -> None:
    """HTTP 500 → up=False."""
    httpx_mock.add_response(method="GET", status_code=500)
    async with httpx.AsyncClient() as client:
        result = await execute_http(client, "http://example.com/health", 5.0)
    assert result.up is False
    assert result.error is not None
    assert "http_status_500" in result.error


@pytest.mark.asyncio
async def test_execute_http_timeout() -> None:
    """Timeout → up=False, error contains 'timeout'."""
    async with httpx.AsyncClient() as client:
        # Mock raises TimeoutException
        client.get = AsyncMock(side_effect=httpx.TimeoutException("timeout"))
        result = await execute_http(client, "http://example.com/health", 0.001)
    assert result.up is False
    assert result.error is not None
    assert "timeout" in result.error


@pytest.mark.asyncio
async def test_execute_http_connection_error(httpx_mock: HTTPXMock) -> None:
    """ConnectError → up=False, error contains 'http_error'."""
    async with httpx.AsyncClient() as client:
        client.get = AsyncMock(side_effect=httpx.ConnectError("connection refused"))
        result = await execute_http(client, "http://example.com/health", 5.0)
    assert result.up is False
    assert result.error is not None
    assert "http_error" in result.error


@pytest.mark.asyncio
async def test_execute_http_duration_recorded(httpx_mock: HTTPXMock) -> None:
    """Duration is recorded."""
    httpx_mock.add_response(method="GET", status_code=200)
    async with httpx.AsyncClient() as client:
        result = await execute_http(client, "http://example.com/health", 5.0)
    assert result.duration_seconds >= 0.0


# ============================================================================
# execute_tcp tests
# ============================================================================


@pytest.mark.asyncio
async def test_execute_tcp_success() -> None:
    """TCP connection succeeds → up=True."""

    @asynccontextmanager
    async def run_tcp_server() -> AsyncGenerator[str, None]:
        server = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
        addr = server.sockets[0].getsockname()
        port = addr[1]
        task = asyncio.create_task(server.serve_forever())
        try:
            yield f"127.0.0.1:{port}"
        finally:
            server.close()
            task.cancel()

    async with run_tcp_server() as target:
        result = await execute_tcp(target, 5.0)

    assert result.up is True
    assert result.error is None


@pytest.mark.asyncio
async def test_execute_tcp_connection_refused() -> None:
    """Port with nothing listening → connection_error."""
    # Use an unlikely port
    result = await execute_tcp("127.0.0.1:9", 0.5)
    assert result.up is False
    assert result.error is not None
    assert "connection_error" in result.error


@pytest.mark.asyncio
async def test_execute_tcp_timeout() -> None:
    """Timeout on connection → up=False, error='timeout'."""
    # Point at a non-routable IP with low timeout
    result = await execute_tcp("192.0.2.1:8080", 0.1)
    assert result.up is False
    assert result.error == "timeout"


@pytest.mark.asyncio
async def test_execute_tcp_malformed_target() -> None:
    """Target without colon → malformed_target error."""
    result = await execute_tcp("127.0.0.1", 5.0)
    assert result.up is False
    assert result.error is not None
    assert "malformed_target" in result.error


@pytest.mark.asyncio
async def test_execute_tcp_malformed_port() -> None:
    """Target with non-numeric port → malformed_target error."""
    result = await execute_tcp("127.0.0.1:notaport", 5.0)
    assert result.up is False
    assert result.error is not None
    assert "malformed_target" in result.error


# ============================================================================
# execute_exec tests
# ============================================================================


@pytest.mark.asyncio
async def test_execute_exec_zero_exit() -> None:
    """Exec returns 0 → up=True."""
    mock_socket = AsyncMock(spec=DockerSocketClient)
    mock_socket.exec_in_container = AsyncMock(return_value=0)

    result = await execute_exec(mock_socket, "abc123", "true", 5.0)

    assert result.up is True
    assert result.error is None
    mock_socket.exec_in_container.assert_called_once_with(container_id="abc123", cmd="true")


@pytest.mark.asyncio
async def test_execute_exec_nonzero_exit() -> None:
    """Exec returns 2 → up=False, error contains exit_code."""
    mock_socket = AsyncMock(spec=DockerSocketClient)
    mock_socket.exec_in_container = AsyncMock(return_value=2)

    result = await execute_exec(mock_socket, "abc123", "false", 5.0)

    assert result.up is False
    assert result.error is not None
    assert "exit_code_2" in result.error


@pytest.mark.asyncio
async def test_execute_exec_timeout() -> None:
    """Exec timeout → up=False, error='timeout'."""
    mock_socket = AsyncMock(spec=DockerSocketClient)

    async def slow_exec(*args: object, **kwargs: object) -> None:
        await asyncio.sleep(10)

    mock_socket.exec_in_container = slow_exec

    result = await execute_exec(mock_socket, "abc123", "slow_cmd", 0.05)

    assert result.up is False
    assert result.error == "timeout"


@pytest.mark.asyncio
async def test_execute_exec_docker_error() -> None:
    """Docker socket error → up=False."""
    mock_socket = AsyncMock(spec=DockerSocketClient)
    mock_socket.exec_in_container = AsyncMock(side_effect=DockerSocketError("socket error"))

    result = await execute_exec(mock_socket, "abc123", "cmd", 5.0)

    assert result.up is False
    assert result.error is not None
    assert "docker_error" in result.error


@pytest.mark.asyncio
async def test_execute_exec_rejects_overlong_cmd() -> None:
    """Cmd longer than 4096 chars → up=False, error='cmd_too_long', no socket call."""
    mock_socket = AsyncMock(spec=DockerSocketClient)
    cmd = "x" * 5000

    result = await execute_exec(mock_socket, "abc123", cmd, 5.0)

    assert result.up is False
    assert result.error == "cmd_too_long"
    assert result.duration_seconds == 0.0
    mock_socket.exec_in_container.assert_not_called()


@pytest.mark.asyncio
async def test_execute_exec_rejects_newline_in_cmd() -> None:
    """Cmd with newline → up=False, error='cmd_invalid_chars'."""
    mock_socket = AsyncMock(spec=DockerSocketClient)
    cmd = "echo a\necho b"

    result = await execute_exec(mock_socket, "abc123", cmd, 5.0)

    assert result.up is False
    assert result.error == "cmd_invalid_chars"
    assert result.duration_seconds == 0.0
    mock_socket.exec_in_container.assert_not_called()


@pytest.mark.asyncio
async def test_execute_exec_rejects_null_byte_in_cmd() -> None:
    """Cmd with null byte → up=False, error='cmd_invalid_chars'."""
    mock_socket = AsyncMock(spec=DockerSocketClient)
    cmd = "echo a\x00b"

    result = await execute_exec(mock_socket, "abc123", cmd, 5.0)

    assert result.up is False
    assert result.error == "cmd_invalid_chars"
    assert result.duration_seconds == 0.0
    mock_socket.exec_in_container.assert_not_called()


# ============================================================================
# execute_metrics tests
# ============================================================================


@pytest.mark.asyncio
async def test_execute_metrics_valid_exposition(httpx_mock: HTTPXMock) -> None:
    """Valid Prometheus exposition → up=True."""
    body = "# HELP foo_count Counter\nfoo_count 5\n"
    httpx_mock.add_response(method="GET", status_code=200, text=body)
    async with httpx.AsyncClient() as client:
        result = await execute_metrics(client, "http://example.com/metrics", 5.0)
    assert result.up is True
    assert result.error is None


@pytest.mark.asyncio
async def test_execute_metrics_valid_with_labels(httpx_mock: HTTPXMock) -> None:
    """Metrics with labels → up=True."""
    body = 'foo_count{label="x"} 5\n'
    httpx_mock.add_response(method="GET", status_code=200, text=body)
    async with httpx.AsyncClient() as client:
        result = await execute_metrics(client, "http://example.com/metrics", 5.0)
    assert result.up is True


@pytest.mark.asyncio
async def test_execute_metrics_only_comments(httpx_mock: HTTPXMock) -> None:
    """Only comment lines → up=False, error='no_metric_samples'."""
    body = "# HELP foo Counter\n# TYPE foo counter\n"
    httpx_mock.add_response(method="GET", status_code=200, text=body)
    async with httpx.AsyncClient() as client:
        result = await execute_metrics(client, "http://example.com/metrics", 5.0)
    assert result.up is False
    assert result.error == "no_metric_samples"


@pytest.mark.asyncio
async def test_execute_metrics_empty_body(httpx_mock: HTTPXMock) -> None:
    """Empty body → up=False, error='empty_body'."""
    httpx_mock.add_response(method="GET", status_code=200, text="")
    async with httpx.AsyncClient() as client:
        result = await execute_metrics(client, "http://example.com/metrics", 5.0)
    assert result.up is False
    assert result.error == "empty_body"


@pytest.mark.asyncio
async def test_execute_metrics_http_failure(httpx_mock: HTTPXMock) -> None:
    """HTTP 500 → up=False, error contains status."""
    httpx_mock.add_response(method="GET", status_code=500)
    async with httpx.AsyncClient() as client:
        result = await execute_metrics(client, "http://example.com/metrics", 5.0)
    assert result.up is False
    assert result.error is not None
    assert "http_status_500" in result.error


@pytest.mark.asyncio
async def test_execute_metrics_valid_with_negative_value(httpx_mock: HTTPXMock) -> None:
    """Negative metric value → up=True."""
    body = "foo_gauge -1.5\n"
    httpx_mock.add_response(method="GET", status_code=200, text=body)
    async with httpx.AsyncClient() as client:
        result = await execute_metrics(client, "http://example.com/metrics", 5.0)
    assert result.up is True


@pytest.mark.asyncio
async def test_execute_metrics_valid_with_scientific_notation(httpx_mock: HTTPXMock) -> None:
    """Scientific notation → up=True."""
    body = "foo_gauge 1.5e-10\n"
    httpx_mock.add_response(method="GET", status_code=200, text=body)
    async with httpx.AsyncClient() as client:
        result = await execute_metrics(client, "http://example.com/metrics", 5.0)
    assert result.up is True


# ============================================================================
# execute_resolved_probe tests
# ============================================================================


@pytest.mark.asyncio
async def test_execute_resolved_probe_http(httpx_mock: HTTPXMock) -> None:
    """Dispatch to execute_http."""
    httpx_mock.add_response(method="GET", status_code=200)
    probe = ResolvedProbe(
        kind="http",
        name="api",
        target="http://example.com/health",
        exec_cmd=None,
        container_id=None,
    )
    async with httpx.AsyncClient() as client:
        result = await execute_resolved_probe(
            probe, http_client=client, socket_client=None, timeout_seconds=5.0
        )
    assert result.up is True


@pytest.mark.asyncio
async def test_execute_resolved_probe_metrics(httpx_mock: HTTPXMock) -> None:
    """Dispatch to execute_metrics."""
    body = "foo 5\n"
    httpx_mock.add_response(method="GET", status_code=200, text=body)
    probe = ResolvedProbe(
        kind="metrics",
        name="prometheus",
        target="http://example.com/metrics",
        exec_cmd=None,
        container_id=None,
    )
    async with httpx.AsyncClient() as client:
        result = await execute_resolved_probe(
            probe, http_client=client, socket_client=None, timeout_seconds=5.0
        )
    assert result.up is True


@pytest.mark.asyncio
async def test_execute_resolved_probe_tcp() -> None:
    """Dispatch to execute_tcp."""
    probe = ResolvedProbe(
        kind="tcp",
        name="db",
        target="127.0.0.1:9",
        exec_cmd=None,
        container_id=None,
    )
    async with httpx.AsyncClient() as client:
        result = await execute_resolved_probe(
            probe, http_client=client, socket_client=None, timeout_seconds=0.5
        )
    # Should fail due to refused connection
    assert result.up is False


@pytest.mark.asyncio
async def test_execute_resolved_probe_exec() -> None:
    """Dispatch to execute_exec."""
    mock_socket = AsyncMock(spec=DockerSocketClient)
    mock_socket.exec_in_container = AsyncMock(return_value=0)

    probe = ResolvedProbe(
        kind="exec",
        name="test_probe",
        target="",
        exec_cmd="true",
        container_id="abc123",
    )
    async with httpx.AsyncClient() as client:
        result = await execute_resolved_probe(
            probe, http_client=client, socket_client=mock_socket, timeout_seconds=5.0
        )
    assert result.up is True


@pytest.mark.asyncio
async def test_execute_resolved_probe_exec_unconfigured() -> None:
    """Exec without socket_client → exec_unconfigured."""
    probe = ResolvedProbe(
        kind="exec",
        name="test_probe",
        target="",
        exec_cmd="true",
        container_id="abc123",
    )
    async with httpx.AsyncClient() as client:
        result = await execute_resolved_probe(
            probe, http_client=client, socket_client=None, timeout_seconds=5.0
        )
    assert result.up is False
    assert result.error == "exec_unconfigured"


@pytest.mark.asyncio
async def test_execute_resolved_probe_unknown_kind(httpx_mock: HTTPXMock) -> None:
    """Unknown kind → error."""
    probe = ResolvedProbe(
        kind="unknown_kind",
        name="test",
        target="",
        exec_cmd=None,
        container_id=None,
    )
    async with httpx.AsyncClient() as client:
        result = await execute_resolved_probe(
            probe, http_client=client, socket_client=None, timeout_seconds=5.0
        )
    assert result.up is False
    assert result.error is not None
    assert "unknown_kind" in result.error


@pytest.mark.asyncio
async def test_execute_tcp_close_error_does_not_fail_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """If wait_closed() raises OSError/ConnectionError, the probe still reports up=True.

    Close-time errors are not probe failures (lines 78-79).
    """
    from homelab_monitor.kernel.docker import probe_executor  # noqa: PLC0415

    class _FakeWriter:
        def close(self) -> None:
            pass  # close() succeeds

        async def wait_closed(self) -> None:
            raise OSError("simulated wait-close failure")

    class _FakeReader:
        pass

    async def fake_open_connection(host: str, port: int) -> tuple[object, object]:
        return _FakeReader(), _FakeWriter()

    monkeypatch.setattr(probe_executor.asyncio, "open_connection", fake_open_connection)

    result = await probe_executor.execute_tcp("127.0.0.1:9999", timeout_seconds=1.0)

    assert result.up is True
    assert result.error is None


@pytest.mark.asyncio
async def test_execute_http_generic_http_error_caught() -> None:
    """HTTPError (non-timeout) → up=False, error includes exception class name."""
    import httpx as httpx_lib  # noqa: PLC0415

    from homelab_monitor.kernel.docker.probe_executor import execute_http  # noqa: PLC0415

    async with httpx_lib.AsyncClient() as client:
        # Mock with ProtocolError which is an HTTPError but not TimeoutException
        client.get = AsyncMock(side_effect=httpx_lib.ProtocolError("generic protocol error"))
        result = await execute_http(client, "http://example.com/health", timeout_seconds=1.0)

    assert result.up is False
    assert result.error is not None
    assert "http_error" in result.error.lower()


@pytest.mark.asyncio
async def test_execute_metrics_timeout_exception_returns_timeout_error(
    httpx_mock: HTTPXMock,
) -> None:
    """TimeoutException during GET → up=False, error='timeout'."""
    import httpx as httpx_lib  # noqa: PLC0415

    from homelab_monitor.kernel.docker.probe_executor import execute_metrics  # noqa: PLC0415

    httpx_mock.add_exception(  # pyright: ignore[reportUnknownMemberType]
        httpx_lib.TimeoutException("simulated timeout"),
        method="GET",
        url="http://example.com/metrics",
    )

    async with httpx_lib.AsyncClient() as client:
        result = await execute_metrics(client, "http://example.com/metrics", timeout_seconds=1.0)

    assert result.up is False
    assert result.error == "timeout"


@pytest.mark.asyncio
async def test_execute_metrics_generic_http_error_returns_http_error(
    httpx_mock: HTTPXMock,
) -> None:
    """HTTPError (non-timeout) during GET → up=False, error startswith 'http_error'."""
    import httpx as httpx_lib  # noqa: PLC0415

    from homelab_monitor.kernel.docker.probe_executor import execute_metrics  # noqa: PLC0415

    httpx_mock.add_exception(  # pyright: ignore[reportUnknownMemberType]
        httpx_lib.ConnectError("simulated connect error"),
        method="GET",
        url="http://example.com/metrics",
    )

    async with httpx_lib.AsyncClient() as client:
        result = await execute_metrics(client, "http://example.com/metrics", timeout_seconds=1.0)

    assert result.up is False
    assert result.error is not None
    assert result.error.startswith("http_error:")
