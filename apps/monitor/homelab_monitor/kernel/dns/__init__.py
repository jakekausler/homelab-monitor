"""Pure-stdlib async UDP DNS probe helper (STAGE-006-014).

A minimal, reusable A-record query primitive used by the Pi-hole DNS health
collector to perform an INDEPENDENT real DNS resolution over UDP :53 — no
dnspython, no docker socket, no ProbeSupervisor. The UDP analogue of
``kernel/docker/probe_executor.py::execute_tcp``.
"""

from __future__ import annotations

from homelab_monitor.kernel.dns.resolver import DnsProbeResult, resolve_a

__all__ = ["DnsProbeResult", "resolve_a"]
