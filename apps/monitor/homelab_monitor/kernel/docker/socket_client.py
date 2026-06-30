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

import asyncio
import json
import struct
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
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
# Lifecycle writes (restart/stop) can take longer than the 5s read default:
# Docker's default stop-grace is 10s before SIGKILL, so a restart can take
# ~10s+ before the API responds. Override the per-call timeout for writes.
_WRITE_TIMEOUT_SECONDS: Final[float] = 30.0


class DockerSocketError(Exception):
    """Raised when the Docker socket is unreachable or returns malformed data.

    Subclasses indicate failure category (connection / protocol / version).
    Collectors catch this and convert to CollectorResult(ok=False).
    """


class DockerSocketConnectionError(DockerSocketError):
    """Socket file missing, permission denied, or peer reset."""


class DockerExecTimeoutError(DockerSocketConnectionError):
    """Raised when a bounded ``exec_capture`` exceeds its ``timeout_seconds``.

    Subclass of ``DockerSocketConnectionError`` so existing ``except`` clauses
    keep catching it, while callers that need to distinguish a timeout from a
    generic connection failure can ``isinstance``-check this type (the
    orchestrator maps it to exit-code sentinel 124).
    """


class DockerSocketProtocolError(DockerSocketError):
    """HTTP/JSON unexpected shape; includes status code + raw snippet."""


# Docker exec stream-frame layout (Tty:false). Each frame is an 8-byte header
# followed by a payload. Header byte0 = stream type (1=stdout, 2=stderr,
# 0=stdin, 3=systemerr); bytes 1-3 = zero padding; bytes 4-7 = big-endian
# uint32 payload length. CONFIRMED via real socket hexdump:
#   01 00 00 00 00 00 00 06 68 65 6c 6c 6f 0a  -> stdout frame, len 6, "hello\n"
_EXEC_FRAME_HEADER_LEN: Final[int] = 8
_EXEC_STREAM_STDOUT: Final[int] = 1
_EXEC_STREAM_STDERR: Final[int] = 2


@dataclass(frozen=True, slots=True)
class ExecResult:
    """Captured result of an exec_capture() run: exit code + decoded stdout/stderr."""

    exit_code: int
    stdout: str
    stderr: str


