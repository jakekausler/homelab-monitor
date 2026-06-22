"""Tests for UnifiDeviceCollector -- stat/device parsing and metric emission."""

from __future__ import annotations

import structlog

from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.io import (
    InMemoryLogsWriter,
    InMemoryMetricsWriter,
    UnifiClient,
)
from homelab_monitor.kernel.plugins.types import CollectorConfig
from homelab_monitor.kernel.unifi.client import UnifiResponse
from homelab_monitor.kernel.unifi.errors import UnifiError
from homelab_monitor.plugins.collectors.integrations.unifi.device import (
    UnifiDeviceCollector,
    _emit_device_level,  # pyright: ignore[reportPrivateUsage]
    _kind_for,  # pyright: ignore[reportPrivateUsage]
)

# ---------------------------------------------------------------------------
# Fixture payload -- synthetic stat/device response containing 4 device kinds
# ---------------------------------------------------------------------------

_UDM_RECORD: dict[str, object] = {
    "type": "udm",
    "model": "UDMPRO",
    "name": "udm",
    "mac": "aa:bb:cc:00:00:01",
    "state": 1,
    "version": "7.4.1.16850",
    "displayable_version": "7.4.1",
    "upgradable": False,
    "uptime": 123456,
    "system-stats": {"cpu": "21.9", "mem": "44.4"},
    "sys_stats": {"loadavg_1": "0.5"},
    "temperatures": [
        {"name": "CPU", "type": "cpu", "value": 58.25},
        {"name": "Local", "type": "board", "value": 55.0},
    ],
    "port_table": [
        {
            "port_idx": 1,
            "up": True,
            "speed": 1000,
            "poe_enable": False,
            "poe_good": False,
            "poe_power": "0.00",
            "poe_current": "0.00",
            "poe_voltage": "0.00",
            "rx_bytes": 500,
            "tx_bytes": 600,
            "rx_errors": 0,
            "tx_errors": 0,
            "rx_dropped": 0,
            "tx_dropped": 0,
            "mac_table_count": 10,
            "link_down_count": 0,
            "satisfaction": 100,
        }
    ],
    "radio_table_stats": [],
    "outlet_table": [],
}

_SWITCH_RECORD: dict[str, object] = {
    "type": "usw",
    "model": "USL48PB",
    "name": "switch-poe",
    "mac": "aa:bb:cc:00:00:02",
    "state": 1,
    "version": "6.6.63.15693",
    "displayable_version": "6.6.63",
    "upgradable": True,
    "uptime": 654321,
    "satisfaction": -1,
    "system-stats": {"cpu": "10.0", "mem": "30.0"},
    "sys_stats": {"loadavg_1": "0.1"},
    "temperatures": [],
    "port_table": [
        {
            "port_idx": 1,
            "up": True,
            "speed": 1000,
            "poe_enable": True,
            "poe_good": True,
            "poe_power": "3.46",
            "poe_current": "64.00",
            "poe_voltage": "54.14",
            "rx_bytes": 100,
            "tx_bytes": 200,
            "rx_errors": 0,
            "tx_errors": 1,
            "rx_dropped": 0,
            "tx_dropped": 0,
            "mac_table_count": 3,
            "link_down_count": 2,
            "satisfaction": 98,
        },
        {
            "port_idx": 2,
            "up": False,
            "speed": 0,
            "poe_enable": False,
            "poe_good": False,
            "poe_power": "0.00",
            "poe_current": "0.00",
            "poe_voltage": "0.00",
            "rx_bytes": 0,
            "tx_bytes": 0,
            "rx_errors": 0,
            "tx_errors": 0,
            "rx_dropped": 0,
            "tx_dropped": 0,
            "mac_table_count": 0,
            "link_down_count": 0,
            "satisfaction": -1,
        },
    ],
    "radio_table_stats": [],
    "outlet_table": [],
}

