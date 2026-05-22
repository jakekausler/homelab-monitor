"""Resolve ProbeDescriptors into concrete probe targets.

Handles sentinel substitution:
  - `host` → host_ip
  - `container` → container_ip (for bridge net) or host_ip (for host net)

D-HOST-NETWORK-IP-RESOLVE: when network_mode == "host", both `host` AND
  `container` sentinels resolve to host_ip.
D-EXEC-PER-CONTAINER-LABEL + D-EXEC-OPT-IN: exec resolution requires
  BOTH exec_enabled (global flag) AND exec_authorized (per-container label).

`host.docker.internal:host-gateway` aliases are PRESERVED — when the user
writes http://host.docker.internal:port/, urlparse sees hostname
'host.docker.internal' which is NOT a sentinel, so substitution is skipped
and the docker daemon's host-gateway DNS handles routing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final
from urllib.parse import urlparse, urlunparse

from homelab_monitor.kernel.docker.label_parser import ProbeDescriptor

_HOST_SENTINEL: Final[str] = "host"
_CONTAINER_SENTINEL: Final[str] = "container"
_NETWORK_MODE_HOST: Final[str] = "host"


@dataclass(frozen=True, slots=True)
class ResolvedProbe:
    kind: str  # mirrors descriptor.kind
    name: str
    target: str  # for http/metrics: URL; for tcp: "host:port"
    exec_cmd: str | None  # for exec: the command; else None
    container_id: str | None  # for exec: the container id; else None


def resolve_probe(  # noqa: PLR0913, PLR0911
    descriptor: ProbeDescriptor,
    *,
    network_mode: str,
    container_ip: str | None,
    container_id: str,
    host_ip: str,
    exec_enabled: bool,
    exec_authorized: bool,
) -> ResolvedProbe | None:
    """Produce a concrete probe target, or None when gated off.

    Returns None when:
      - kind is 'exec' and (exec_enabled is False or exec_authorized is False).
      - kind is 'http'/'metrics'/'tcp' and the descriptor references the
        `container` sentinel but container_ip is None (and network_mode is
        not 'host'). The caller logs a warning.
    """
    if descriptor.kind == "exec":
        if not (exec_enabled and exec_authorized):
            return None
        return ResolvedProbe(
            kind="exec",
            name=descriptor.name,
            target=descriptor.raw_value,
            exec_cmd=descriptor.raw_value,
            container_id=container_id,
        )

    if descriptor.kind in ("http", "metrics"):
        resolved_url = _substitute_url_hostname(
            descriptor.raw_value,
            network_mode=network_mode,
            container_ip=container_ip,
            host_ip=host_ip,
        )
        if resolved_url is None:
            return None
        return ResolvedProbe(
            kind=descriptor.kind,
            name=descriptor.name,
            target=resolved_url,
            exec_cmd=None,
            container_id=None,
        )

    if descriptor.kind == "tcp":
        # raw_value is `tcp://host:port`. urlparse handles this.
        parsed = urlparse(descriptor.raw_value)
        host = parsed.hostname or ""
        port = parsed.port
        if port is None:
            return None  # pragma: no cover -- label_parser already filters
        resolved_host = _resolve_sentinel(
            host,
            network_mode=network_mode,
            container_ip=container_ip,
            host_ip=host_ip,
        )
        if resolved_host is None:
            return None
        return ResolvedProbe(
            kind="tcp",
            name=descriptor.name,
            target=f"{resolved_host}:{port}",
            exec_cmd=None,
            container_id=None,
        )
    return None  # pragma: no cover -- defensive


def _substitute_url_hostname(
    raw_url: str,
    *,
    network_mode: str,
    container_ip: str | None,
    host_ip: str,
) -> str | None:
    parsed = urlparse(raw_url)
    host = parsed.hostname or ""
    resolved_host = _resolve_sentinel(
        host,
        network_mode=network_mode,
        container_ip=container_ip,
        host_ip=host_ip,
    )
    if resolved_host is None:
        return None
    # Rebuild netloc — preserve port + userinfo if any.
    new_netloc = resolved_host
    if parsed.port is not None:
        new_netloc = f"{resolved_host}:{parsed.port}"
    if parsed.username:
        userinfo = parsed.username
        if parsed.password:
            userinfo = f"{userinfo}:{parsed.password}"
        new_netloc = f"{userinfo}@{new_netloc}"
    return urlunparse(
        (
            parsed.scheme,
            new_netloc,
            parsed.path,
            parsed.params,
            parsed.query,
            parsed.fragment,
        )
    )


def _resolve_sentinel(
    host: str,
    *,
    network_mode: str,
    container_ip: str | None,
    host_ip: str,
) -> str | None:
    """Substitute `host`/`container` sentinels; pass through everything else.

    Returns None when the descriptor needs `container_ip` but it's missing
    AND the container is NOT on host network.
    """
    if host == _HOST_SENTINEL:
        return host_ip
    if host == _CONTAINER_SENTINEL:
        if network_mode == _NETWORK_MODE_HOST:
            return host_ip
        if container_ip is None:
            return None
        return container_ip
    # Everything else (literal IP, hostname, host.docker.internal) passes through.
    return host


__all__ = ["ResolvedProbe", "resolve_probe"]
