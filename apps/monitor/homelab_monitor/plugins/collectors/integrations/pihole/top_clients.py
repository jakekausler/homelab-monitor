"""Pi-hole per-client (Tier-2) metrics collector — STAGE-006-012.

Polls 5 Pi-hole v6 endpoints and emits classified, capped per-client query/blocked
gauges plus top permitted/blocked domain gauges:

- /api/stats/top_clients              -> homelab_pihole_client_queries  (per kept client)
- /api/network/devices                -> MAC<->IP map (enriches client_mac label only)
- /api/stats/top_clients?blocked=true -> homelab_pihole_client_blocked  (per kept client)
- /api/stats/top_domains              -> homelab_pihole_top_permitted_domain (per kept domain)
- /api/stats/top_domains?blocked=true -> homelab_pihole_top_blocked_domain  (per kept domain)

Plus homelab_pihole_api_took_seconds{endpoint} per successful call and
homelab_metric_family_dropped_series{family} per capped family (always, value 0 when nothing
dropped). Resilience mirrors gravity.py: each endpoint has its own *_ok flag; a failed endpoint
appends an error but does not abort the run. network/devices is best-effort and NOT part of the
ok disjunction. ok = clients_ok or clients_blocked_ok or domains_ok or domains_blocked_ok.
"""

from __future__ import annotations

import time
from datetime import timedelta
from typing import ClassVar, cast

from homelab_monitor.kernel.config import (
    load_cardinality_caps_config,
    load_pihole_config,
)
from homelab_monitor.kernel.metrics.cardinality import M_FAMILY_DROPPED_SERIES
from homelab_monitor.kernel.pihole.clients import RawClient, cap_domains, classify_clients
from homelab_monitor.kernel.pihole.errors import PiholeError
from homelab_monitor.kernel.plugins.base import BaseCollector
from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.types import CollectorResult
from homelab_monitor.plugins.collectors.integrations.pihole._parsing import as_float

# --- Public metric name constants (contract; literal-asserted in tests) ---------
M_API_TOOK = "homelab_pihole_api_took_seconds"
M_CLIENT_QUERIES = "homelab_pihole_client_queries"
M_CLIENT_BLOCKED = "homelab_pihole_client_blocked"
M_TOP_BLOCKED_DOMAIN = "homelab_pihole_top_blocked_domain"
M_TOP_PERMITTED_DOMAIN = "homelab_pihole_top_permitted_domain"

# --- Cap-config family keys (match _DEFAULT_CARDINALITY_FAMILIES) ----------------
_CAP_CLIENTS = "pihole_client_queries"
_CAP_DOMAINS = "pihole_top_domains"

# Ask the API for cap + headroom so the cap is actually exercisable end-to-end.
_REQUEST_HEADROOM = 10


def _parse_clients(
    payload: dict[str, object],
) -> tuple[list[tuple[str, str, float]], dict[str, float]]:
    """Parse the top_clients payload into (temp_clients, value_by_ip).

    temp_clients is a list of (ip, name, value) for every well-formed client entry.
    value_by_ip maps ip -> value (last write wins). Mis-shaped entries are skipped.
    Returns ([], {}) when "clients" is missing or not a list.
    """
    temp: list[tuple[str, str, float]] = []
    value_by_ip: dict[str, float] = {}
    clients_obj = payload.get("clients")
    if not isinstance(clients_obj, list):
        return temp, value_by_ip
    clients_seq = cast("list[object]", clients_obj)
    for entry in clients_seq:
        if not isinstance(entry, dict):
            continue
        e = cast("dict[str, object]", entry)
        ip_obj = e.get("ip")
        if not isinstance(ip_obj, str) or not ip_obj:
            continue
        name_obj = e.get("name")
        name = name_obj if isinstance(name_obj, str) else ""
        value = as_float(e.get("count")) or 0.0
        temp.append((ip_obj, name, value))
        value_by_ip[ip_obj] = value
    return temp, value_by_ip


def _build_blocked_map(payload: dict[str, object]) -> dict[str, float]:
    """Build ip -> blocked_count from the top_clients?blocked payload.

    Returns {} when "clients" is missing or not a list. Mis-shaped entries skipped.
    """
    result: dict[str, float] = {}
    clients_obj = payload.get("clients")
    if not isinstance(clients_obj, list):
        return result
    clients_seq = cast("list[object]", clients_obj)
    for entry in clients_seq:
        if not isinstance(entry, dict):
            continue
        e = cast("dict[str, object]", entry)
        ip_obj = e.get("ip")
        if not isinstance(ip_obj, str) or not ip_obj:
            continue
        result[ip_obj] = as_float(e.get("count")) or 0.0
    return result


