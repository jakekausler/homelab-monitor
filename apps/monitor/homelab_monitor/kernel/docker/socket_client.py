"""Async Docker Engine API client over Unix Domain Socket (httpx transport).

D-DOCKER-SDK-MANUAL-HTTPX (Design exit 2026-05-21): No external SDK; we own the
endpoints we need:
  - GET /containers/json?all=true    -> list (running + exited)
  - GET /containers/{id}/json        -> inspect
  - POST /containers/{id}/exec       -> create exec instance
  - POST /exec/{id}/start            -> start exec instance
  - GET /exec/{id}/json              -> inspect exec result

Per D-EXEC-OPT-IN (spec §7.4): write operations (exec) are enabled only when
the operator opts in via global flag + per-container label.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Final, NotRequired, TypedDict, cast

import httpx
from structlog.stdlib import BoundLogger

HTTP_OK = 200  # Docker Engine API success status
HTTP_NOT_FOUND: Final[int] = 404

_DEFAULT_SOCKET_PATH: Final[str] = "/var/run/docker.sock"
# Docker Engine API base host — any value works, httpx UDS transport ignores it.
_BASE_URL: Final[str] = "http://localhost"
# Timeouts kept tight: list/inspect should each complete in <2s on a healthy host.
_REQUEST_TIMEOUT_SECONDS: Final[float] = 5.0


class DockerSocketError(Exception):
    """Raised when the Docker socket is unreachable or returns malformed data.

    Subclasses indicate failure category (connection / protocol / version).
    Collectors catch this and convert to CollectorResult(ok=False).
    """


class DockerSocketConnectionError(DockerSocketError):
    """Socket file missing, permission denied, or peer reset."""


class DockerSocketProtocolError(DockerSocketError):
    """HTTP/JSON unexpected shape; includes status code + raw snippet."""


class ContainerListEntry(TypedDict):
    """Subset of /containers/json entry we consume (Docker API v1.41+)."""

    Id: str
    Names: list[str]
    Image: str
    ImageID: str
    State: str  # "running" | "exited" | "restarting" | "paused" | "dead" | "created"
    Status: str  # e.g. "Up 3 hours", "Exited (0) 2 minutes ago"
    Labels: dict[str, str]


class HealthLog(TypedDict, total=False):
    """One health-probe history entry — unused fields elided."""


class HealthState(TypedDict, total=False):
    Status: str  # "none" | "starting" | "healthy" | "unhealthy"
    FailingStreak: int


class ContainerState(TypedDict, total=False):
    Status: str
    Running: bool
    Paused: bool
    Restarting: bool
    Dead: bool
    ExitCode: int
    FinishedAt: NotRequired[
        str
    ]  # RFC3339 stop time; "0001-01-01T00:00:00Z" sentinel when never stopped
    RestartCount: NotRequired[int]  # may live on .RestartCount root vs .State.RestartCount
    Health: NotRequired[HealthState]


class HostConfig(TypedDict, total=False):
    NetworkMode: str


class ContainerInspect(TypedDict):
    """Subset of /containers/{id}/json we consume."""

    Id: str
    Name: str
    Image: str
    State: ContainerState
    RestartCount: int  # Docker emits this at root of inspect response (not under State)
    HostConfig: HostConfig
    Config: NotRequired[dict[str, object]]  # Labels live here too; redundant with list view


class DockerSocketClient:
    """Read-only Docker Engine API client over UDS.

    Single-instance per process; reused across collector ticks. Holds one
    httpx.AsyncClient bound to a UDS transport; closed via aclose().
    """

    def __init__(
        self,
        *,
        socket_path: str = _DEFAULT_SOCKET_PATH,
        log: BoundLogger,
        httpx_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._socket_path: str = socket_path
        self._log: BoundLogger = log
        self._client: httpx.AsyncClient = httpx_client or httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds=socket_path),
            base_url=_BASE_URL,
            timeout=httpx.Timeout(_REQUEST_TIMEOUT_SECONDS, connect=_REQUEST_TIMEOUT_SECONDS),
        )

    async def aclose(self) -> None:
        """Close the underlying httpx client."""
        await self._client.aclose()

    async def list_containers(self) -> list[ContainerListEntry]:
        """GET /containers/json?all=true. Returns running + exited.

        Raises:
            DockerSocketConnectionError: socket missing / permission denied.
            DockerSocketProtocolError: non-200 status or non-list JSON.
        """
        try:
            resp = await self._client.get("/containers/json", params={"all": "true"})
        except httpx.ConnectError as exc:
            raise DockerSocketConnectionError(
                f"docker socket unreachable at {self._socket_path}: {exc}"
            ) from exc
        except httpx.HTTPError as exc:
            raise DockerSocketConnectionError(f"docker socket transport error: {exc}") from exc
        if resp.status_code != HTTP_OK:
            raise DockerSocketProtocolError(
                f"unexpected status {resp.status_code} from /containers/json: {resp.text[:200]}"
            )
        try:
            data = resp.json()
        except json.JSONDecodeError as exc:
            raise DockerSocketProtocolError(f"malformed JSON from /containers/json: {exc}") from exc
        if not isinstance(data, list):
            raise DockerSocketProtocolError(
                f"expected list from /containers/json, got {type(data).__name__}"
            )
        # Cast: we've validated the top-level shape (list/dict) but Docker may return
        # fields we haven't typed. Trust boundary — failures surface as KeyError at consumers.
        return cast("list[ContainerListEntry]", data)

    async def inspect_container(self, container_id: str) -> ContainerInspect:
        """GET /containers/{id}/json.

        Raises:
            DockerSocketConnectionError: socket missing / permission denied.
            DockerSocketProtocolError: non-200 or malformed JSON.
        """
        try:
            resp = await self._client.get(f"/containers/{container_id}/json")
        except httpx.ConnectError as exc:
            raise DockerSocketConnectionError(
                f"docker socket unreachable at {self._socket_path}: {exc}"
            ) from exc
        except httpx.HTTPError as exc:
            raise DockerSocketConnectionError(f"docker socket transport error: {exc}") from exc
        if resp.status_code != HTTP_OK:
            raise DockerSocketProtocolError(
                f"unexpected status {resp.status_code} from /containers/{container_id}/json: "
                f"{resp.text[:200]}"
            )
        try:
            data = resp.json()
        except json.JSONDecodeError as exc:
            raise DockerSocketProtocolError(
                f"malformed JSON from /containers/{container_id}/json: {exc}"
            ) from exc
        if not isinstance(data, dict):
            raise DockerSocketProtocolError(
                f"expected dict from inspect, got {type(data).__name__}"
            )
        # Cast: we've validated the top-level shape (list/dict) but Docker may return
        # fields we haven't typed. Trust boundary — failures surface as KeyError at consumers.
        return cast("ContainerInspect", data)

    async def image_inspect(self, image_id: str) -> dict[str, object] | None:
        """GET /images/{image_id}/json — returns inspect blob.

        The response includes 'RepoDigests': list[str], which is the
        local-side equivalent of the registry's "latest" digest.
        Required by STAGE-003-008's ImageUpdateCollector because
        /containers/json + /containers/{id}/json do NOT include
        RepoDigests.

        Returns:
            The decoded JSON inspect dict on 200, or None on 404.

        Raises:
            DockerSocketConnectionError: socket unreachable.
            DockerSocketProtocolError: non-200/non-404 status, malformed JSON.
        """
        try:
            resp = await self._client.get(f"/images/{image_id}/json")
        except httpx.ConnectError as exc:
            raise DockerSocketConnectionError(
                f"docker socket unreachable at {self._socket_path}: {exc}"
            ) from exc
        except httpx.HTTPError as exc:  # pragma: no cover -- defensive
            raise DockerSocketConnectionError(f"docker socket transport error: {exc}") from exc
        if resp.status_code == HTTP_NOT_FOUND:
            return None
        if resp.status_code != HTTP_OK:
            raise DockerSocketProtocolError(
                f"unexpected status {resp.status_code} from /images/{image_id}/json: "
                f"{resp.text[:200]}"
            )
        try:
            data = resp.json()
        except json.JSONDecodeError as exc:
            raise DockerSocketProtocolError(
                f"malformed JSON from /images/{image_id}/json: {exc}"
            ) from exc
        if not isinstance(data, dict):
            raise DockerSocketProtocolError(
                f"expected dict from /images/{image_id}/json, got {type(data).__name__}"
            )
        return cast("dict[str, object]", data)

    async def events(
        self,
        *,
        filters: dict[str, list[str]] | None = None,
    ) -> AsyncIterator[dict[str, object]]:
        """GET /events?filters=... — long-lived stream of newline-delimited JSON.

        Yields one dict per event. The caller decides which events to act on
        (the discoverer handles container.create / container.destroy). The
        stream lives until the consumer breaks out of the iterator or the
        socket disconnects (httpx raises, surfaces to the caller).

        The stream uses read_timeout=None (override the client default) since
        Docker may emit nothing for many seconds. Connect timeout stays at
        the client default for fail-fast on a dead socket.

        Raises:
            DockerSocketConnectionError: socket unreachable / peer reset mid-stream.
            DockerSocketProtocolError: non-200 status from /events.
        """
        params: dict[str, str] = {}
        if filters:
            params["filters"] = json.dumps(filters)
        try:
            async with self._client.stream(
                "GET",
                "/events",
                params=params,
                timeout=httpx.Timeout(
                    _REQUEST_TIMEOUT_SECONDS, connect=_REQUEST_TIMEOUT_SECONDS, read=None
                ),
            ) as resp:
                if resp.status_code != HTTP_OK:
                    raise DockerSocketProtocolError(
                        f"unexpected status {resp.status_code} from /events"
                    )
                async for raw_line in resp.aiter_lines():
                    stripped_line = raw_line.strip()
                    if not stripped_line:
                        continue
                    try:
                        event = json.loads(stripped_line)
                    except json.JSONDecodeError as exc:
                        self._log.warning(
                            "docker_socket.events_bad_json",
                            line=stripped_line[:200],
                            error=str(exc),
                        )
                        continue
                    if isinstance(event, dict):
                        yield event
        except httpx.ConnectError as exc:
            raise DockerSocketConnectionError(
                f"docker socket unreachable at {self._socket_path}: {exc}"
            ) from exc
        except httpx.HTTPError as exc:
            raise DockerSocketConnectionError(
                f"docker socket transport error during events stream: {exc}"
            ) from exc

    async def exec_in_container(
        self,
        *,
        container_id: str,
        cmd: str,
    ) -> int:
        """Run `cmd` inside container_id; return exit code.

        Performs three HTTP calls:
          1. POST /containers/{id}/exec — create exec instance, get exec_id.
          2. POST /exec/{id}/start — start it (detached so we don't stream stdout).
          3. GET  /exec/{id}/json   — inspect for ExitCode.

        The cmd string is shell-split — we pass it as a one-element ["sh", "-c", cmd]
        argv to use the container's /bin/sh. This matches `docker exec <c> sh -c '<cmd>'`.

        NEVER raises on a non-zero exit code — caller (probe_executor) interprets
        that as "probe failed".

        Raises:
            DockerSocketConnectionError: socket unreachable.
            DockerSocketProtocolError: non-200 from any of the three calls.
        """
        # 1. Create the exec instance.
        create_body = {
            "Cmd": ["sh", "-c", cmd],
            "AttachStdout": False,
            "AttachStderr": False,
            "Tty": False,
        }
        try:
            resp = await self._client.post(
                f"/containers/{container_id}/exec",
                json=create_body,
            )
        except httpx.ConnectError as exc:
            raise DockerSocketConnectionError(
                f"docker socket unreachable at {self._socket_path}: {exc}"
            ) from exc
        except httpx.HTTPError as exc:
            raise DockerSocketConnectionError(f"docker socket transport error: {exc}") from exc
        if resp.status_code not in (200, 201):
            raise DockerSocketProtocolError(
                f"unexpected status {resp.status_code} from /containers/{container_id}/exec: "
                f"{resp.text[:200]}"
            )
        try:
            data = resp.json()
        except json.JSONDecodeError as exc:
            raise DockerSocketProtocolError(
                f"malformed JSON from /containers/{container_id}/exec: {exc}"
            ) from exc
        if not isinstance(data, dict) or "Id" not in data:
            raise DockerSocketProtocolError(
                f"expected exec_id in /containers/{container_id}/exec response, got: {data}"
            )
        exec_id: str = str(cast(dict[str, object], data)["Id"])

        # 2. Start the exec instance (detached).
        try:
            start_resp = await self._client.post(
                f"/exec/{exec_id}/start",
                json={"Detach": False, "Tty": False},
            )
        except httpx.HTTPError as exc:
            raise DockerSocketConnectionError(f"docker socket transport error: {exc}") from exc
        if start_resp.status_code not in (200, 201):
            raise DockerSocketProtocolError(
                f"unexpected status {start_resp.status_code} from /exec/{exec_id}/start"
            )

        # 3. Inspect to get the exit code.
        try:
            inspect_resp = await self._client.get(f"/exec/{exec_id}/json")
        except httpx.HTTPError as exc:
            raise DockerSocketConnectionError(f"docker socket transport error: {exc}") from exc
        if inspect_resp.status_code != HTTP_OK:
            raise DockerSocketProtocolError(
                f"unexpected status {inspect_resp.status_code} from /exec/{exec_id}/json"
            )
        try:
            idata = inspect_resp.json()
        except json.JSONDecodeError as exc:
            raise DockerSocketProtocolError(
                f"malformed JSON from /exec/{exec_id}/json: {exc}"
            ) from exc
        if not isinstance(idata, dict):
            raise DockerSocketProtocolError(
                f"expected dict from /exec/{exec_id}/json, got {type(idata).__name__}"
            )
        typed_idata: dict[str, object] = cast(dict[str, object], idata)
        exit_code_raw = typed_idata.get("ExitCode")
        if exit_code_raw is None:
            return 1
        return int(cast(int, exit_code_raw))
