"""Unit tests for UnifiNetworkconfCollector (STAGE-007-011).

Covers the always-present network count (count == 0.0 on empty), per-network
pool-size/start/end gauges, the primary-DNS info-gauge, the WAN-skip and
dhcp-disabled-skip filter branches, the name-guard skip, the malformed-pool-range
path (no pool gauges but still counts + dns), the missing-DNS path, the
_parse_ip helper branches, the malformed-payload paths (non-dict payload,
non-list data, non-dict entry), and both error paths (None client, UnifiError).
"""

from __future__ import annotations

import ipaddress

import pytest
import structlog

from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.io import (
    InMemoryLogsWriter,
    InMemoryMetricsWriter,
    MetricEntry,
)
from homelab_monitor.kernel.plugins.types import CollectorConfig
from homelab_monitor.kernel.unifi.client import UnifiResponse
from homelab_monitor.kernel.unifi.errors import UnifiError
from homelab_monitor.plugins.collectors.integrations.unifi.networkconf import (
    M_DHCP_NETWORK_COUNT,
    M_DNS_PRIMARY,
    M_POOL_END,
    M_POOL_SIZE,
    M_POOL_START,
    UnifiNetworkconfCollector,
    _parse_ip,  # pyright: ignore[reportPrivateUsage]
    _parse_records,  # pyright: ignore[reportPrivateUsage]
)

_NETCONF_ENDPOINT = "rest/networkconf"

# Pre-computed integer IP values (verified with python ipaddress).
_IP_START = 3232236038  # int(ipaddress.ip_address("192.168.2.6"))
_IP_STOP = 3232236286  # int(ipaddress.ip_address("192.168.2.254"))


# --- assertion helpers (mirror test_unifi_alarms_collector.py) ----------
def _gauges(writer: InMemoryMetricsWriter, name: str) -> list[MetricEntry]:
    """Return all recorded gauge entries with the given metric name."""
    return [
        e
        for e in writer.recorded  # pyright: ignore[reportPrivateUsage]
        if e.kind == "gauge" and e.name == name
    ]


def _gauge_value(writer: InMemoryMetricsWriter, name: str, labels: dict[str, str]) -> float | None:
    """Return the value of the gauge matching name + exact labels, or None."""
    for e in writer.recorded:  # pyright: ignore[reportPrivateUsage]
        if e.kind == "gauge" and e.name == name and e.labels == labels:
            return e.value
    return None


# --- fake clients (conform to the UnifiClient Protocol via the base) ----------
class _FakeUnifiBase:
    """Base fake UnifiClient: every method returns a stub UnifiError."""

    site_name: str = "default"
    v1_site_id: str = "fake-uuid"

    async def v1_sites(self) -> UnifiResponse | UnifiError:
        return UnifiError(reason="bad_response", message="stub")

    async def v1_devices(self) -> UnifiResponse | UnifiError:
        return UnifiError(reason="bad_response", message="stub")

    async def v1_device(self, device_id: str) -> UnifiResponse | UnifiError:
        return UnifiError(reason="bad_response", message="stub")

    async def v1_device_stats(self, device_id: str) -> UnifiResponse | UnifiError:
        return UnifiError(reason="bad_response", message="stub")

    async def v1_clients(self) -> UnifiResponse | UnifiError:
        return UnifiError(reason="bad_response", message="stub")

    async def stat_device(self) -> UnifiResponse | UnifiError:
        return UnifiError(reason="bad_response", message="stub")

    async def stat_sta(self) -> UnifiResponse | UnifiError:
        return UnifiError(reason="bad_response", message="stub")

    async def stat_alluser(self) -> UnifiResponse | UnifiError:
        return UnifiError(reason="bad_response", message="stub")

    async def stat_health(self) -> UnifiResponse | UnifiError:
        return UnifiError(reason="bad_response", message="stub")

    async def stat_stadpi(self) -> UnifiResponse | UnifiError:
        return UnifiError(reason="bad_response", message="stub")

    async def rest_networkconf(self) -> UnifiResponse | UnifiError:
        return UnifiError(reason="bad_response", message="stub")

    async def rest_alarm(self) -> UnifiResponse | UnifiError:
        return UnifiError(reason="bad_response", message="stub")

    async def stat_sysinfo(self) -> UnifiResponse | UnifiError:
        return UnifiError(reason="bad_response", message="stub")

    async def v2_traffic(self, start_ms: int, end_ms: int) -> UnifiResponse | UnifiError:
        return UnifiError(reason="bad_response", message="stub")

    async def resolve_site_id(self) -> UnifiError | None:
        return None