_AP_RECORD: dict[str, object] = {
    "type": "uap",
    "model": "U7PIW",
    "name": "ap-1",
    "mac": "aa:bb:cc:00:00:03",
    "state": 1,
    "version": "7.0.0.1",
    "displayable_version": "7.0.0",
    "upgradable": False,
    "uptime": 99999,
    "satisfaction": 98,
    "system-stats": {"cpu": "5.0", "mem": "25.0"},
    "sys_stats": {"loadavg_1": "0.2"},
    "temperatures": [],
    "port_table": [],
    "radio_table_stats": [
        {
            "name": "wifi0",
            "radio": "ng",
            "channel": 6,
            "bw": 20,
            "cu_total": 12,
            "cu_self_rx": 3,
            "cu_self_tx": 2,
            "num_sta": 5,
            "tx_power": 20,
            "tx_retries_pct": 1.5,
            "satisfaction": 95,
        },
        {
            "name": "wifi1",
            "radio": "na",
            "channel": 36,
            "bw": 80,
            "cu_total": 8,
            "cu_self_rx": 2,
            "cu_self_tx": 1,
            "num_sta": 3,
            "tx_power": 23,
            "tx_retries_pct": 0.5,
            "satisfaction": -1,
        },
    ],
    "outlet_table": [],
}

# PDU: model=USPPDUP, type=usw, outlet_table has power fields that must NOT be emitted.
# sys_stats={} (empty) and system-stats is absent -- graceful-degrade case.
_PDU_RECORD: dict[str, object] = {
    "type": "usw",
    "model": "USPPDUP",
    "name": "pdu",
    "mac": "aa:bb:cc:00:00:04",
    "state": 1,
    "version": "6.0.0.1",
    "displayable_version": "6.0.0",
    "upgradable": False,
    "uptime": 77777,
    "sys_stats": {},
    # NOTE: system-stats is intentionally ABSENT to test graceful degrade.
    "temperatures": [],
    "port_table": [],
    "radio_table_stats": [],
    "outlet_table": [
        {
            "index": 1,
            "relay_state": True,
            "name": "Outlet 1",
            "outlet_power": "64.943",  # must NOT be emitted
            "outlet_current": "0.59",  # must NOT be emitted
            "outlet_voltage": "120.1",  # must NOT be emitted
        },
        {
            "index": 2,
            "relay_state": False,
            "name": "USB 1",
            "outlet_power": "0.000",
        },
    ],
}

_FIXTURE_PAYLOAD: dict[str, object] = {
    "meta": {"rc": "ok"},
    "data": [_UDM_RECORD, _SWITCH_RECORD, _AP_RECORD, _PDU_RECORD],
}

# ---------------------------------------------------------------------------
# Fake Unifi client
# ---------------------------------------------------------------------------


class _FakeUnifiBase:
    """Base class for fake Unifi clients with shared stub methods."""

    site_name: str = "default"
    v1_site_id: str = "fake-uuid"

    async def stat_device(self) -> UnifiResponse | UnifiError:
        return UnifiError(reason="bad_response", message="stub")

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


class _FakeUnifiOk(_FakeUnifiBase):
    """Returns the fixture payload as a successful UnifiResponse."""

    async def stat_device(self) -> UnifiResponse | UnifiError:
        return UnifiResponse(
            payload=_FIXTURE_PAYLOAD,
            took_seconds=0.042,
            endpoint="stat/device",
        )


class _FakeUnifiFail(_FakeUnifiBase):
    """Returns a UnifiError from stat_device()."""

    async def stat_device(self) -> UnifiResponse | UnifiError:
        return UnifiError(reason="unreachable", message="GET stat/device: connection failed")


class _FakeUnifiCustom(_FakeUnifiBase):
    """Returns a caller-supplied payload via stat_device()."""

    def __init__(self, payload: dict[str, object]) -> None:
        self.payload_response = payload

    async def stat_device(self) -> UnifiResponse | UnifiError:
        return UnifiResponse(
            payload=self.payload_response,
            took_seconds=0.042,
            endpoint="stat/device",
        )


# ---------------------------------------------------------------------------
# Context factory
# ---------------------------------------------------------------------------


