"""Async Docker Engine API client over Unix Domain Socket (httpx transport).

D-DOCKER-SDK-MANUAL-HTTPX (Design exit 2026-05-21): No external SDK; we own the
two endpoints we need:
  - GET /containers/json?all=true    -> list (running + exited)
  - GET /containers/{id}/json        -> inspect

NEVER invokes write operations — the socket mount is :ro until STAGE-003-010
(D-SOCKET-READ-ONLY).
"""

from __future__ import annotations

import json
from typing import Final, NotRequired, TypedDict, cast

import httpx
from structlog.stdlib import BoundLogger

HTTP_OK = 200  # Docker Engine API success status

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