class _FakeNetconfOk(_FakeUnifiBase):
    """rest_networkconf returns a fixed classic payload wrapping the given records."""

    def __init__(self, records: list[object], took: float = 0.012) -> None:
        self._records = records
        self._took = took

    async def rest_networkconf(self) -> UnifiResponse | UnifiError:
        payload: dict[str, object] = {"meta": {"rc": "ok"}, "data": self._records}
        return UnifiResponse(payload=payload, took_seconds=self._took, endpoint=_NETCONF_ENDPOINT)


class _FakeNetconfRawPayload(_FakeUnifiBase):
    """rest_networkconf returns an arbitrary (possibly non-dict) payload."""

    def __init__(self, payload: object, took: float = 0.01) -> None:
        self._payload = payload
        self._took = took

    async def rest_networkconf(self) -> UnifiResponse | UnifiError:
        return UnifiResponse(
            payload=self._payload, took_seconds=self._took, endpoint=_NETCONF_ENDPOINT
        )


class _FakeNetconfFail(_FakeUnifiBase):
    """rest_networkconf returns a UnifiError."""

    async def rest_networkconf(self) -> UnifiResponse | UnifiError:
        return UnifiError(reason="timeout", message="networkconf timed out")


def _ctx(writer: InMemoryMetricsWriter, unifi: object | None) -> CollectorContext:
    """Build a CollectorContext. Mirrors the alarms test's _ctx(); db/http/ssh/
    secrets are unused by this collector. Note: CollectorConfig has
    extra='forbid' -- do NOT pass concurrency_group (it is a ClassVar)."""
    return CollectorContext(
        config=CollectorConfig(
            name="unifi_networkconf",
            interval_seconds=300,
            timeout_seconds=15,
        ),
        db=None,  # type: ignore[arg-type]
        vm=writer,
        vl=InMemoryLogsWriter(),
        http=None,  # pyright: ignore[reportArgumentType]
        ssh=None,  # pyright: ignore[reportArgumentType]
        secrets=None,  # pyright: ignore[reportArgumentType]
        log=structlog.get_logger().bind(collector="unifi_networkconf"),
        unifi=unifi,  # type: ignore[arg-type]
    )


# --- synthetic rest/networkconf fixture builder -------------------------------
def _network(  # noqa: PLR0913
    *,
    name: object = None,
    purpose: object = None,
    dhcpd_enabled: object = None,
    dhcpd_start: object = None,
    dhcpd_stop: object = None,
    dhcpd_dns_1: object = None,
) -> dict[str, object]:
    """Build one networkconf record; include a field only when explicitly provided.

    Passing None for a kwarg omits that key entirely (to exercise missing-field
    branches). Pass an explicit value (str / "" / int / bool) to set it.
    """
    rec: dict[str, object] = {}
    if name is not None:
        rec["name"] = name
    if purpose is not None:
        rec["purpose"] = purpose
    if dhcpd_enabled is not None:
        rec["dhcpd_enabled"] = dhcpd_enabled
    if dhcpd_start is not None:
        rec["dhcpd_start"] = dhcpd_start
    if dhcpd_stop is not None:
        rec["dhcpd_stop"] = dhcpd_stop
    if dhcpd_dns_1 is not None:
        rec["dhcpd_dns_1"] = dhcpd_dns_1
    return rec


def _full_default_network() -> dict[str, object]:
    """The live "Default" corporate DHCP network (mirrors the real UDM payload)."""
    return _network(
        name="Default",
        purpose="corporate",
        dhcpd_enabled=True,
        dhcpd_start="192.168.2.6",
        dhcpd_stop="192.168.2.254",
        dhcpd_dns_1="192.168.2.148",
    )


# ============================================================================
# _parse_records unit tests (malformed-payload branches in isolation).
# ============================================================================
def test_parse_records_non_dict_payload() -> None:
    """Non-dict payload -> []."""
    assert _parse_records(["not", "a", "dict"]) == []


def test_parse_records_data_not_a_list() -> None:
    """Dict payload whose data is not a list -> []."""
    payload: dict[str, object] = {"meta": {}, "data": None}
    assert _parse_records(payload) == []