def _demux_stream(data: bytes) -> tuple[str, str]:
    """De-multiplex a Docker exec start-response body into (stdout, stderr).

    Parses the multiplexed frame stream (8-byte header + payload per frame; see
    the layout note above). Frames typed stdout (1) accumulate into the stdout
    buffer, stderr (2) into the stderr buffer; any other stream type is ignored.
    Buffers are decoded utf-8 with errors="replace".

    Robust to malformed input:
      - empty input -> ("", "")
      - a truncated final header (<8 bytes remaining) -> stop, return what we have
      - a truncated payload (header claims N but <N bytes remain) -> stop, return
        what we have
    """
    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []
    offset = 0
    total = len(data)
    while offset + _EXEC_FRAME_HEADER_LEN <= total:
        header = data[offset : offset + _EXEC_FRAME_HEADER_LEN]
        # '>BxxxI' = stream-type byte, 3 pad bytes, big-endian uint32 length.
        stream_type, length = cast("tuple[int, int]", struct.unpack(">BxxxI", header))
        payload_start = offset + _EXEC_FRAME_HEADER_LEN
        payload_end = payload_start + length
        if payload_end > total:
            # Truncated payload: header claims more bytes than remain. Stop gracefully.
            break
        payload = data[payload_start:payload_end]
        if stream_type == _EXEC_STREAM_STDOUT:
            stdout_chunks.append(payload)
        elif stream_type == _EXEC_STREAM_STDERR:
            stderr_chunks.append(payload)
        # any other stream type (stdin / systemerr) is ignored.
        offset = payload_end
    stdout = b"".join(stdout_chunks).decode("utf-8", errors="replace")
    stderr = b"".join(stderr_chunks).decode("utf-8", errors="replace")
    return stdout, stderr


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

    async def exec_capture(
        self,
        *,
        container_id: str,
        cmd: list[str],
        timeout_seconds: float,
        user: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> ExecResult:
        """Run ``cmd`` (argv list) inside container_id; CAPTURE stdout/stderr + exit code.

        The stdout-capturing sibling of :meth:`exec_in_container` (which is
        exit-code-only). Performs the same three-call dance but attaches stdout +
        stderr, reads the (multiplexed) start-response body, and de-muxes it:
          1. POST /containers/{id}/exec  — create with AttachStdout/AttachStderr.
          2. POST /exec/{id}/start       — start; read ``resp.content`` (raw bytes).
          3. GET  /exec/{id}/json        — inspect for ExitCode.

        ``cmd`` is passed as argv directly (no ``sh -c`` shell layer), e.g.
        ``["unbound-control", "stats_noreset"]``.

        ``user`` (optional) is the user to run the exec as (e.g. ``"homelab-fixer"``).
        ``env`` (optional) is a dict of environment variables to set.

        The whole operation is bounded by ``asyncio.wait_for(timeout_seconds)``; a
        timeout surfaces as DockerSocketConnectionError (a timed-out socket is a
        connectivity failure), keeping the typed-error surface identical to
        exec_in_container so callers need no new exception type.

        NEVER raises on a non-zero exit code — the caller interprets ExitCode.

        SCAFFOLDING NOTE: this is a general capability. Only the unbound-control
        access layer (STAGE-006-003) consumes it today; it is NOT throwaway.

        Raises:
            DockerSocketConnectionError: socket unreachable, transport error, or timeout.
            DockerSocketProtocolError: non-200/201 status or malformed JSON from any call.
        """
        try:
            return await asyncio.wait_for(
                self._exec_capture_inner(
                    container_id=container_id,
                    cmd=cmd,
                    user=user,
                    env=env,
                    timeout_seconds=timeout_seconds,
                ),
                timeout=timeout_seconds,
            )
        except TimeoutError as exc:
            raise DockerExecTimeoutError(
                f"docker exec timed out after {timeout_seconds}s in {container_id}: {exc}"
            ) from exc

    async def _exec_capture_inner(  # noqa: PLR0912 -- User/Env/timeout extension adds branches
        self,
        *,
        container_id: str,
        cmd: list[str],
        user: str | None = None,
        env: Mapping[str, str] | None = None,
        timeout_seconds: float = _REQUEST_TIMEOUT_SECONDS,
    ) -> ExecResult:
        """Unbounded body of exec_capture (wrapped in a timeout by the caller)."""
        # 1. Create the exec instance (attach stdout + stderr for capture).
        create_body: dict[str, object] = {
            "Cmd": cmd,
            "AttachStdout": True,
            "AttachStderr": True,
            "Tty": False,
        }
        if user is not None:
            create_body["User"] = user
        if env is not None:
            create_body["Env"] = [f"{k}={v}" for k, v in env.items()]
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

        # 2. Start the exec instance and READ the multiplexed stream body.
        # stdin intentionally not attached: Docker gives an unattached exec stdin
        # an immediate EOF (003 contract: claude must see EOF on stdin and never block).
        try:
            start_resp = await self._client.post(
                f"/exec/{exec_id}/start",
                json={"Detach": False, "Tty": False},
                timeout=timeout_seconds,
            )
        except httpx.HTTPError as exc:
            raise DockerSocketConnectionError(f"docker socket transport error: {exc}") from exc
        if start_resp.status_code not in (200, 201):
            raise DockerSocketProtocolError(
                f"unexpected status {start_resp.status_code} from /exec/{exec_id}/start"
            )
        stdout, stderr = _demux_stream(start_resp.content)

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
        exit_code = 1 if exit_code_raw is None else int(cast(int, exit_code_raw))
        return ExecResult(exit_code=exit_code, stdout=stdout, stderr=stderr)

    async def restart_container(
        self, container_id: str, *, timeout_seconds: int | None = None
    ) -> None:
        """POST /containers/{id}/restart. Returns None on success (204 or 304).

        304 == container already in the target state == idempotent success.
        The per-call timeout is widened to ``_WRITE_TIMEOUT_SECONDS`` because a
        restart waits out Docker's stop-grace (~10s) before responding.

        Raises:
            DockerSocketConnectionError: socket unreachable / transport error.
            DockerSocketProtocolError: unexpected (non-204/304) status.
        """
        params: dict[str, str] = {}
        if timeout_seconds is not None:
            params["t"] = str(timeout_seconds)
        try:
            resp = await self._client.post(
                f"/containers/{container_id}/restart",
                params=params,
                timeout=httpx.Timeout(_WRITE_TIMEOUT_SECONDS, connect=_REQUEST_TIMEOUT_SECONDS),
            )
        except httpx.ConnectError as exc:
            raise DockerSocketConnectionError(
                f"docker socket unreachable at {self._socket_path}: {exc}"
            ) from exc
        except httpx.HTTPError as exc:
            raise DockerSocketConnectionError(f"docker socket transport error: {exc}") from exc
        if resp.status_code not in (204, 304):
            raise DockerSocketProtocolError(
                f"unexpected status {resp.status_code} from "
                f"/containers/{container_id}/restart: {resp.text[:200]}"
            )

    async def start_container(self, container_id: str) -> None:
        """POST /containers/{id}/start. Returns None on success (204 or 304).

        304 == already running == idempotent success.

        Raises:
            DockerSocketConnectionError: socket unreachable / transport error.
            DockerSocketProtocolError: unexpected (non-204/304) status.
        """
        try:
            resp = await self._client.post(
                f"/containers/{container_id}/start",
                timeout=httpx.Timeout(_WRITE_TIMEOUT_SECONDS, connect=_REQUEST_TIMEOUT_SECONDS),
            )
        except httpx.ConnectError as exc:
            raise DockerSocketConnectionError(
                f"docker socket unreachable at {self._socket_path}: {exc}"
            ) from exc
        except httpx.HTTPError as exc:
            raise DockerSocketConnectionError(f"docker socket transport error: {exc}") from exc
        if resp.status_code not in (204, 304):
            raise DockerSocketProtocolError(
                f"unexpected status {resp.status_code} from "
                f"/containers/{container_id}/start: {resp.text[:200]}"
            )

    async def stop_container(
        self, container_id: str, *, timeout_seconds: int | None = None
    ) -> None:
        """POST /containers/{id}/stop. Returns None on success (204 or 304).

        304 == already stopped == idempotent success.

        Raises:
            DockerSocketConnectionError: socket unreachable / transport error.
            DockerSocketProtocolError: unexpected (non-204/304) status.
        """
        params: dict[str, str] = {}
        if timeout_seconds is not None:
            params["t"] = str(timeout_seconds)
        try:
            resp = await self._client.post(
                f"/containers/{container_id}/stop",
                params=params,
                timeout=httpx.Timeout(_WRITE_TIMEOUT_SECONDS, connect=_REQUEST_TIMEOUT_SECONDS),
            )
        except httpx.ConnectError as exc:
            raise DockerSocketConnectionError(
                f"docker socket unreachable at {self._socket_path}: {exc}"
            ) from exc
        except httpx.HTTPError as exc:
            raise DockerSocketConnectionError(f"docker socket transport error: {exc}") from exc
        if resp.status_code not in (204, 304):
            raise DockerSocketProtocolError(
                f"unexpected status {resp.status_code} from "
                f"/containers/{container_id}/stop: {resp.text[:200]}"
            )