def _ctx(
    writer: InMemoryMetricsWriter,
    unifi: UnifiClient | None,
) -> CollectorContext:
    """Minimal CollectorContext -- only vm + unifi are used by run()."""
    return CollectorContext(
        config=CollectorConfig(name="unifi_device", interval_seconds=60, timeout_seconds=15),
        db=None,  # pyright: ignore[reportArgumentType]
        vm=writer,
        vl=InMemoryLogsWriter(),
        http=None,  # pyright: ignore[reportArgumentType]
        ssh=None,  # pyright: ignore[reportArgumentType]
        secrets=None,  # pyright: ignore[reportArgumentType]
        log=structlog.get_logger().bind(collector="unifi_device"),  # pyright: ignore[reportArgumentType]
        unifi=unifi,
    )


# ---------------------------------------------------------------------------
# Helper: find recorded gauges by name (and optionally label subset)
# ---------------------------------------------------------------------------


def _gauges(writer: InMemoryMetricsWriter, name: str) -> list[tuple[float, dict[str, str]]]:
    """Return (value, labels) for all recorded entries with the given name."""
    return [(e.value, e.labels) for e in writer.recorded if e.name == name]


def _gauge_value(
    writer: InMemoryMetricsWriter,
    name: str,
    label_subset: dict[str, str],
) -> float | None:
    """Return the value of the first gauge matching name + all label_subset entries."""
    for e in writer.recorded:
        if e.name == name and all(e.labels.get(k) == v for k, v in label_subset.items()):
            return e.value
    return None


# ---------------------------------------------------------------------------
# Tests: ClassVars
# ---------------------------------------------------------------------------


def test_classvars() -> None:
    """ClassVars match the locked spec values."""
    assert UnifiDeviceCollector.name == "unifi_device"
    assert UnifiDeviceCollector.interval.total_seconds() == 60.0  # noqa: PLR2004
    assert UnifiDeviceCollector.timeout.total_seconds() == 15.0  # noqa: PLR2004
    assert UnifiDeviceCollector.concurrency_group == "unifi"


# ---------------------------------------------------------------------------
# Tests: device-level metrics
# ---------------------------------------------------------------------------


async def test_device_up_and_firmware() -> None:
    """device_up=1.0 for state==1; update_available correct; firmware_info=1.0."""
    writer = InMemoryMetricsWriter()
    result = await UnifiDeviceCollector().run(_ctx(writer, _FakeUnifiOk()))
    assert result.ok is True

    # All 4 devices have state==1 -> device_up==1.0
    up_gauges = _gauges(writer, "homelab_unifi_device_up")
    assert len(up_gauges) == 4  # noqa: PLR2004
    for val, _ in up_gauges:
        assert val == 1.0

    # Switch is upgradable=True -> update_available=1.0
    sw_update = _gauge_value(
        writer,
        "homelab_unifi_device_update_available",
        {"device": "switch-poe"},
    )
    assert sw_update == 1.0

    # UDM is upgradable=False -> update_available=0.0
    udm_update = _gauge_value(
        writer,
        "homelab_unifi_device_update_available",
        {"device": "udm"},
    )
    assert udm_update == 0.0

    # UDM firmware_info emitted with version labels, value=1.0
    fw_info = _gauge_value(
        writer,
        "homelab_unifi_device_firmware_info",
        {"device": "udm", "version": "7.4.1.16850", "displayable_version": "7.4.1"},
    )
    assert fw_info == 1.0


async def test_device_health_metrics() -> None:
    """cpu/mem parsed from STRING system-stats; load1 from sys_stats; uptime emitted."""
    writer = InMemoryMetricsWriter()
    await UnifiDeviceCollector().run(_ctx(writer, _FakeUnifiOk()))

    cpu = _gauge_value(writer, "homelab_unifi_device_cpu_percent", {"device": "udm"})
    assert cpu == 21.9  # noqa: PLR2004

    mem = _gauge_value(writer, "homelab_unifi_device_mem_percent", {"device": "udm"})
    assert mem == 44.4  # noqa: PLR2004

    load1 = _gauge_value(writer, "homelab_unifi_device_load1", {"device": "udm"})
    assert load1 == 0.5  # noqa: PLR2004

    uptime = _gauge_value(writer, "homelab_unifi_device_uptime_seconds", {"device": "udm"})
    assert uptime == 123456.0  # noqa: PLR2004