def _flatten_ip_mac(payload: dict[str, object]) -> dict[str, str]:
    """Flatten /api/network/devices into ip -> hwaddr (first-MAC-wins per ip).

    Returns {} when "devices" is missing or not a list. Skips devices/ip entries
    that are not well-formed (non-dict, missing/non-str/empty hwaddr or ip, non-list
    ips). One device (MAC) may carry multiple ips.
    """
    result: dict[str, str] = {}
    devices_obj = payload.get("devices")
    if not isinstance(devices_obj, list):
        return result
    devices_seq = cast("list[object]", devices_obj)
    for device in devices_seq:
        if not isinstance(device, dict):
            continue
        d = cast("dict[str, object]", device)
        hwaddr = d.get("hwaddr")
        if not isinstance(hwaddr, str) or not hwaddr:
            continue
        ips_obj = d.get("ips")
        if not isinstance(ips_obj, list):
            continue
        ips_seq = cast("list[object]", ips_obj)
        for ip_entry in ips_seq:
            if not isinstance(ip_entry, dict):
                continue
            ie = cast("dict[str, object]", ip_entry)
            ip = ie.get("ip")
            if not isinstance(ip, str) or not ip:
                continue
            result.setdefault(ip, hwaddr)
    return result


def _parse_domains(payload: dict[str, object]) -> list[tuple[str, float]]:
    """Parse a top_domains payload into [(domain, value)].

    Returns [] when "domains" is missing or not a list. Skips entries missing/with a
    non-str domain. Non-numeric counts default to 0.0.
    """
    pairs: list[tuple[str, float]] = []
    domains_obj = payload.get("domains")
    if not isinstance(domains_obj, list):
        return pairs
    domains_seq = cast("list[object]", domains_obj)
    for entry in domains_seq:
        if not isinstance(entry, dict):
            continue
        e = cast("dict[str, object]", entry)
        domain_obj = e.get("domain")
        if not isinstance(domain_obj, str) or not domain_obj:
            continue
        pairs.append((domain_obj, as_float(e.get("count")) or 0.0))
    return pairs


def _emit_domains(
    ctx: CollectorContext,
    payload: dict[str, object],
    cap: int,
    metric: str,
    emitted: list[int],
) -> None:
    """Cap + emit one top_domains family, then its drop gauge (always)."""
    pairs = _parse_domains(payload)
    cap_result = cap_domains(pairs, cap)
    for labels, value in cap_result.survivors:
        ctx.vm.write_gauge(metric, value, labels)
        emitted[0] += 1
    ctx.vm.write_gauge(M_FAMILY_DROPPED_SERIES, float(cap_result.dropped), {"family": metric})
    emitted[0] += 1