def test_parse_records_skips_non_dict_entries() -> None:
    """Non-dict entries in data are skipped; dict entries are kept."""
    good: dict[str, object] = {"name": "Default"}
    payload: dict[str, object] = {"data": ["string-entry", 42, good]}
    assert _parse_records(payload) == [good]


# ============================================================================
# _parse_ip unit tests (all branches in isolation).
# ============================================================================
def test_parse_ip_valid_ipv4() -> None:
    """A valid IPv4 string -> address object whose int matches _IP_START."""
    result = _parse_ip("192.168.2.6")
    assert result is not None
    assert int(result) == _IP_START


def test_parse_ip_valid_ipv6() -> None:
    """A valid IPv6 string -> address object with version 6."""
    result = _parse_ip("fe80::1")
    assert result is not None
    assert result.version == 6  # noqa: PLR2004


def test_parse_ip_non_str() -> None:
    """Non-string input -> None."""
    assert _parse_ip(123) is None
    assert _parse_ip(None) is None


def test_parse_ip_empty_str() -> None:
    """Empty string -> None."""
    assert _parse_ip("") is None


def test_parse_ip_invalid_str() -> None:
    """Unparseable IP string -> None (ValueError path)."""
    assert _parse_ip("nope") is None


# ============================================================================
# Test 1: None client -> ok=False, no emits.
# ============================================================================
@pytest.mark.asyncio
async def test_none_client_fails() -> None:
    """ctx.unifi is None -> ok=False, metrics_emitted==0, nothing emitted."""
    writer = InMemoryMetricsWriter()
    result = await UnifiNetworkconfCollector().run(_ctx(writer, None))
    assert result.ok is False
    assert result.metrics_emitted == 0
    assert result.errors == ["unifi client not configured"]
    assert writer.recorded == []  # pyright: ignore[reportPrivateUsage]


# ============================================================================
# Test 2: rest_networkconf UnifiError -> ok=False, no emits.
# ============================================================================
@pytest.mark.asyncio
async def test_networkconf_error_fails() -> None:
    """rest_networkconf UnifiError -> ok=False, errors=[message], nothing emitted."""
    writer = InMemoryMetricsWriter()
    result = await UnifiNetworkconfCollector().run(_ctx(writer, _FakeNetconfFail()))
    assert result.ok is False
    assert result.metrics_emitted == 0
    assert result.errors == ["networkconf timed out"]
    assert writer.recorded == []  # pyright: ignore[reportPrivateUsage]


# ============================================================================
# Test 3: empty data ([]) -> ok=True, latency + count=0.0, no pool/dns series.
# ============================================================================
@pytest.mark.asyncio
async def test_empty_data_emits_count_zero() -> None:
    """data == [] -> ok=True: latency + count 0.0, no pool/dns series."""
    writer = InMemoryMetricsWriter()
    records: list[object] = []
    result = await UnifiNetworkconfCollector().run(_ctx(writer, _FakeNetconfOk(records)))

    assert result.ok is True
    assert (
        _gauge_value(writer, "homelab_unifi_api_took_seconds", {"endpoint": _NETCONF_ENDPOINT})
        == 0.012  # noqa: PLR2004
    )
    assert _gauge_value(writer, M_DHCP_NETWORK_COUNT, {}) == 0.0
    assert _gauges(writer, M_POOL_SIZE) == []
    assert _gauges(writer, M_DNS_PRIMARY) == []
    # latency + count only.
    assert result.metrics_emitted == 2  # noqa: PLR2004


# ============================================================================
# Test 4: non-dict payload -> _parse_records [] (FALSE dict-guard).
# ============================================================================
@pytest.mark.asyncio
async def test_non_dict_payload_count_zero() -> None:
    """payload is a list (not a dict) -> records=[]: ok=True, count 0.0, latency."""
    writer = InMemoryMetricsWriter()
    payload: list[str] = ["not", "a", "dict"]
    result = await UnifiNetworkconfCollector().run(_ctx(writer, _FakeNetconfRawPayload(payload)))

    assert result.ok is True
    assert _gauge_value(writer, M_DHCP_NETWORK_COUNT, {}) == 0.0
    assert (
        _gauge_value(writer, "homelab_unifi_api_took_seconds", {"endpoint": _NETCONF_ENDPOINT})
        == 0.01  # noqa: PLR2004
    )
    assert _gauges(writer, M_POOL_SIZE) == []