async def test_temperatures_udm_only() -> None:
    """Temperature gauges emitted per entry for UDM; NOT for switch/AP/PDU."""
    writer = InMemoryMetricsWriter()
    await UnifiDeviceCollector().run(_ctx(writer, _FakeUnifiOk()))

    temps = _gauges(writer, "homelab_unifi_device_temperature_celsius")
    # UDM has 2 temperature entries; other devices have empty arrays
    assert len(temps) == 2  # noqa: PLR2004

    cpu_temp = _gauge_value(
        writer,
        "homelab_unifi_device_temperature_celsius",
        {"device": "udm", "name": "CPU", "type": "cpu"},
    )
    assert cpu_temp == 58.25  # noqa: PLR2004

    board_temp = _gauge_value(
        writer,
        "homelab_unifi_device_temperature_celsius",
        {"device": "udm", "name": "Local", "type": "board"},
    )
    assert board_temp == 55.0  # noqa: PLR2004


# ---------------------------------------------------------------------------
# Tests: port metrics
# ---------------------------------------------------------------------------


async def test_port_metrics_poe_strings() -> None:
    """Port metrics emitted correctly; PoE strings parsed to float."""
    writer = InMemoryMetricsWriter()
    await UnifiDeviceCollector().run(_ctx(writer, _FakeUnifiOk()))

    # Port 1 (PoE active) on switch-poe
    port1 = {"device": "switch-poe", "port": "1"}
    assert _gauge_value(writer, "homelab_unifi_port_up", port1) == 1.0
    assert _gauge_value(writer, "homelab_unifi_port_speed_bps", port1) == 1_000_000_000.0  # noqa: PLR2004
    assert _gauge_value(writer, "homelab_unifi_port_poe_power_watts", port1) == 3.46  # noqa: PLR2004
    assert _gauge_value(writer, "homelab_unifi_port_poe_current_ma", port1) == 64.0  # noqa: PLR2004
    assert _gauge_value(writer, "homelab_unifi_port_poe_voltage", port1) == 54.14  # noqa: PLR2004
    assert _gauge_value(writer, "homelab_unifi_port_poe_good", port1) == 1.0
    assert _gauge_value(writer, "homelab_unifi_port_tx_errors", port1) == 1.0
    assert _gauge_value(writer, "homelab_unifi_port_mac_table_count", port1) == 3.0  # noqa: PLR2004
    assert _gauge_value(writer, "homelab_unifi_port_link_down_count", port1) == 2.0  # noqa: PLR2004
    assert _gauge_value(writer, "homelab_unifi_port_satisfaction", port1) == 98.0  # noqa: PLR2004

    # Port 2 (non-PoE) on switch-poe -- poe_power "0.00" parses to 0.0
    port2 = {"device": "switch-poe", "port": "2"}
    assert _gauge_value(writer, "homelab_unifi_port_up", port2) == 0.0
    assert _gauge_value(writer, "homelab_unifi_port_poe_power_watts", port2) == 0.0


async def test_ap_emits_no_port_metrics() -> None:
    """AP with port_table=[] emits no port metrics."""
    writer = InMemoryMetricsWriter()
    await UnifiDeviceCollector().run(_ctx(writer, _FakeUnifiOk()))

    ap_ports = [
        e
        for e in writer.recorded
        if e.name.startswith("homelab_unifi_port_") and e.labels.get("device") == "ap-1"
    ]
    assert ap_ports == []


# ---------------------------------------------------------------------------
# Tests: radio metrics
# ---------------------------------------------------------------------------


