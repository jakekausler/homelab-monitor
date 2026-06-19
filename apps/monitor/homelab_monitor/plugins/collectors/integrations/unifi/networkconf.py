"""unifi_networkconf collector -- DHCP pool / DNS-steering / network-count from rest/networkconf.

Polls the classic ``rest/networkconf`` endpoint once per 5m tick and, for each
DHCP-enabled non-WAN network, emits:

- ``homelab_unifi_dhcp_pool_size{network=...}`` -- the number of addresses in the
  DHCP pool (``stop - start + 1``), emitted only when BOTH dhcpd_start and
  dhcpd_stop parse to valid IP addresses of the SAME family with stop >= start.
- ``homelab_unifi_dhcp_pool_start{network=...}`` -- the integer value of the pool
  start IP (emitted only alongside pool_size).
- ``homelab_unifi_dhcp_pool_end{network=...}`` -- the integer value of the pool
  stop IP (emitted only alongside pool_size).
- ``homelab_unifi_dhcp_dns_primary{network=..., dns=...}`` -- an info-gauge whose
  value is always ``1.0``; the steered primary DNS IP lives in the ``dns`` label.
  Emitted only when ``dhcpd_dns_1`` is a non-empty string.
- ``homelab_unifi_dhcp_enabled_network_count`` -- an ALWAYS-PRESENT GAUGE (no
  labels) of the number of DHCP-enabled non-WAN networks seen this poll. Emitted
  as ``0.0`` when there are none, so the series is never absent (avoids the
  ``absent()`` trap).
- ``homelab_unifi_api_took_seconds{endpoint="rest/networkconf"}`` -- API latency
  (always emitted on a successful fetch).

A network is considered for emit only when ``purpose != "wan"`` AND
``dhcpd_enabled`` is True. A network with a malformed pool range (missing /
non-IP dhcpd_start or dhcpd_stop, mixed IPv4/IPv6 families, or stop < start)
emits NO pool gauges but still counts toward the network count and may still
emit the dns gauge.

NO SuggestionEvents. NO cardinality cap (the network space is tiny).

FAILURE / OK SEMANTICS (mirrors the sibling collectors):
- ``ctx.unifi is None`` -> ok=False, errors=["unifi client not configured"], no emits.
- ``rest_networkconf()`` returns UnifiError -> ok=False, errors=[message], no emits.
- A 200-with-malformed-body (payload not a dict, or data not a list) -> records=[],
  ok=True: latency + network_count=0.0 emitted, no per-network series.
- Empty data (``[]``) -> ok=True, same as above.
"""

from __future__ import annotations

import ipaddress
import time
from datetime import timedelta
from typing import ClassVar, Final, cast

from homelab_monitor.kernel.plugins.base import BaseCollector
from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.types import CollectorResult
from homelab_monitor.kernel.unifi.errors import UnifiError
from homelab_monitor.plugins.collectors.integrations.unifi._parsing import as_bool

# --- Metric names ---------------------------------------------------------------
M_POOL_SIZE: Final[str] = "homelab_unifi_dhcp_pool_size"
M_POOL_START: Final[str] = "homelab_unifi_dhcp_pool_start"
M_POOL_END: Final[str] = "homelab_unifi_dhcp_pool_end"
M_DNS_PRIMARY: Final[str] = "homelab_unifi_dhcp_dns_primary"
M_DHCP_NETWORK_COUNT: Final[str] = "homelab_unifi_dhcp_enabled_network_count"


def _parse_records(payload: object) -> list[dict[str, object]]:
    """Narrow a classic ``{"data":[...]}`` payload to a list of record dicts.

    Returns [] when the payload is not a dict, ``data`` is not a list, or there are
    no dict entries. Non-dict entries are skipped.
    """
    if not isinstance(payload, dict):
        return []
    payload_dict = cast("dict[str, object]", payload)
    data_obj = payload_dict.get("data")
    if not isinstance(data_obj, list):
        return []
    data = cast("list[object]", data_obj)
    return [cast("dict[str, object]", r) for r in data if isinstance(r, dict)]


