"""Tests for :class:`DockerSocketClient`."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import structlog

from homelab_monitor.kernel.docker.socket_client import (
    DockerSocketClient,
    DockerSocketConnectionError,
    DockerSocketProtocolError,
)


@pytest.mark.asyncio
async def test_list_containers_happy_path() -> None:
    """list_containers returns parsed list on 200 + JSON list."""
    log = structlog.get_logger()

    # Mock the internal httpx client
    mock_response = AsyncMock()
    mock_response.status_code = 200
    mock_response.json = MagicMock(
        return_value=[
            {
                "Id": "abc123",
                "Names": ["/foo"],
                "Image": "nginx:latest",
                "ImageID": "sha256:xxx",
                "State": "running",
                "Status": "Up 3 hours",
                "Labels": {},
            }
        ]
    )

    mock_http = AsyncMock(spec=httpx.AsyncClient)
    mock_http.get.return_value = mock_response
    client = DockerSocketClient(socket_path="/var/run/docker.sock", log=log, httpx_client=mock_http)

    result = await client.list_containers()

    assert len(result) == 1
    assert result[0]["Id"] == "abc123"
    assert result[0]["Names"] == ["/foo"]
    await client.aclose()


@pytest.mark.asyncio
async def test_inspect_container_happy_path() -> None:
    """inspect_container returns dict for status 200 + JSON dict."""
    log = structlog.get_logger()

    mock_response = AsyncMock()
    mock_response.status_code = 200
    mock_response.json = MagicMock(
        return_value={
            "Id": "abc123",
            "Name": "/foo",
            "Image": "nginx:latest",
            "State": {"Status": "running", "Running": True, "ExitCode": 0},
            "RestartCount": 0,
            "HostConfig": {"NetworkMode": "bridge"},
        }
    )

    mock_http = AsyncMock(spec=httpx.AsyncClient)
    mock_http.get.return_value = mock_response
    client = DockerSocketClient(socket_path="/var/run/docker.sock", log=log, httpx_client=mock_http)

    result = await client.inspect_container("abc123")

    assert result["Id"] == "abc123"
    assert result["Name"] == "/foo"
    assert result["State"].get("Running") is True
    await client.aclose()


@pytest.mark.asyncio
async def test_list_containers_connection_refused_raises() -> None:
    """ConnectError -> DockerSocketConnectionError with socket path in message."""
    log = structlog.get_logger()
    socket_path = "/var/run/docker.sock"

    mock_http = AsyncMock(spec=httpx.AsyncClient)
    mock_http.get.side_effect = httpx.ConnectError("Connection refused")
    client = DockerSocketClient(socket_path=socket_path, log=log, httpx_client=mock_http)

    with pytest.raises(DockerSocketConnectionError) as exc_info:
        await client.list_containers()

    assert socket_path in str(exc_info.value)

    await client.aclose()


@pytest.mark.asyncio
async def test_list_containers_non_200_raises_protocol_error() -> None:
    """Status 500 raises DockerSocketProtocolError with status in message."""
    log = structlog.get_logger()

    mock_response = AsyncMock()
    mock_response.status_code = 500
    mock_response.text = "Internal server error"

    mock_http = AsyncMock(spec=httpx.AsyncClient)
    mock_http.get.return_value = mock_response
    client = DockerSocketClient(socket_path="/var/run/docker.sock", log=log, httpx_client=mock_http)

    with pytest.raises(DockerSocketProtocolError) as exc_info:
        await client.list_containers()

    assert "500" in str(exc_info.value)

    await client.aclose()


@pytest.mark.asyncio
async def test_list_containers_malformed_json_raises_protocol_error() -> None:
    """When resp.json() raises JSONDecodeError -> DockerSocketProtocolError."""
    log = structlog.get_logger()

    mock_response = AsyncMock()
    mock_response.status_code = 200
    mock_response.json = MagicMock(
        side_effect=json.JSONDecodeError("Expecting value", doc="{bad json", pos=0)
    )

    mock_http = AsyncMock(spec=httpx.AsyncClient)
    mock_http.get.return_value = mock_response
    client = DockerSocketClient(socket_path="/var/run/docker.sock", log=log, httpx_client=mock_http)

    with pytest.raises(DockerSocketProtocolError) as exc_info:
        await client.list_containers()

    assert "malformed JSON" in str(exc_info.value)

    await client.aclose()


@pytest.mark.asyncio
async def test_list_containers_non_list_payload_raises() -> None:
    """When server returns dict (not list) -> DockerSocketProtocolError."""
    log = structlog.get_logger()

    mock_response = AsyncMock()
    mock_response.status_code = 200
    mock_response.json = MagicMock(return_value={"error": "something went wrong"})

    mock_http = AsyncMock(spec=httpx.AsyncClient)
    mock_http.get.return_value = mock_response
    client = DockerSocketClient(socket_path="/var/run/docker.sock", log=log, httpx_client=mock_http)

    with pytest.raises(DockerSocketProtocolError) as exc_info:
        await client.list_containers()

    assert "expected list" in str(exc_info.value)

    await client.aclose()


@pytest.mark.asyncio
async def test_aclose_closes_underlying_httpx_client() -> None:
    """aclose() awaits the inner httpx.AsyncClient.aclose."""
    log = structlog.get_logger()

    mock_http = AsyncMock(spec=httpx.AsyncClient)
    client = DockerSocketClient(socket_path="/var/run/docker.sock", log=log, httpx_client=mock_http)

    await client.aclose()
    mock_http.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_list_containers_http_error_raises_connection_error() -> None:
    """Generic httpx.HTTPError -> DockerSocketConnectionError."""
    log = structlog.get_logger()

    mock_http = AsyncMock(spec=httpx.AsyncClient)
    mock_http.get.side_effect = httpx.HTTPError("timeout")
    client = DockerSocketClient(socket_path="/var/run/docker.sock", log=log, httpx_client=mock_http)

    with pytest.raises(DockerSocketConnectionError) as exc_info:
        await client.list_containers()

    assert "transport error" in str(exc_info.value)
    await client.aclose()


@pytest.mark.asyncio
async def test_inspect_container_connection_refused_raises() -> None:
    """ConnectError in inspect_container -> DockerSocketConnectionError."""
    log = structlog.get_logger()

    mock_http = AsyncMock(spec=httpx.AsyncClient)
    mock_http.get.side_effect = httpx.ConnectError("refused")
    client = DockerSocketClient(socket_path="/var/run/docker.sock", log=log, httpx_client=mock_http)

    with pytest.raises(DockerSocketConnectionError) as exc_info:
        await client.inspect_container("abc123")

    assert "/var/run/docker.sock" in str(exc_info.value)
    await client.aclose()


@pytest.mark.asyncio
async def test_inspect_container_http_error_raises_connection_error() -> None:
    """Generic httpx.HTTPError in inspect_container -> DockerSocketConnectionError."""
    log = structlog.get_logger()

    mock_http = AsyncMock(spec=httpx.AsyncClient)
    mock_http.get.side_effect = httpx.HTTPError("timeout")
    client = DockerSocketClient(socket_path="/var/run/docker.sock", log=log, httpx_client=mock_http)

    with pytest.raises(DockerSocketConnectionError) as exc_info:
        await client.inspect_container("abc123")

    assert "transport error" in str(exc_info.value)
    await client.aclose()


@pytest.mark.asyncio
async def test_inspect_container_non_200_raises_protocol_error() -> None:
    """Non-200 from inspect endpoint -> DockerSocketProtocolError."""
    log = structlog.get_logger()

    mock_response = AsyncMock()
    mock_response.status_code = 404
    mock_response.text = "No such container"

    mock_http = AsyncMock(spec=httpx.AsyncClient)
    mock_http.get.return_value = mock_response
    client = DockerSocketClient(socket_path="/var/run/docker.sock", log=log, httpx_client=mock_http)

    with pytest.raises(DockerSocketProtocolError) as exc_info:
        await client.inspect_container("abc123")

    assert "404" in str(exc_info.value)
    await client.aclose()


@pytest.mark.asyncio
async def test_inspect_container_malformed_json_raises_protocol_error() -> None:
    """JSONDecodeError from inspect -> DockerSocketProtocolError."""
    log = structlog.get_logger()

    mock_response = AsyncMock()
    mock_response.status_code = 200
    mock_response.json = MagicMock(
        side_effect=json.JSONDecodeError("Expecting value", doc="{", pos=0)
    )

    mock_http = AsyncMock(spec=httpx.AsyncClient)
    mock_http.get.return_value = mock_response
    client = DockerSocketClient(socket_path="/var/run/docker.sock", log=log, httpx_client=mock_http)

    with pytest.raises(DockerSocketProtocolError) as exc_info:
        await client.inspect_container("abc123")

    assert "malformed JSON" in str(exc_info.value)
    await client.aclose()


@pytest.mark.asyncio
async def test_inspect_container_non_dict_payload_raises() -> None:
    """Server returns a list (not dict) from inspect -> DockerSocketProtocolError."""
    log = structlog.get_logger()

    mock_response = AsyncMock()
    mock_response.status_code = 200
    mock_response.json = MagicMock(return_value=[{"unexpected": "list"}])

    mock_http = AsyncMock(spec=httpx.AsyncClient)
    mock_http.get.return_value = mock_response
    client = DockerSocketClient(socket_path="/var/run/docker.sock", log=log, httpx_client=mock_http)

    with pytest.raises(DockerSocketProtocolError) as exc_info:
        await client.inspect_container("abc123")

    assert "expected dict" in str(exc_info.value)
    await client.aclose()


# ---------------------------------------------------------------------------
# events() method tests
# ---------------------------------------------------------------------------


async def _aiter_lines(lines: list[str]):  # type: ignore[no-untyped-def]  # noqa: ANN202
    """Async iterator that yields lines one by one."""
    for line in lines:
        yield line


@pytest.mark.asyncio
async def test_events_yields_parsed_json_dicts() -> None:
    """events() yields parsed dict for each valid JSON line."""
    log = structlog.get_logger()

    event1 = {"Action": "create", "Type": "container"}
    event2 = {"Action": "destroy", "Type": "container"}

    mock_resp = AsyncMock()
    mock_resp.status_code = 200
    mock_resp.aiter_lines = MagicMock(
        return_value=_aiter_lines(
            [
                json.dumps(event1),
                json.dumps(event2),
            ]
        )
    )

    mock_stream_cm = AsyncMock()
    mock_stream_cm.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_stream_cm.__aexit__ = AsyncMock(return_value=False)

    mock_http = AsyncMock(spec=httpx.AsyncClient)
    mock_http.stream = MagicMock(return_value=mock_stream_cm)

    client = DockerSocketClient(socket_path="/var/run/docker.sock", log=log, httpx_client=mock_http)

    results: list[dict[str, object]] = []
    async for ev in client.events():
        results.append(ev)

    assert len(results) == 2  # noqa: PLR2004
    assert results[0]["Action"] == "create"
    assert results[1]["Action"] == "destroy"
    await client.aclose()


@pytest.mark.asyncio
async def test_events_includes_filters_param_when_provided() -> None:
    """events() passes filters as JSON-encoded query param when provided."""
    log = structlog.get_logger()

    mock_resp = AsyncMock()
    mock_resp.status_code = 200
    mock_resp.aiter_lines = MagicMock(return_value=_aiter_lines([]))

    mock_stream_cm = AsyncMock()
    mock_stream_cm.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_stream_cm.__aexit__ = AsyncMock(return_value=False)

    mock_http = AsyncMock(spec=httpx.AsyncClient)
    mock_http.stream = MagicMock(return_value=mock_stream_cm)

    client = DockerSocketClient(socket_path="/var/run/docker.sock", log=log, httpx_client=mock_http)

    async for _ in client.events(filters={"type": ["container"]}):
        pass

    call_kwargs = mock_http.stream.call_args
    params = call_kwargs[1]["params"] if call_kwargs[1] else call_kwargs[0][2]
    assert "filters" in params
    assert json.loads(params["filters"]) == {"type": ["container"]}
    await client.aclose()


@pytest.mark.asyncio
async def test_events_no_filters_param_when_filters_none() -> None:
    """events() passes empty params dict when filters=None (covers branch 206->208)."""
    log = structlog.get_logger()

    mock_resp = AsyncMock()
    mock_resp.status_code = 200
    mock_resp.aiter_lines = MagicMock(return_value=_aiter_lines([]))

    mock_stream_cm = AsyncMock()
    mock_stream_cm.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_stream_cm.__aexit__ = AsyncMock(return_value=False)

    mock_http = AsyncMock(spec=httpx.AsyncClient)
    mock_http.stream = MagicMock(return_value=mock_stream_cm)

    client = DockerSocketClient(socket_path="/var/run/docker.sock", log=log, httpx_client=mock_http)

    async for _ in client.events():  # filters=None default
        pass

    call_kwargs = mock_http.stream.call_args
    params = call_kwargs[1]["params"] if call_kwargs[1] else call_kwargs[0][2]
    assert "filters" not in params
    await client.aclose()


@pytest.mark.asyncio
async def test_events_raises_protocol_error_on_non_200_status() -> None:
    """Non-200 status from /events raises DockerSocketProtocolError."""
    log = structlog.get_logger()

    mock_resp = AsyncMock()
    mock_resp.status_code = 500

    mock_stream_cm = AsyncMock()
    mock_stream_cm.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_stream_cm.__aexit__ = AsyncMock(return_value=False)

    mock_http = AsyncMock(spec=httpx.AsyncClient)
    mock_http.stream = MagicMock(return_value=mock_stream_cm)

    client = DockerSocketClient(socket_path="/var/run/docker.sock", log=log, httpx_client=mock_http)

    with pytest.raises(DockerSocketProtocolError) as exc_info:
        async for _ in client.events():
            pass

    assert "500" in str(exc_info.value)
    await client.aclose()


@pytest.mark.asyncio
async def test_events_skips_empty_lines() -> None:
    """Blank lines in the stream are skipped; valid events are still yielded."""
    log = structlog.get_logger()

    event = {"Action": "create", "Type": "container"}

    mock_resp = AsyncMock()
    mock_resp.status_code = 200
    mock_resp.aiter_lines = MagicMock(
        return_value=_aiter_lines(
            [
                json.dumps(event),
                "",  # blank line — must be skipped
                "   ",  # whitespace-only — must be skipped
                json.dumps({"Action": "destroy", "Type": "container"}),
            ]
        )
    )

    mock_stream_cm = AsyncMock()
    mock_stream_cm.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_stream_cm.__aexit__ = AsyncMock(return_value=False)

    mock_http = AsyncMock(spec=httpx.AsyncClient)
    mock_http.stream = MagicMock(return_value=mock_stream_cm)

    client = DockerSocketClient(socket_path="/var/run/docker.sock", log=log, httpx_client=mock_http)

    results: list[dict[str, object]] = []
    async for ev in client.events():
        results.append(ev)

    assert len(results) == 2  # noqa: PLR2004
    await client.aclose()


@pytest.mark.asyncio
async def test_events_skips_bad_json_lines() -> None:
    """Malformed JSON lines are skipped (warning logged); valid events still yielded."""
    log = structlog.get_logger()

    good = {"Action": "create", "Type": "container"}

    mock_resp = AsyncMock()
    mock_resp.status_code = 200
    mock_resp.aiter_lines = MagicMock(
        return_value=_aiter_lines(
            [
                json.dumps(good),
                "{not valid json!!!",  # bad JSON — must be skipped
            ]
        )
    )

    mock_stream_cm = AsyncMock()
    mock_stream_cm.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_stream_cm.__aexit__ = AsyncMock(return_value=False)

    mock_http = AsyncMock(spec=httpx.AsyncClient)
    mock_http.stream = MagicMock(return_value=mock_stream_cm)

    client = DockerSocketClient(socket_path="/var/run/docker.sock", log=log, httpx_client=mock_http)

    results: list[dict[str, object]] = []
    async for ev in client.events():
        results.append(ev)

    assert len(results) == 1
    assert results[0]["Action"] == "create"
    await client.aclose()


@pytest.mark.asyncio
async def test_events_skips_non_dict_events() -> None:
    """JSON that parses to a non-dict (e.g. list) is skipped (covers branch 234->221)."""
    log = structlog.get_logger()

    mock_resp = AsyncMock()
    mock_resp.status_code = 200
    mock_resp.aiter_lines = MagicMock(
        return_value=_aiter_lines(
            [
                json.dumps([1, 2, 3]),  # valid JSON but a list, not dict
                json.dumps({"Action": "create"}),  # valid dict — should be yielded
            ]
        )
    )

    mock_stream_cm = AsyncMock()
    mock_stream_cm.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_stream_cm.__aexit__ = AsyncMock(return_value=False)

    mock_http = AsyncMock(spec=httpx.AsyncClient)
    mock_http.stream = MagicMock(return_value=mock_stream_cm)

    client = DockerSocketClient(socket_path="/var/run/docker.sock", log=log, httpx_client=mock_http)

    results: list[dict[str, object]] = []
    async for ev in client.events():
        results.append(ev)

    assert len(results) == 1
    assert results[0]["Action"] == "create"
    await client.aclose()


@pytest.mark.asyncio
async def test_events_raises_connection_error_on_httpx_connect_error() -> None:
    """httpx.ConnectError during events stream raises DockerSocketConnectionError."""
    log = structlog.get_logger()
    socket_path = "/var/run/docker.sock"

    mock_stream_cm = AsyncMock()
    mock_stream_cm.__aenter__ = AsyncMock(side_effect=httpx.ConnectError("refused"))
    mock_stream_cm.__aexit__ = AsyncMock(return_value=False)

    mock_http = AsyncMock(spec=httpx.AsyncClient)
    mock_http.stream = MagicMock(return_value=mock_stream_cm)

    client = DockerSocketClient(socket_path=socket_path, log=log, httpx_client=mock_http)

    with pytest.raises(DockerSocketConnectionError) as exc_info:
        async for _ in client.events():
            pass

    assert socket_path in str(exc_info.value)
    await client.aclose()


@pytest.mark.asyncio
async def test_events_raises_connection_error_on_httpx_http_error() -> None:
    """Non-ConnectError httpx.HTTPError during events stream raises DockerSocketConnectionError."""
    log = structlog.get_logger()

    mock_stream_cm = AsyncMock()
    mock_stream_cm.__aenter__ = AsyncMock(side_effect=httpx.ReadError("connection reset"))
    mock_stream_cm.__aexit__ = AsyncMock(return_value=False)

    mock_http = AsyncMock(spec=httpx.AsyncClient)
    mock_http.stream = MagicMock(return_value=mock_stream_cm)

    client = DockerSocketClient(socket_path="/var/run/docker.sock", log=log, httpx_client=mock_http)

    with pytest.raises(DockerSocketConnectionError) as exc_info:
        async for _ in client.events():
            pass

    assert "transport error" in str(exc_info.value)
    await client.aclose()


@pytest.mark.asyncio
async def test_default_constructor_creates_real_client_smoke() -> None:
    """Default constructor (no httpx_client injected) creates a real client and closes cleanly.

    Smoke-test the production-path UDS transport construction. Does NOT touch the socket.
    """
    log = structlog.get_logger()
    # Use a path that won't be touched — constructor doesn't open the socket.
    client = DockerSocketClient(socket_path="/tmp/nonexistent-socket-for-test", log=log)
    await client.aclose()