async def test_radio_metrics() -> None:
    """Radio metrics emitted per radio with {device, radio} labels."""
    writer = InMemoryMetricsWriter()
    await UnifiDeviceCollector().run(_ctx(writer, _FakeUnifiOk()))

    wifi0 = {"device": "ap-1", "radio": "wifi0"}
    assert _gauge_value(writer, "homelab_unifi_radio_cu_total", wifi0) == 12.0  # noqa: PLR2004
    assert _gauge_value(writer, "homelab_unifi_radio_cu_self_rx", wifi0) == 3.0  # noqa: PLR2004
    assert _gauge_value(writer, "homelab_unifi_radio_cu_self_tx", wifi0) == 2.0  # noqa: PLR2004
    assert _gauge_value(writer, "homelab_unifi_radio_num_sta", wifi0) == 5.0  # noqa: PLR2004
    assert _gauge_value(writer, "homelab_unifi_radio_tx_power", wifi0) == 20.0  # noqa: PLR2004
    assert _gauge_value(writer, "homelab_unifi_radio_tx_retries_pct", wifi0) == 1.5  # noqa: PLR2004
    assert _gauge_value(writer, "homelab_unifi_radio_satisfaction", wifi0) == 95.0  # noqa: PLR2004
    assert _gauge_value(writer, "homelab_unifi_radio_channel", wifi0) == 6.0  # noqa: PLR2004
    assert _gauge_value(writer, "homelab_unifi_radio_bandwidth_mhz", wifi0) == 20.0  # noqa: PLR2004

    # wifi1 satisfaction is -1 (not computed) -- still emitted as -1.0
    wifi1 = {"device": "ap-1", "radio": "wifi1"}
    assert _gauge_value(writer, "homelab_unifi_radio_satisfaction", wifi1) == -1.0


# ---------------------------------------------------------------------------
# Tests: outlet metrics
# ---------------------------------------------------------------------------


async def test_outlet_relay_only_no_power() -> None:
    """outlet_relay_state emitted; NO outlet_power/current/voltage metrics."""
    writer = InMemoryMetricsWriter()
    await UnifiDeviceCollector().run(_ctx(writer, _FakeUnifiOk()))

    outlet1 = {"device": "pdu", "outlet": "1", "name": "Outlet 1"}
    assert _gauge_value(writer, "homelab_unifi_outlet_relay_state", outlet1) == 1.0

    outlet2 = {"device": "pdu", "outlet": "2", "name": "USB 1"}
    assert _gauge_value(writer, "homelab_unifi_outlet_relay_state", outlet2) == 0.0

    # Assert no power/current/voltage metrics were emitted for outlets
    power_names = [
        e.name
        for e in writer.recorded
        if "outlet_power" in e.name or "outlet_current" in e.name or "outlet_voltage" in e.name
    ]
    assert power_names == []


# ---------------------------------------------------------------------------
# Tests: graceful degrade
# ---------------------------------------------------------------------------


async def test_graceful_degrade_empty_sys_stats() -> None:
    """PDU with sys_stats={} and absent system-stats: no cpu/mem/load, no crash, ok=True."""
    writer = InMemoryMetricsWriter()
    result = await UnifiDeviceCollector().run(_ctx(writer, _FakeUnifiOk()))
    assert result.ok is True

    # PDU should have no cpu/mem/load metrics
    pdu_cpu = _gauge_value(writer, "homelab_unifi_device_cpu_percent", {"device": "pdu"})
    assert pdu_cpu is None

    pdu_mem = _gauge_value(writer, "homelab_unifi_device_mem_percent", {"device": "pdu"})
    assert pdu_mem is None

    pdu_load = _gauge_value(writer, "homelab_unifi_device_load1", {"device": "pdu"})
    assert pdu_load is None


# ---------------------------------------------------------------------------
# Tests: API latency
# ---------------------------------------------------------------------------


async def test_api_latency_emitted() -> None:
    """homelab_unifi_api_took_seconds{endpoint=stat/device} == 0.042 on success."""
    writer = InMemoryMetricsWriter()
    await UnifiDeviceCollector().run(_ctx(writer, _FakeUnifiOk()))

    lat = _gauge_value(
        writer,
        "homelab_unifi_api_took_seconds",
        {"endpoint": "stat/device"},
    )
    assert lat == 0.042  # noqa: PLR2004


# ---------------------------------------------------------------------------
# Tests: error paths
# ---------------------------------------------------------------------------


async def test_unifi_error_returns_failed() -> None:
    """UnifiError from stat_device -> ok=False, error in errors, metrics_emitted=0."""
    writer = InMemoryMetricsWriter()
    result = await UnifiDeviceCollector().run(_ctx(writer, _FakeUnifiFail()))
    assert result.ok is False
    assert result.metrics_emitted == 0
    assert "GET stat/device: connection failed" in result.errors[0]
    # Latency gauge must NOT be emitted on error path (no successful response)
    assert _gauges(writer, "homelab_unifi_api_took_seconds") == []