# ============================================================================
# Test 5: dict payload with non-list data -> [] (FALSE data-list guard).
# ============================================================================
@pytest.mark.asyncio
async def test_data_not_a_list_count_zero() -> None:
    """payload is a dict whose data is not a list -> records=[]: ok=True, count 0.0."""
    writer = InMemoryMetricsWriter()
    payload: dict[str, object] = {"meta": {"rc": "ok"}, "data": None}
    result = await UnifiNetworkconfCollector().run(_ctx(writer, _FakeNetconfRawPayload(payload)))

    assert result.ok is True
    assert _gauge_value(writer, M_DHCP_NETWORK_COUNT, {}) == 0.0
    assert _gauges(writer, M_POOL_SIZE) == []


# ============================================================================
# Test 6: non-dict entry in data -> skipped by _parse_records.
# ============================================================================
@pytest.mark.asyncio
async def test_non_dict_entry_skipped() -> None:
    """A string entry alongside a valid network -> string skipped, network counted."""
    records: list[object] = ["not-a-dict", _full_default_network()]
    writer = InMemoryMetricsWriter()
    result = await UnifiNetworkconfCollector().run(_ctx(writer, _FakeNetconfOk(records)))

    assert result.ok is True
    assert _gauge_value(writer, M_DHCP_NETWORK_COUNT, {}) == 1.0


# ============================================================================
# Test 7: a WAN record -> skipped (purpose=="wan" branch).
# ============================================================================
@pytest.mark.asyncio
async def test_wan_record_skipped() -> None:
    """A purpose=='wan' record contributes nothing (no count, no series)."""
    wan = _network(name="WAN", purpose="wan")
    writer = InMemoryMetricsWriter()
    result = await UnifiNetworkconfCollector().run(_ctx(writer, _FakeNetconfOk([wan])))

    assert result.ok is True
    assert _gauge_value(writer, M_DHCP_NETWORK_COUNT, {}) == 0.0
    assert _gauges(writer, M_POOL_SIZE) == []
    assert _gauges(writer, M_DNS_PRIMARY) == []


# ============================================================================
# Test 8: dhcpd_enabled False / absent -> skipped (not dhcpd_enabled branch).
# ============================================================================
@pytest.mark.asyncio
async def test_dhcp_disabled_record_skipped() -> None:
    """A non-WAN record with dhcpd_enabled False (or absent) contributes nothing."""
    disabled = _network(name="Guest", purpose="guest", dhcpd_enabled=False)
    absent = _network(name="IoT", purpose="corporate")  # dhcpd_enabled omitted
    writer = InMemoryMetricsWriter()
    result = await UnifiNetworkconfCollector().run(_ctx(writer, _FakeNetconfOk([disabled, absent])))

    assert result.ok is True
    assert _gauge_value(writer, M_DHCP_NETWORK_COUNT, {}) == 0.0
    assert _gauges(writer, M_POOL_SIZE) == []


# ============================================================================
# Test 9: missing / empty / non-str name -> skipped (name-guard FALSE side).
# ============================================================================
@pytest.mark.asyncio
async def test_unusable_name_records_skipped() -> None:
    """A dhcp-enabled record with missing or empty name contributes nothing."""
    missing_name = _network(purpose="corporate", dhcpd_enabled=True)  # name omitted
    empty_name = _network(name="", purpose="corporate", dhcpd_enabled=True)
    writer = InMemoryMetricsWriter()
    result = await UnifiNetworkconfCollector().run(
        _ctx(writer, _FakeNetconfOk([missing_name, empty_name]))
    )

    assert result.ok is True
    assert _gauge_value(writer, M_DHCP_NETWORK_COUNT, {}) == 0.0
    assert _gauges(writer, M_POOL_SIZE) == []


