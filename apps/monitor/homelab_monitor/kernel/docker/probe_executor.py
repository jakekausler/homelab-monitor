"""Execute resolved probes. One function per kind.

All executors return ProbeOutcome. NEVER raise — exceptions are caught
and converted to ProbeOutcome(up=False, error=<str>).
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass
from typing import Final

import httpx

from homelab_monitor.kernel.docker.probe_resolver import ResolvedProbe
from homelab_monitor.kernel.docker.socket_client import (
    DockerSocketClient,
    DockerSocketError,
)

# Security model for exec probes:
# - exec probes run inside a target container via `docker exec` (a write op on
#   the Docker API). The :ro socket bind-mount does NOT restrict API ops.
# - exec is gated by TWO opt-ins: (1) global `docker.probes.exec_enabled` flag,
#   (2) per-container `homelab-monitor.exec_authorized=true` label. The label
#   is operator consent expressed in the compose file, not a security check
#   against a malicious container.
# - Trust boundary: a container that can set its own labels is already
#   operator-trusted. The label IS the operator's per-container opt-in. This
#   model does NOT defend against a compromised container modifying its own
#   labels at runtime (that would require the operator to lose control of
#   docker socket first, which is a higher-tier compromise).
# - Defense-in-depth: cmd is capped at 4096 chars and rejects newlines/null.

_HTTP_SUCCESS_LOWER: Final[int] = 200
_HTTP_SUCCESS_UPPER: Final[int] = 400  # 2xx and 3xx are "up"
_MAX_EXEC_CMD_LEN: Final[int] = 4096


@dataclass(frozen=True, slots=True)
class ProbeOutcome:
    up: bool
    duration_seconds: float
    error: str | None


async def execute_http(
    http_client: httpx.AsyncClient,
    target: str,
    timeout_seconds: float,
) -> ProbeOutcome:
    start = time.monotonic()
    try:
        resp = await http_client.get(target, timeout=timeout_seconds)
    except httpx.TimeoutException as exc:
        return ProbeOutcome(
            up=False, duration_seconds=time.monotonic() - start, error=f"timeout: {exc}"
        )
    except httpx.HTTPError as exc:
        return ProbeOutcome(
            up=False, duration_seconds=time.monotonic() - start, error=f"http_error: {exc}"
        )
    duration = time.monotonic() - start
    if _HTTP_SUCCESS_LOWER <= resp.status_code < _HTTP_SUCCESS_UPPER:
        return ProbeOutcome(up=True, duration_seconds=duration, error=None)
    return ProbeOutcome(
        up=False, duration_seconds=duration, error=f"http_status_{resp.status_code}"
    )


async def execute_tcp(target: str, timeout_seconds: float) -> ProbeOutcome:
    """target is `host:port`."""
    start = time.monotonic()
    try:
        host, port_str = target.rsplit(":", 1)
        port = int(port_str)
    except (ValueError, AttributeError) as exc:
        return ProbeOutcome(
            up=False,
            duration_seconds=time.monotonic() - start,
            error=f"malformed_target: {exc}",
        )
    try:
        # asyncio.open_connection raises on failure; we wrap with timeout.
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host=host, port=port),
            timeout=timeout_seconds,
        )
        writer.close()
        with contextlib.suppress(ConnectionError, OSError):
            await writer.wait_closed()
        return ProbeOutcome(up=True, duration_seconds=time.monotonic() - start, error=None)
    except TimeoutError:
        return ProbeOutcome(
            up=False,
            duration_seconds=time.monotonic() - start,
            error="timeout",
        )
    except (ConnectionError, OSError) as exc:
        return ProbeOutcome(
            up=False,
            duration_seconds=time.monotonic() - start,
            error=f"connection_error: {exc}",
        )


async def execute_exec(
    socket_client: DockerSocketClient,
    container_id: str,
    cmd: str,
    timeout_seconds: float,
) -> ProbeOutcome:
    if len(cmd) > _MAX_EXEC_CMD_LEN:
        return ProbeOutcome(
            up=False,
            duration_seconds=0.0,
            error="cmd_too_long",
        )
    if "\x00" in cmd or "\n" in cmd:
        return ProbeOutcome(
            up=False,
            duration_seconds=0.0,
            error="cmd_invalid_chars",
        )
    start = time.monotonic()
    try:
        exit_code = await asyncio.wait_for(
            socket_client.exec_in_container(container_id=container_id, cmd=cmd),
            timeout=timeout_seconds,
        )
    except TimeoutError:
        return ProbeOutcome(up=False, duration_seconds=time.monotonic() - start, error="timeout")
    except DockerSocketError as exc:
        return ProbeOutcome(
            up=False,
            duration_seconds=time.monotonic() - start,
            error=f"docker_error: {exc}",
        )
    duration = time.monotonic() - start
    if exit_code == 0:
        return ProbeOutcome(up=True, duration_seconds=duration, error=None)
    return ProbeOutcome(up=False, duration_seconds=duration, error=f"exit_code_{exit_code}")


async def execute_metrics(
    http_client: httpx.AsyncClient,
    target: str,
    timeout_seconds: float,
) -> ProbeOutcome:
    """Fetch URL; validate response is non-empty Prometheus-exposition-shaped.

    We don't import prometheus_client.parser to avoid dep coupling — a
    lightweight check is sufficient: HTTP 2xx + non-empty body + at least one
    line that doesn't start with '#' (i.e., at least one metric sample).
    """
    start = time.monotonic()
    try:
        resp = await http_client.get(target, timeout=timeout_seconds)
    except httpx.TimeoutException:
        return ProbeOutcome(up=False, duration_seconds=time.monotonic() - start, error="timeout")
    except httpx.HTTPError as exc:
        return ProbeOutcome(
            up=False, duration_seconds=time.monotonic() - start, error=f"http_error: {exc}"
        )
    duration = time.monotonic() - start
    if not (_HTTP_SUCCESS_LOWER <= resp.status_code < _HTTP_SUCCESS_UPPER):
        return ProbeOutcome(
            up=False, duration_seconds=duration, error=f"http_status_{resp.status_code}"
        )
    body = resp.text.strip()
    if not body:
        return ProbeOutcome(up=False, duration_seconds=duration, error="empty_body")
    has_sample = any(line and not line.startswith("#") for line in body.splitlines())
    if not has_sample:
        return ProbeOutcome(up=False, duration_seconds=duration, error="no_metric_samples")
    return ProbeOutcome(up=True, duration_seconds=duration, error=None)


async def execute_resolved_probe(
    probe: ResolvedProbe,
    *,
    http_client: httpx.AsyncClient,
    socket_client: DockerSocketClient | None,
    timeout_seconds: float,
) -> ProbeOutcome:
    """Dispatch by kind. Convenience wrapper for callers."""
    if probe.kind == "http":
        return await execute_http(http_client, probe.target, timeout_seconds)
    if probe.kind == "metrics":
        return await execute_metrics(http_client, probe.target, timeout_seconds)
    if probe.kind == "tcp":
        return await execute_tcp(probe.target, timeout_seconds)
    if probe.kind == "exec":
        if socket_client is None or probe.container_id is None or probe.exec_cmd is None:
            return ProbeOutcome(up=False, duration_seconds=0.0, error="exec_unconfigured")
        return await execute_exec(
            socket_client, probe.container_id, probe.exec_cmd, timeout_seconds
        )
    return ProbeOutcome(
        up=False, duration_seconds=0.0, error=f"unknown_kind: {probe.kind}"
    )  # pragma: no cover -- defensive


__all__ = [
    "ProbeOutcome",
    "execute_exec",
    "execute_http",
    "execute_metrics",
    "execute_resolved_probe",
    "execute_tcp",
]