def _parse_ip(value: object) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Parse an IPv4/IPv6 string to its address object; None on any failure.

    Non-string input -> None. Empty string -> None. An unparseable IP string
    raises ValueError internally and yields None. A valid IP string yields the
    parsed address object (carrying its .version for same-family checks).
    """
    if isinstance(value, str) and value:
        try:
            return ipaddress.ip_address(value.strip())
        except ValueError:
            return None
    return None


class UnifiNetworkconfCollector(BaseCollector):
    """Emit DHCP pool / DNS-steering / network-count gauges from rest/networkconf.

    Reads classic ``rest/networkconf`` once per 5m tick and, for each DHCP-enabled
    non-WAN network, emits pool-size/start/end, a primary-DNS info-gauge, and an
    always-present count of DHCP-enabled networks.
    """

    name: ClassVar[str] = "unifi_networkconf"
    interval: ClassVar[timedelta] = timedelta(seconds=300)
    timeout: ClassVar[timedelta] = timedelta(seconds=15)
    concurrency_group: ClassVar[str] = "unifi"

    async def run(self, ctx: CollectorContext) -> CollectorResult:
        """Poll rest/networkconf and emit DHCP pool / DNS / network-count gauges."""
        start = time.monotonic()
        if ctx.unifi is None:
            return CollectorResult(
                ok=False,
                metrics_emitted=0,
                errors=["unifi client not configured"],
                duration_seconds=time.monotonic() - start,
            )

        resp = await ctx.unifi.rest_networkconf()
        if isinstance(resp, UnifiError):
            return CollectorResult(
                ok=False,
                metrics_emitted=0,
                errors=[resp.message],
                duration_seconds=time.monotonic() - start,
            )

        emitted = 0

        # Always emit the API latency gauge (graceful-degrade indicator).
        ctx.vm.write_gauge(
            "homelab_unifi_api_took_seconds",
            resp.took_seconds,
            {"endpoint": resp.endpoint},
        )
        emitted += 1

        records = _parse_records(resp.payload)

        dhcp_network_count = 0
        for rec in records:
            # Filter: only DHCP-enabled, non-WAN networks.
            purpose = rec.get("purpose")
            dhcpd_enabled = as_bool(rec.get("dhcpd_enabled"))
            if purpose == "wan" or not dhcpd_enabled:
                continue

            # Need a usable network label (non-empty string name).
            name = rec.get("name")
            if not (isinstance(name, str) and name):
                continue
            network = name

            dhcp_network_count += 1

            # Pool size/start/end -- emit all three only when both IPs parse to
            # the same address family and the range is correctly ordered.
            start_addr = _parse_ip(rec.get("dhcpd_start"))
            stop_addr = _parse_ip(rec.get("dhcpd_stop"))
            if (
                start_addr is not None
                and stop_addr is not None
                and start_addr.version == stop_addr.version
                and int(stop_addr) >= int(start_addr)
            ):
                start_int = int(start_addr)
                stop_int = int(stop_addr)
                ctx.vm.write_gauge(
                    M_POOL_SIZE,
                    float(stop_int - start_int + 1),
                    {"network": network},
                )
                emitted += 1
                ctx.vm.write_gauge(M_POOL_START, float(start_int), {"network": network})
                emitted += 1
                ctx.vm.write_gauge(M_POOL_END, float(stop_int), {"network": network})
                emitted += 1

            # Primary DNS steering -- info-gauge (value 1.0, IP in the label).
            dns1 = rec.get("dhcpd_dns_1")
            if isinstance(dns1, str) and dns1:
                ctx.vm.write_gauge(
                    M_DNS_PRIMARY,
                    1.0,
                    {"network": network, "dns": dns1},
                )
                emitted += 1

        # Always emit the count (0.0 when none) with NO labels -- never absent.
        ctx.vm.write_gauge(M_DHCP_NETWORK_COUNT, float(dhcp_network_count), {})
        emitted += 1

        return CollectorResult(
            ok=True,
            metrics_emitted=emitted,
            errors=[],
            duration_seconds=time.monotonic() - start,
        )