# ============================================================================
# Test 10: a FULL dhcp network -> all gauges with exact values.
# ============================================================================
@pytest.mark.asyncio
async def test_full_network_emits_all_gauges() -> None:
    """The live "Default" network -> pool size/start/end, dns, count, latency."""
    writer = InMemoryMetricsWriter()
    result = await UnifiNetworkconfCollector().run(
        _ctx(writer, _FakeNetconfOk([_full_default_network()]))
    )

    assert result.ok is True
    assert _gauge_value(writer, M_POOL_SIZE, {"network": "Default"}) == 249.0  # noqa: PLR2004
    assert _gauge_value(writer, M_POOL_START, {"network": "Default"}) == float(_IP_START)
    assert _gauge_value(writer, M_POOL_END, {"network": "Default"}) == float(_IP_STOP)
    assert (
        _gauge_value(writer, M_DNS_PRIMARY, {"network": "Default", "dns": "192.168.2.148"}) == 1.0
    )
    assert _gauge_value(writer, M_DHCP_NETWORK_COUNT, {}) == 1.0
    assert (
        _gauge_value(writer, "homelab_unifi_api_took_seconds", {"endpoint": _NETCONF_ENDPOINT})
        == 0.012  # noqa: PLR2004
    )
    # latency + 3 pool + dns + count == 6.
    assert result.metrics_emitted == 6  # noqa: PLR2004


# ============================================================================
# Test 11a: malformed pool range -- dhcpd_stop missing -> no pool, still counts + dns.
# ============================================================================
@pytest.mark.asyncio
async def test_malformed_pool_missing_stop() -> None:
    """dhcpd_stop missing -> no pool gauges, but count + dns still emitted."""
    rec = _network(
        name="NoStop",
        purpose="corporate",
        dhcpd_enabled=True,
        dhcpd_start="192.168.2.6",
        dhcpd_dns_1="192.168.2.148",
    )
    writer = InMemoryMetricsWriter()
    result = await UnifiNetworkconfCollector().run(_ctx(writer, _FakeNetconfOk([rec])))

    assert result.ok is True
    assert _gauges(writer, M_POOL_SIZE) == []
    assert _gauges(writer, M_POOL_START) == []
    assert _gauges(writer, M_POOL_END) == []
    assert _gauge_value(writer, M_DHCP_NETWORK_COUNT, {}) == 1.0
    assert _gauge_value(writer, M_DNS_PRIMARY, {"network": "NoStop", "dns": "192.168.2.148"}) == 1.0


@pytest.mark.asyncio
async def test_inverted_pool_range_skips_pool_gauges() -> None:
    """start > stop -> no pool gauges, but count + dns still emitted."""
    rec = _network(
        name="Inv",
        purpose="corporate",
        dhcpd_enabled=True,
        dhcpd_start="192.168.2.254",
        dhcpd_stop="192.168.2.6",
        dhcpd_dns_1="192.168.2.148",
    )
    writer = InMemoryMetricsWriter()
    result = await UnifiNetworkconfCollector().run(_ctx(writer, _FakeNetconfOk([rec])))

    assert result.ok is True
    assert _gauges(writer, M_POOL_SIZE) == []
    assert _gauges(writer, M_POOL_START) == []
    assert _gauges(writer, M_POOL_END) == []
    assert _gauge_value(writer, M_DHCP_NETWORK_COUNT, {}) == 1.0
    assert _gauge_value(writer, M_DNS_PRIMARY, {"network": "Inv", "dns": "192.168.2.148"}) == 1.0


@pytest.mark.asyncio
async def test_mixed_family_pool_range_skips_pool_gauges() -> None:
    """IPv4 start + IPv6 stop (mixed family) -> no pool gauges, but count + dns still emitted."""
    rec = _network(
        name="Mix",
        purpose="corporate",
        dhcpd_enabled=True,
        dhcpd_start="192.168.2.6",
        dhcpd_stop="fe80::1",
        dhcpd_dns_1="192.168.2.148",
    )
    writer = InMemoryMetricsWriter()
    result = await UnifiNetworkconfCollector().run(_ctx(writer, _FakeNetconfOk([rec])))

    assert result.ok is True
    assert _gauges(writer, M_POOL_SIZE) == []
    assert _gauges(writer, M_POOL_START) == []
    assert _gauges(writer, M_POOL_END) == []
    assert _gauge_value(writer, M_DHCP_NETWORK_COUNT, {}) == 1.0
    assert _gauge_value(writer, M_DNS_PRIMARY, {"network": "Mix", "dns": "192.168.2.148"}) == 1.0