class PiholeClientsCollector(BaseCollector):
    """Poll Pi-hole per-client + top-domain endpoints; emit capped, classified gauges."""

    name: ClassVar[str] = "pihole_clients"
    interval: ClassVar[timedelta] = timedelta(seconds=60)
    timeout: ClassVar[timedelta] = timedelta(seconds=15)
    concurrency_group: ClassVar[str] = "pihole"

    async def run(self, ctx: CollectorContext) -> CollectorResult:  # noqa: PLR0912, PLR0915
        """Poll 5 endpoints, emit gauges, return CollectorResult."""
        start = time.monotonic()

        if ctx.pihole is None:
            return CollectorResult(
                ok=False,
                metrics_emitted=0,
                errors=["pihole client not configured"],
                events=[],
                duration_seconds=time.monotonic() - start,
            )

        caps = load_cardinality_caps_config()
        client_cap = caps.cap_for(_CAP_CLIENTS)
        domain_cap = caps.cap_for(_CAP_DOMAINS)
        host_lan_ip = load_pihole_config().host_lan_ip
        client_request_count = client_cap + _REQUEST_HEADROOM
        domain_request_count = domain_cap + _REQUEST_HEADROOM

        emitted: list[int] = [0]
        errors: list[str] = []
        clients_ok = False
        clients_blocked_ok = False
        domains_ok = False
        domains_blocked_ok = False

        temp_clients: list[tuple[str, str, float]] = []
        value_by_ip: dict[str, float] = {}
        ip_mac: dict[str, str] = {}
        blocked_by_ip: dict[str, float] = {}

        # --- Endpoint 1: top_clients (permitted/total per client) ---
        clients_result = await ctx.pihole.stats_top_clients(count=client_request_count)
        if isinstance(clients_result, PiholeError):
            errors.append(clients_result.message)
        else:
            ctx.vm.write_gauge(
                M_API_TOOK, clients_result.took_seconds, {"endpoint": clients_result.endpoint}
            )
            emitted[0] += 1
            clients_ok = True
            clients_payload: object = clients_result.payload
            if isinstance(clients_payload, dict):
                temp_clients, value_by_ip = _parse_clients(
                    cast("dict[str, object]", clients_payload)
                )

        # --- Endpoint 2: network/devices (MAC map; best-effort, not in ok) ---
        devices_result = await ctx.pihole.network_devices()
        if isinstance(devices_result, PiholeError):
            errors.append(devices_result.message)
        else:
            ctx.vm.write_gauge(
                M_API_TOOK, devices_result.took_seconds, {"endpoint": devices_result.endpoint}
            )
            emitted[0] += 1
            devices_payload: object = devices_result.payload
            if isinstance(devices_payload, dict):
                ip_mac = _flatten_ip_mac(cast("dict[str, object]", devices_payload))

        # --- Endpoint 3: top_clients?blocked (blocked per client) ---
        blocked_result = await ctx.pihole.stats_top_clients(
            blocked=True, count=client_request_count
        )
        if isinstance(blocked_result, PiholeError):
            errors.append(blocked_result.message)
        else:
            ctx.vm.write_gauge(
                M_API_TOOK, blocked_result.took_seconds, {"endpoint": blocked_result.endpoint}
            )
            emitted[0] += 1
            clients_blocked_ok = True
            blocked_payload: object = blocked_result.payload
            if isinstance(blocked_payload, dict):
                blocked_by_ip = _build_blocked_map(cast("dict[str, object]", blocked_payload))

        # --- Classify ONCE + emit per-client families (only if top_clients call OK) ---
        if clients_ok:
            # mac may be FTL's synthetic "ip-<addr>" hwaddr for MAC-less/loopback
            # clients — passed through verbatim (not a bug).
            raw_clients = [
                RawClient(ip, name, value, mac=ip_mac.get(ip)) for (ip, name, value) in temp_clients
            ]
            classification = classify_clients(raw_clients, host_lan_ip=host_lan_ip, cap=client_cap)
            for cc in classification.kept:
                labels = {
                    "client_ip": cc.client_ip,
                    "client_name": cc.client_name,
                    "client_kind": cc.client_kind,
                    "host_lan_ip": cc.host_lan_ip or "",
                    "client_mac": cc.client_mac or "",
                }
                ctx.vm.write_gauge(M_CLIENT_QUERIES, value_by_ip.get(cc.client_ip, 0.0), labels)
                emitted[0] += 1
                ctx.vm.write_gauge(M_CLIENT_BLOCKED, blocked_by_ip.get(cc.client_ip, 0.0), labels)
                emitted[0] += 1
            dropped = float(classification.dropped)
            ctx.vm.write_gauge(M_FAMILY_DROPPED_SERIES, dropped, {"family": M_CLIENT_QUERIES})
            emitted[0] += 1
            ctx.vm.write_gauge(M_FAMILY_DROPPED_SERIES, dropped, {"family": M_CLIENT_BLOCKED})
            emitted[0] += 1

        # --- Endpoint 4: top_domains (permitted) ---
        domains_result = await ctx.pihole.stats_top_domains(count=domain_request_count)
        if isinstance(domains_result, PiholeError):
            errors.append(domains_result.message)
        else:
            ctx.vm.write_gauge(
                M_API_TOOK, domains_result.took_seconds, {"endpoint": domains_result.endpoint}
            )
            emitted[0] += 1
            domains_ok = True
            domains_payload: object = domains_result.payload
            if isinstance(domains_payload, dict):
                _emit_domains(
                    ctx,
                    cast("dict[str, object]", domains_payload),
                    domain_cap,
                    M_TOP_PERMITTED_DOMAIN,
                    emitted,
                )

        # --- Endpoint 5: top_domains?blocked ---
        domains_blocked_result = await ctx.pihole.stats_top_domains(
            blocked=True, count=domain_request_count
        )
        if isinstance(domains_blocked_result, PiholeError):
            errors.append(domains_blocked_result.message)
        else:
            ctx.vm.write_gauge(
                M_API_TOOK,
                domains_blocked_result.took_seconds,
                {"endpoint": domains_blocked_result.endpoint},
            )
            emitted[0] += 1
            domains_blocked_ok = True
            domains_blocked_payload: object = domains_blocked_result.payload
            if isinstance(domains_blocked_payload, dict):
                _emit_domains(
                    ctx,
                    cast("dict[str, object]", domains_blocked_payload),
                    domain_cap,
                    M_TOP_BLOCKED_DOMAIN,
                    emitted,
                )

        return CollectorResult(
            ok=clients_ok or clients_blocked_ok or domains_ok or domains_blocked_ok,
            metrics_emitted=emitted[0],
            errors=errors,
            events=[],
            duration_seconds=time.monotonic() - start,
        )