async def test_unifi_client_none() -> None:
    """ctx.unifi=None -> ok=False, errors=['unifi client not configured'], no crash."""
    writer = InMemoryMetricsWriter()
    result = await UnifiDeviceCollector().run(_ctx(writer, None))
    assert result.ok is False
    assert result.errors == ["unifi client not configured"]
    assert result.metrics_emitted == 0


# ---------------------------------------------------------------------------
# Tests: metrics_emitted count consistency
# ---------------------------------------------------------------------------


async def test_metrics_emitted_count() -> None:
    """result.metrics_emitted equals the number of write_gauge calls recorded."""
    writer = InMemoryMetricsWriter()
    result = await UnifiDeviceCollector().run(_ctx(writer, _FakeUnifiOk()))
    assert result.ok is True
    # Every recorded entry is a gauge (no counters/summaries from this collector)
    gauge_count = len(writer.recorded)
    assert result.metrics_emitted == gauge_count


async def test_device_satisfaction_emitted_for_ap_skipped_for_sentinel() -> None:
    """device_satisfaction emitted for AP (sat=98); NOT emitted for switch (sat=-1 sentinel);
    NOT emitted for PDU or UDM (no satisfaction field in fixtures).
    Covers all guard branches: present+>=0 (emit), present+<0 (skip), absent (skip).
    """
    writer = InMemoryMetricsWriter()
    await UnifiDeviceCollector().run(_ctx(writer, _FakeUnifiOk()))

    # Branch A: AP has satisfaction=98 -> must be emitted
    ap_sat = _gauge_value(writer, "homelab_unifi_device_satisfaction", {"device": "ap-1"})
    assert ap_sat == 98.0  # noqa: PLR2004

    # Branch B: switch-poe has satisfaction=-1 (sentinel) -> must NOT be emitted
    sw_sat = _gauge_value(writer, "homelab_unifi_device_satisfaction", {"device": "switch-poe"})
    assert sw_sat is None

    # Branch C: UDM has no satisfaction field -> must NOT be emitted
    udm_sat = _gauge_value(writer, "homelab_unifi_device_satisfaction", {"device": "udm"})
    assert udm_sat is None

    # Confirm only one device_satisfaction gauge total (the AP)
    all_sat = _gauges(writer, "homelab_unifi_device_satisfaction")
    assert len(all_sat) == 1


async def test_device_satisfaction_skips_bool() -> None:
    """satisfaction=True (bool) must not be emitted even though bool is subclass of int.
    Covers Branch D: bool exclusion.
    """
    writer = InMemoryMetricsWriter()
    fake = _FakeUnifiCustom(
        payload={
            "meta": {"rc": "ok"},
            "data": [
                {
                    "name": "bool-device",
                    "type": "uap",
                    "model": "U7PIW",
                    "state": 1,
                    "satisfaction": True,  # bool, must be skipped
                }
            ],
        }
    )
    result = await UnifiDeviceCollector().run(_ctx(writer, fake))
    assert result.ok is True
    sat = _gauges(writer, "homelab_unifi_device_satisfaction")
    assert sat == []


# ---------------------------------------------------------------------------
# Tests: edge cases for payload structure and data narrowing
# ---------------------------------------------------------------------------


async def test_payload_not_dict() -> None:
    """payload not a dict -> run returns ok=True, no device_up gauge."""
    writer = InMemoryMetricsWriter()
    fake = _FakeUnifiCustom(payload="not-a-dict")  # type: ignore[arg-type]
    result = await UnifiDeviceCollector().run(_ctx(writer, fake))
    assert result.ok is True
    assert _gauges(writer, "homelab_unifi_device_up") == []


async def test_data_not_list() -> None:
    """payload['data'] not a list -> ok=True, no device_up."""
    writer = InMemoryMetricsWriter()
    fake = _FakeUnifiCustom(payload={"meta": {"rc": "ok"}, "data": {"device": "invalid"}})
    result = await UnifiDeviceCollector().run(_ctx(writer, fake))
    assert result.ok is True
    assert _gauges(writer, "homelab_unifi_device_up") == []