# ============================================================================
# Test 11b: malformed pool range -- dhcpd_stop invalid IP -> ValueError path.
# ============================================================================
@pytest.mark.asyncio
async def test_malformed_pool_invalid_stop() -> None:
    """dhcpd_stop is an unparseable string -> no pool gauges, count + dns emitted."""
    rec = _network(
        name="BadStop",
        purpose="corporate",
        dhcpd_enabled=True,
        dhcpd_start="192.168.2.6",
        dhcpd_stop="not-an-ip",
        dhcpd_dns_1="192.168.2.148",
    )
    writer = InMemoryMetricsWriter()
    result = await UnifiNetworkconfCollector().run(_ctx(writer, _FakeNetconfOk([rec])))

    assert result.ok is True
    assert _gauges(writer, M_POOL_SIZE) == []
    assert _gauge_value(writer, M_DHCP_NETWORK_COUNT, {}) == 1.0
    assert (
        _gauge_value(writer, M_DNS_PRIMARY, {"network": "BadStop", "dns": "192.168.2.148"}) == 1.0
    )


# ============================================================================
# Test 12: missing / empty / non-str dhcpd_dns_1 -> no dns gauge, pool + count ok.
# ============================================================================
@pytest.mark.asyncio
async def test_missing_dns_no_dns_gauge() -> None:
    """A dhcp network with no usable dhcpd_dns_1 -> pool + count emitted, no dns."""
    rec = _network(
        name="NoDns",
        purpose="corporate",
        dhcpd_enabled=True,
        dhcpd_start="192.168.2.6",
        dhcpd_stop="192.168.2.254",
    )  # dhcpd_dns_1 omitted
    writer = InMemoryMetricsWriter()
    result = await UnifiNetworkconfCollector().run(_ctx(writer, _FakeNetconfOk([rec])))

    assert result.ok is True
    assert _gauge_value(writer, M_POOL_SIZE, {"network": "NoDns"}) == 249.0  # noqa: PLR2004
    assert _gauge_value(writer, M_DHCP_NETWORK_COUNT, {}) == 1.0
    assert _gauges(writer, M_DNS_PRIMARY) == []


# ============================================================================
# Test 13: multi-network payload (WAN + dhcp-enabled + dhcp-disabled).
# ============================================================================
@pytest.mark.asyncio
async def test_multi_network_only_enabled_emits() -> None:
    """1 WAN + 1 dhcp-enabled + 1 dhcp-disabled -> only the enabled one emits."""
    wan = _network(name="WAN", purpose="wan")
    enabled = _full_default_network()
    disabled = _network(name="Guest", purpose="guest", dhcpd_enabled=False)
    writer = InMemoryMetricsWriter()
    result = await UnifiNetworkconfCollector().run(
        _ctx(writer, _FakeNetconfOk([wan, enabled, disabled]))
    )

    assert result.ok is True
    assert _gauge_value(writer, M_DHCP_NETWORK_COUNT, {}) == 1.0
    assert _gauge_value(writer, M_POOL_SIZE, {"network": "Default"}) == 249.0  # noqa: PLR2004
    # No series for the WAN or disabled networks.
    assert {e.labels.get("network") for e in _gauges(writer, M_POOL_SIZE)} == {"Default"}


# ============================================================================
# Test 14: metrics_emitted equals the count of recorded gauge writes.
# ============================================================================
@pytest.mark.asyncio
async def test_metrics_emitted_matches_recorded() -> None:
    """metrics_emitted == the number of recorded gauge series."""
    writer = InMemoryMetricsWriter()
    result = await UnifiNetworkconfCollector().run(
        _ctx(writer, _FakeNetconfOk([_full_default_network()], took=0.077))
    )

    recorded = [e for e in writer.recorded if e.kind == "gauge"]  # pyright: ignore[reportPrivateUsage]
    assert result.metrics_emitted == len(recorded)
    # latency + 3 pool + dns + count == 6.
    assert result.metrics_emitted == 6  # noqa: PLR2004
    assert (
        _gauge_value(writer, "homelab_unifi_api_took_seconds", {"endpoint": _NETCONF_ENDPOINT})
        == 0.077  # noqa: PLR2004
    )


# Sanity guard: the pre-computed IP int constants must match ipaddress.
def test_ip_constants_match_ipaddress() -> None:
    """Guards the literal constants used in the pool-value assertions."""
    assert int(ipaddress.ip_address("192.168.2.6")) == _IP_START
    assert int(ipaddress.ip_address("192.168.2.254")) == _IP_STOP