async def test_data_contains_non_dict_and_dict_with_no_name() -> None:
    """data list with non-dict and dict missing str 'name' -> both skipped, ok=True."""
    writer = InMemoryMetricsWriter()
    fake = _FakeUnifiCustom(
        payload={
            "meta": {"rc": "ok"},
            "data": [
                "not-a-dict",
                {"type": "usw", "model": "USL48"},  # dict but no name field
            ],
        }
    )
    result = await UnifiDeviceCollector().run(_ctx(writer, fake))
    assert result.ok is True
    assert _gauges(writer, "homelab_unifi_device_up") == []


async def test_minimal_device_no_optional_fields() -> None:
    """Device with state=1 but no version/upgradable/sys_stats/system-stats."""
    writer = InMemoryMetricsWriter()
    fake = _FakeUnifiCustom(
        payload={
            "meta": {"rc": "ok"},
            "data": [
                {
                    "name": "minimal",
                    "type": "usw",
                    "model": "USL48",
                    "state": 1,
                }
            ],
        }
    )
    result = await UnifiDeviceCollector().run(_ctx(writer, fake))
    assert result.ok is True
    up_gauges = _gauges(writer, "homelab_unifi_device_up")
    assert len(up_gauges) == 1
    assert up_gauges[0][0] == 1.0


async def test_device_with_no_state() -> None:
    """Device record with no state field -> no device_up gauge emitted."""
    writer = InMemoryMetricsWriter()
    fake = _FakeUnifiCustom(
        payload={
            "meta": {"rc": "ok"},
            "data": [
                {
                    "name": "no-state",
                    "type": "usw",
                    "model": "USL48",
                }
            ],
        }
    )
    result = await UnifiDeviceCollector().run(_ctx(writer, fake))
    assert result.ok is True
    up_gauges = _gauges(writer, "homelab_unifi_device_up")
    assert up_gauges == []


async def test_temperatures_malformed() -> None:
    """Temperatures list with non-dict, missing name/type, unparseable value."""
    writer = InMemoryMetricsWriter()
    fake = _FakeUnifiCustom(
        payload={
            "meta": {"rc": "ok"},
            "data": [
                {
                    "name": "device-with-temps",
                    "type": "usw",
                    "model": "USL48",
                    "state": 1,
                    "temperatures": [
                        "not-a-dict",
                        {"type": "cpu"},  # dict but no name
                        {"name": "Temp1", "type": "board", "value": "not-a-number"},
                    ],
                }
            ],
        }
    )
    result = await UnifiDeviceCollector().run(_ctx(writer, fake))
    assert result.ok is True
    temps = _gauges(writer, "homelab_unifi_device_temperature_celsius")
    assert temps == []


async def test_temperatures_non_list() -> None:
    """Temperatures as non-list (dict) -> no temperature_celsius emitted."""
    writer = InMemoryMetricsWriter()
    fake = _FakeUnifiCustom(
        payload={
            "meta": {"rc": "ok"},
            "data": [
                {
                    "name": "device-bad-temps",
                    "type": "usw",
                    "model": "USL48",
                    "state": 1,
                    "temperatures": {"cpu": 65.0},  # dict instead of list
                }
            ],
        }
    )
    result = await UnifiDeviceCollector().run(_ctx(writer, fake))
    assert result.ok is True
    temps = _gauges(writer, "homelab_unifi_device_temperature_celsius")
    assert temps == []


async def test_port_radio_outlet_tables_non_list() -> None:
    """Port/radio/outlet tables as non-list -> no metrics emitted."""
    writer = InMemoryMetricsWriter()
    fake = _FakeUnifiCustom(
        payload={
            "meta": {"rc": "ok"},
            "data": [
                {
                    "name": "device-bad-tables",
                    "type": "usw",
                    "model": "USL48",
                    "state": 1,
                    "port_table": "not-a-list",
                    "radio_table_stats": {"channel": 6},  # dict instead of list
                    "outlet_table": 12345,  # int instead of list
                }
            ],
        }
    )
    result = await UnifiDeviceCollector().run(_ctx(writer, fake))
    assert result.ok is True
    assert _gauges(writer, "homelab_unifi_port_up") == []
    assert _gauges(writer, "homelab_unifi_radio_cu_total") == []
    assert _gauges(writer, "homelab_unifi_outlet_relay_state") == []


async def test_port_radio_outlet_entries_malformed() -> None:
    """Malformed port/radio/outlet entries with missing fields."""
    writer = InMemoryMetricsWriter()
    fake = _FakeUnifiCustom(
        payload={
            "meta": {"rc": "ok"},
            "data": [
                {
                    "name": "device-malformed",
                    "type": "usw",
                    "model": "USL48",
                    "state": 1,
                    "port_table": [
                        "not-a-dict",
                        {"up": True},  # no port_idx
                        {
                            "port_idx": 5,
                            "up": "yes",  # non-bool up (should be False via as_bool)
                            # no speed, no poe_good
                        },
                    ],
                    "radio_table_stats": [
                        "not-a-dict",
                        {"channel": 1},  # no str name
                    ],
                    "outlet_table": [
                        "not-a-dict",
                        {"relay_state": True},  # no index
                    ],
                }
            ],
        }
    )
    result = await UnifiDeviceCollector().run(_ctx(writer, fake))
    assert result.ok is True

    port5 = {"device": "device-malformed", "port": "5"}
    port_up = _gauge_value(writer, "homelab_unifi_port_up", port5)
    assert port_up == 0.0  # non-bool up -> as_bool returns False

    port_speed = _gauge_value(writer, "homelab_unifi_port_speed_bps", port5)
    assert port_speed is None

    poe_good = _gauge_value(writer, "homelab_unifi_port_poe_good", port5)
    assert poe_good is None

    radios = _gauges(writer, "homelab_unifi_radio_cu_total")
    assert radios == []


def test_kind_for_outlet_table_non_empty() -> None:
    """_kind_for with outlet_table containing entries -> 'pdu'."""
    rec: dict[str, object] = {"type": "usw", "model": "OTHER", "outlet_table": [{"index": 1}]}
    assert _kind_for(rec) == "pdu"


async def test_emit_device_level_direct_no_name() -> None:
    """_emit_device_level called directly with record missing name -> emitted=0."""
    writer = InMemoryMetricsWriter()
    ctx = _ctx(writer, None)
    emitted = [0]
    rec: dict[str, object] = {"state": 1}
    _emit_device_level(ctx, rec, emitted)
    assert emitted[0] == 0
    assert writer.recorded == []


async def test_device_info_emitted_with_mac() -> None:
    """device_info{mac,device,kind,model} value 1.0 emitted per device with a str mac."""
    writer = InMemoryMetricsWriter()
    await UnifiDeviceCollector().run(_ctx(writer, _FakeUnifiOk()))

    # AP record: mac present -> device_info emitted with device name + kind 'ap' + model.
    ap_info = _gauge_value(
        writer,
        "homelab_unifi_device_info",
        {"mac": "aa:bb:cc:00:00:03", "device": "ap-1", "kind": "ap", "model": "U7PIW"},
    )
    assert ap_info == 1.0

    # One device_info series per fixture device (all four now carry a mac).
    all_info = _gauges(writer, "homelab_unifi_device_info")
    assert len(all_info) == 4  # noqa: PLR2004


async def test_device_info_skipped_when_mac_absent_or_non_str() -> None:
    """No device_info series when mac is missing or non-str (both FALSE-branch cases)."""
    writer = InMemoryMetricsWriter()
    fake = _FakeUnifiCustom(
        payload={
            "meta": {"rc": "ok"},
            "data": [
                # mac absent entirely
                {"name": "no-mac", "type": "usw", "model": "USL48", "state": 1},
                # mac present but non-str (int) -> isinstance(str) False
                {"name": "int-mac", "type": "usw", "model": "USL48", "state": 1, "mac": 12345},
            ],
        }
    )
    result = await UnifiDeviceCollector().run(_ctx(writer, fake))
    assert result.ok is True
    assert _gauges(writer, "homelab_unifi_device_info") == []
