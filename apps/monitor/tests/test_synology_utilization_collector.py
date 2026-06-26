"""Unit tests for the synology_utilization collector (STAGE-008-008, fixture-based).

100% branch coverage of utilization.py. The fixture is built from the authoritative
ground-truth live payload (/tmp/synology_utilization_raw.json) per the STAGE-008-008 spec.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from homelab_monitor.kernel.plugins.context import CollectorContext

from homelab_monitor.kernel.plugins.io import MemoryRetainingMetricsWriter
from homelab_monitor.kernel.synology.client import SynologyResponse
from homelab_monitor.kernel.synology.errors import SynologyError
from homelab_monitor.plugins.collectors.integrations.synology.utilization import (
    M_CPU_LOAD1,
    M_CPU_LOAD5,
    M_CPU_LOAD15,
    M_CPU_OTHER_PERCENT,
    M_CPU_SYSTEM_PERCENT,
    M_CPU_USER_PERCENT,
    M_DISK_READ_BPS,
    M_DISK_READ_IOPS,
    M_DISK_UTILIZATION_PERCENT,
    M_DISK_WRITE_IOPS,
    M_MEM_AVAILABLE_BYTES,
    M_MEM_BUFFER_BYTES,
    M_MEM_CACHED_BYTES,
    M_MEM_TOTAL_BYTES,
    M_MEM_USAGE_PERCENT,
    M_NET_RX_BPS,
    M_NET_TX_BPS,
    M_NFS_READ_LATENCY_SECONDS,
    M_NFS_READ_OPS,
    M_NFS_TOTAL_LATENCY_SECONDS,
    M_NFS_TOTAL_OPS,
    M_NFS_WRITE_LATENCY_SECONDS,
    M_NFS_WRITE_OPS,
    M_SWAP_USAGE_PERCENT,
    M_VOL_READ_BPS,
    M_VOL_UTILIZATION_PERCENT,
    M_VOL_WRITE_BPS,
    SynologyUtilizationCollector,
)

_API_TOOK = "homelab_synology_api_took_seconds"
_DROP = "homelab_metric_family_dropped_series"

_EXPECTED_INTERVAL = 60.0
_EXPECTED_TIMEOUT = 30.0

# Number of family() calls in _emit. COUNT them in utilization.py — do not guess.
# 6 CPU + 6 MEM + 5 DISK + 2 NET + 3 VOL + 6 NFS = 28.
_FAMILY_COUNT = 28


class _FakeSynology:
    """Stand-in for ctx.synology with a programmable system_utilization()."""

    def __init__(self, result: object | None = None) -> None:
        self._result = result if result is not None else _util_resp(_utilization_payload())

    async def system_utilization(self) -> object:
        return self._result


@dataclass
class _Ctx:
    """Typed stand-in for CollectorContext used in utilization tests."""

    vm: MemoryRetainingMetricsWriter = field(default_factory=MemoryRetainingMetricsWriter)
    synology: object = None


def _ctx(writer: MemoryRetainingMetricsWriter, synology: object) -> _Ctx:
    return _Ctx(vm=writer, synology=synology)


def _util_resp(payload: object) -> SynologyResponse:
    return SynologyResponse(
        payload=payload,
        took_seconds=0.5,
        endpoint="SYNO.Core.System.Utilization/get",
    )


def _gauges_named(
    writer: MemoryRetainingMetricsWriter, name: str
) -> list[tuple[str, float, dict[str, str]]]:
    return [g for g in writer.gauges if g[0] == name]


def _utilization_payload() -> dict[str, object]:
    """Happy-path utilization fixture matching the live ground-truth shape."""
    return {
        "cpu": {
            "1min_load": 88,
            "5min_load": 48,
            "15min_load": 34,
            "device": "System",
            "user_load": 1,
            "system_load": 0,
            "other_load": 2,
        },
        "memory": {
            "total_real": 16150712,
            "avail_real": 256256,
            "cached": 12893092,
            "buffer": 18828,
            "real_usage": 18,
            "swap_usage": 26,
        },
        "disk": {
            "total": {
                "device": "total",
                "read_byte": 254752,
                "write_byte": 3379220,
                "read_access": 8,
                "write_access": 36,
                "utilization": 1,
            },
            "disk": [
                {
                    "device": "sda",
                    "read_byte": 35348,
                    "write_byte": 423648,
                    "read_access": 1,
                    "write_access": 5,
                    "utilization": 0,
                },
                {
                    "device": "sdb",
                    "read_byte": 35348,
                    "write_byte": 426987,
                    "read_access": 1,
                    "write_access": 5,
                    "utilization": 2,
                },
            ],
        },
        "network": [
            {"device": "total", "rx": 2376871, "tx": 123947},
            {"device": "eth0", "rx": 60209, "tx": 40348},
            {"device": "eth99", "rx": 2312224, "tx": 83400},
        ],
        "space": {
            "total": {
                "device": "total",
                "read_byte": 1231,
                "write_byte": 2351601,
                "utilization": 1,
            },
            "volume": [
                {
                    "device": "dm-1",
                    "display_name": "volume1",
                    "read_byte": 1231,
                    "write_byte": 2351601,
                    "utilization": 1,
                }
            ],
        },
        "nfs": [
            {
                "device": "nfs",
                "read_OPS": 0,
                "write_OPS": 0,
                "total_OPS": 0,
                "read_max_latency": 0,
                "write_max_latency": 0,
                "total_max_latency": 0,
            }
        ],
    }


def test_utilization_classvars() -> None:
    """Verify collector ClassVars: name, interval, timeout, concurrency_group."""
    assert SynologyUtilizationCollector.name == "synology_utilization"
    assert SynologyUtilizationCollector.interval.total_seconds() == _EXPECTED_INTERVAL
    assert SynologyUtilizationCollector.timeout.total_seconds() == _EXPECTED_TIMEOUT
    assert SynologyUtilizationCollector.concurrency_group == "synology"


async def test_utilization_unconfigured_client() -> None:
    """synology=None -> ok=False with unconfigured error."""
    writer = MemoryRetainingMetricsWriter()
    ctx = cast("CollectorContext", _ctx(writer, None))
    result = await SynologyUtilizationCollector().run(ctx)
    assert result.ok is False
    assert result.errors == ["synology client not configured"]
    assert result.metrics_emitted == 0
    assert writer.gauges == []


async def test_utilization_client_error() -> None:
    """SynologyError response -> ok=False with error message."""
    writer = MemoryRetainingMetricsWriter()
    fake_synology = _FakeSynology(SynologyError(reason="unreachable", message="connection failed"))
    ctx = cast("CollectorContext", _ctx(writer, fake_synology))
    result = await SynologyUtilizationCollector().run(ctx)
    assert result.ok is False
    assert result.errors == ["connection failed"]
    assert result.metrics_emitted == 0
    assert writer.gauges == []


async def test_utilization_malformed_payload_is_list() -> None:
    """Non-dict payload (list) -> ok=True with api_took only."""
    writer = MemoryRetainingMetricsWriter()
    fake_synology = _FakeSynology(_util_resp(["x"]))
    ctx = cast("CollectorContext", _ctx(writer, fake_synology))
    result = await SynologyUtilizationCollector().run(ctx)
    assert result.ok is True
    assert result.errors == []
    assert result.metrics_emitted == 1  # api_took only
    assert len(writer.gauges) == 1
    assert writer.gauges[0][0] == _API_TOOK


async def test_utilization_malformed_payload_is_none() -> None:
    """Non-dict payload (None) -> ok=True with api_took only."""
    writer = MemoryRetainingMetricsWriter()
    fake_synology = _FakeSynology(_util_resp(None))
    ctx = cast("CollectorContext", _ctx(writer, fake_synology))
    result = await SynologyUtilizationCollector().run(ctx)
    assert result.ok is True
    assert result.metrics_emitted == 1  # api_took only


async def test_utilization_happy_path_cpu() -> None:
    """Happy path: CPU load-avg (scaled 0.01) + percentages, all emitted."""
    writer = MemoryRetainingMetricsWriter()
    fake_synology = _FakeSynology()
    ctx = cast("CollectorContext", _ctx(writer, fake_synology))
    result = await SynologyUtilizationCollector().run(ctx)
    assert result.ok is True

    # CPU load-avg scaled by 0.01
    load1_gauges = _gauges_named(writer, M_CPU_LOAD1)
    assert len(load1_gauges) == 1
    assert load1_gauges[0] == (M_CPU_LOAD1, 0.88, {})

    load5_gauges = _gauges_named(writer, M_CPU_LOAD5)
    assert len(load5_gauges) == 1
    assert load5_gauges[0] == (M_CPU_LOAD5, 0.48, {})

    load15_gauges = _gauges_named(writer, M_CPU_LOAD15)
    assert len(load15_gauges) == 1
    assert load15_gauges[0] == (M_CPU_LOAD15, 0.34, {})

    # CPU percentages
    user_gauges = _gauges_named(writer, M_CPU_USER_PERCENT)
    assert len(user_gauges) == 1
    assert user_gauges[0] == (M_CPU_USER_PERCENT, 1.0, {})

    system_gauges = _gauges_named(writer, M_CPU_SYSTEM_PERCENT)
    assert len(system_gauges) == 1
    assert system_gauges[0] == (M_CPU_SYSTEM_PERCENT, 0.0, {})

    other_gauges = _gauges_named(writer, M_CPU_OTHER_PERCENT)
    assert len(other_gauges) == 1
    assert other_gauges[0] == (M_CPU_OTHER_PERCENT, 2.0, {})


async def test_utilization_happy_path_memory() -> None:
    """Happy path: memory/swap usage %, total/avail/cached/buffer bytes (KB->bytes)."""
    writer = MemoryRetainingMetricsWriter()
    fake_synology = _FakeSynology()
    ctx = cast("CollectorContext", _ctx(writer, fake_synology))
    result = await SynologyUtilizationCollector().run(ctx)
    assert result.ok is True

    # Memory usage percents
    mem_usage_gauges = _gauges_named(writer, M_MEM_USAGE_PERCENT)
    assert len(mem_usage_gauges) == 1
    assert mem_usage_gauges[0] == (M_MEM_USAGE_PERCENT, 18.0, {})

    swap_usage_gauges = _gauges_named(writer, M_SWAP_USAGE_PERCENT)
    assert len(swap_usage_gauges) == 1
    assert swap_usage_gauges[0] == (M_SWAP_USAGE_PERCENT, 26.0, {})

    # Memory sizes (KB -> bytes)
    total_bytes = 16150712 * 1024.0
    total_gauges = _gauges_named(writer, M_MEM_TOTAL_BYTES)
    assert len(total_gauges) == 1
    assert total_gauges[0] == (M_MEM_TOTAL_BYTES, total_bytes, {})

    avail_bytes = 256256 * 1024.0
    avail_gauges = _gauges_named(writer, M_MEM_AVAILABLE_BYTES)
    assert len(avail_gauges) == 1
    assert avail_gauges[0] == (M_MEM_AVAILABLE_BYTES, avail_bytes, {})

    cached_bytes = 12893092 * 1024.0
    cached_gauges = _gauges_named(writer, M_MEM_CACHED_BYTES)
    assert len(cached_gauges) == 1
    assert cached_gauges[0] == (M_MEM_CACHED_BYTES, cached_bytes, {})

    buffer_bytes = 18828 * 1024.0
    buffer_gauges = _gauges_named(writer, M_MEM_BUFFER_BYTES)
    assert len(buffer_gauges) == 1
    assert buffer_gauges[0] == (M_MEM_BUFFER_BYTES, buffer_bytes, {})


async def test_utilization_happy_path_disk() -> None:
    """Happy path: disk per-device and total read/write bps, iops, utilization."""
    writer = MemoryRetainingMetricsWriter()
    fake_synology = _FakeSynology()
    ctx = cast("CollectorContext", _ctx(writer, fake_synology))
    result = await SynologyUtilizationCollector().run(ctx)
    assert result.ok is True

    # Disk read BPS: total + sda + sdb
    expected_disk_read_series = 3  # total aggregate + sda + sdb
    read_bps_gauges = _gauges_named(writer, M_DISK_READ_BPS)
    assert len(read_bps_gauges) == expected_disk_read_series
    assert (M_DISK_READ_BPS, 254752.0, {"device": "total"}) in read_bps_gauges
    assert (M_DISK_READ_BPS, 35348.0, {"device": "sda"}) in read_bps_gauges

    # Disk utilization for sda
    util_gauges = _gauges_named(writer, M_DISK_UTILIZATION_PERCENT)
    sda_util = [g for g in util_gauges if g[2].get("device") == "sda"]
    assert len(sda_util) == 1
    assert sda_util[0] == (M_DISK_UTILIZATION_PERCENT, 0.0, {"device": "sda"})

    # Disk IOPS for sda
    read_iops_gauges = _gauges_named(writer, M_DISK_READ_IOPS)
    sda_read_iops = [g for g in read_iops_gauges if g[2].get("device") == "sda"]
    assert len(sda_read_iops) == 1
    assert sda_read_iops[0] == (M_DISK_READ_IOPS, 1.0, {"device": "sda"})

    write_iops_gauges = _gauges_named(writer, M_DISK_WRITE_IOPS)
    sda_write_iops = [g for g in write_iops_gauges if g[2].get("device") == "sda"]
    assert len(sda_write_iops) == 1
    assert sda_write_iops[0] == (M_DISK_WRITE_IOPS, 5.0, {"device": "sda"})


async def test_utilization_happy_path_network() -> None:
    """Happy path: network per-iface rx/tx (total, eth0, eth99)."""
    writer = MemoryRetainingMetricsWriter()
    fake_synology = _FakeSynology()
    ctx = cast("CollectorContext", _ctx(writer, fake_synology))
    result = await SynologyUtilizationCollector().run(ctx)
    assert result.ok is True

    rx_gauges = _gauges_named(writer, M_NET_RX_BPS)
    assert (M_NET_RX_BPS, 2376871.0, {"iface": "total"}) in rx_gauges
    assert (M_NET_RX_BPS, 60209.0, {"iface": "eth0"}) in rx_gauges
    assert (M_NET_RX_BPS, 2312224.0, {"iface": "eth99"}) in rx_gauges

    tx_gauges = _gauges_named(writer, M_NET_TX_BPS)
    eth0_tx = [g for g in tx_gauges if g[2].get("iface") == "eth0"]
    assert len(eth0_tx) == 1
    assert eth0_tx[0] == (M_NET_TX_BPS, 40348.0, {"iface": "eth0"})


async def test_utilization_happy_path_volume() -> None:
    """Happy path: volume per-volume read/write bps, utilization (display_name preferred)."""
    writer = MemoryRetainingMetricsWriter()
    fake_synology = _FakeSynology()
    ctx = cast("CollectorContext", _ctx(writer, fake_synology))
    result = await SynologyUtilizationCollector().run(ctx)
    assert result.ok is True

    read_bps_gauges = _gauges_named(writer, M_VOL_READ_BPS)
    # Should have volume1 (display_name) and total (space.total device)
    assert (M_VOL_READ_BPS, 1231.0, {"volume": "volume1"}) in read_bps_gauges
    assert (M_VOL_READ_BPS, 1231.0, {"volume": "total"}) in read_bps_gauges

    write_bps_gauges = _gauges_named(writer, M_VOL_WRITE_BPS)
    vol1_write = [g for g in write_bps_gauges if g[2].get("volume") == "volume1"]
    assert len(vol1_write) == 1
    assert vol1_write[0] == (M_VOL_WRITE_BPS, 2351601.0, {"volume": "volume1"})

    util_gauges = _gauges_named(writer, M_VOL_UTILIZATION_PERCENT)
    vol1_util = [g for g in util_gauges if g[2].get("volume") == "volume1"]
    assert len(vol1_util) == 1
    assert vol1_util[0] == (M_VOL_UTILIZATION_PERCENT, 1.0, {"volume": "volume1"})


async def test_utilization_happy_path_nfs() -> None:
    """Happy path: NFS read/write/total OPS + latencies (ms->s)."""
    writer = MemoryRetainingMetricsWriter()
    fake_synology = _FakeSynology()
    ctx = cast("CollectorContext", _ctx(writer, fake_synology))
    result = await SynologyUtilizationCollector().run(ctx)
    assert result.ok is True

    read_ops = _gauges_named(writer, M_NFS_READ_OPS)
    assert len(read_ops) == 1
    assert read_ops[0] == (M_NFS_READ_OPS, 0.0, {})

    write_ops = _gauges_named(writer, M_NFS_WRITE_OPS)
    assert len(write_ops) == 1
    assert write_ops[0] == (M_NFS_WRITE_OPS, 0.0, {})

    total_ops = _gauges_named(writer, M_NFS_TOTAL_OPS)
    assert len(total_ops) == 1
    assert total_ops[0] == (M_NFS_TOTAL_OPS, 0.0, {})

    # Latencies (ms -> s, so 0 ms = 0.0 s)
    read_lat = _gauges_named(writer, M_NFS_READ_LATENCY_SECONDS)
    assert len(read_lat) == 1
    assert read_lat[0] == (M_NFS_READ_LATENCY_SECONDS, 0.0, {})

    write_lat = _gauges_named(writer, M_NFS_WRITE_LATENCY_SECONDS)
    assert len(write_lat) == 1
    assert write_lat[0] == (M_NFS_WRITE_LATENCY_SECONDS, 0.0, {})

    total_lat = _gauges_named(writer, M_NFS_TOTAL_LATENCY_SECONDS)
    assert len(total_lat) == 1
    assert total_lat[0] == (M_NFS_TOTAL_LATENCY_SECONDS, 0.0, {})


async def test_utilization_happy_path_metrics_emitted_accounting() -> None:
    """Verify metrics_emitted accounting: api_took + family drops + survivors."""
    writer = MemoryRetainingMetricsWriter()
    fake_synology = _FakeSynology()
    ctx = cast("CollectorContext", _ctx(writer, fake_synology))
    result = await SynologyUtilizationCollector().run(ctx)
    assert result.ok is True

    assert len(_gauges_named(writer, _API_TOOK)) == 1
    assert len(_gauges_named(writer, _DROP)) == _FAMILY_COUNT
    survivors = sum(1 for g in writer.gauges if g[0] not in (_API_TOOK, _DROP))
    assert result.metrics_emitted == survivors + _FAMILY_COUNT + 1
    assert result.metrics_emitted == len(writer.gauges)


async def test_utilization_scaled_non_numeric_skips() -> None:
    """_scaled with non-numeric string -> skipped, no observation."""
    writer = MemoryRetainingMetricsWriter()
    payload: dict[str, object] = {
        "cpu": {
            "1min_load": "x",  # Non-numeric, skipped
            "5min_load": 48,
            "15min_load": 34,
            "user_load": 1,
            "system_load": 0,
            "other_load": 2,
        },
    }
    fake_synology = _FakeSynology(_util_resp(payload))
    ctx = cast("CollectorContext", _ctx(writer, fake_synology))
    result = await SynologyUtilizationCollector().run(ctx)
    assert result.ok is True

    load1_gauges = _gauges_named(writer, M_CPU_LOAD1)
    assert load1_gauges == []


async def test_utilization_scalar_missing_skips() -> None:
    """Missing scalar fields -> as_float returns None, no observation."""
    writer = MemoryRetainingMetricsWriter()
    payload: dict[str, object] = {
        "memory": {"total_real": 100},  # Missing real_usage
        "cpu": {
            "1min_load": 88,
            "5min_load": 48,
            "15min_load": 34,
            # Missing user_load
            "system_load": 0,
            "other_load": 2,
        },
    }
    fake_synology = _FakeSynology(_util_resp(payload))
    ctx = cast("CollectorContext", _ctx(writer, fake_synology))
    result = await SynologyUtilizationCollector().run(ctx)
    assert result.ok is True

    mem_usage_gauges = _gauges_named(writer, M_MEM_USAGE_PERCENT)
    assert mem_usage_gauges == []

    cpu_user_gauges = _gauges_named(writer, M_CPU_USER_PERCENT)
    assert cpu_user_gauges == []


async def test_utilization_disk_non_str_device_skipped() -> None:
    """Disk object with non-str device -> skipped; valid device still emits."""
    writer = MemoryRetainingMetricsWriter()
    payload: dict[str, object] = {
        "disk": {
            "disk": [
                {
                    "device": 42,  # Non-str, skipped
                    "read_byte": 100,
                },
                {
                    "device": "sda",  # Valid, emitted
                    "read_byte": 50,
                },
            ],
        },
    }
    fake_synology = _FakeSynology(_util_resp(payload))
    ctx = cast("CollectorContext", _ctx(writer, fake_synology))
    result = await SynologyUtilizationCollector().run(ctx)
    assert result.ok is True

    read_bps_gauges = _gauges_named(writer, M_DISK_READ_BPS)
    # Only sda should be present
    assert len(read_bps_gauges) == 1
    assert read_bps_gauges[0] == (M_DISK_READ_BPS, 50.0, {"device": "sda"})


async def test_utilization_network_non_str_device_skipped() -> None:
    """Network item with non-str device -> skipped; valid device still emits."""
    writer = MemoryRetainingMetricsWriter()
    payload: dict[str, object] = {
        "network": [
            {"device": 7, "rx": 100},  # Non-str, skipped
            {"device": "eth0", "rx": 50},  # Valid
        ],
    }
    fake_synology = _FakeSynology(_util_resp(payload))
    ctx = cast("CollectorContext", _ctx(writer, fake_synology))
    result = await SynologyUtilizationCollector().run(ctx)
    assert result.ok is True

    rx_gauges = _gauges_named(writer, M_NET_RX_BPS)
    assert len(rx_gauges) == 1
    assert rx_gauges[0] == (M_NET_RX_BPS, 50.0, {"iface": "eth0"})


async def test_utilization_space_display_name_fallback_to_device() -> None:
    """Volume with no display_name -> falls back to device label."""
    writer = MemoryRetainingMetricsWriter()
    payload: dict[str, object] = {
        "space": {
            "volume": [
                {
                    "device": "dm-9",  # Fallback
                    "read_byte": 100,
                },
            ],
        },
    }
    fake_synology = _FakeSynology(_util_resp(payload))
    ctx = cast("CollectorContext", _ctx(writer, fake_synology))
    result = await SynologyUtilizationCollector().run(ctx)
    assert result.ok is True

    read_bps_gauges = _gauges_named(writer, M_VOL_READ_BPS)
    assert len(read_bps_gauges) == 1
    assert read_bps_gauges[0] == (M_VOL_READ_BPS, 100.0, {"volume": "dm-9"})


async def test_utilization_space_both_non_str_skipped() -> None:
    """Volume with both display_name and device non-str -> skipped."""
    writer = MemoryRetainingMetricsWriter()
    payload: dict[str, object] = {
        "space": {
            "volume": [
                {
                    "display_name": 42,  # Non-str
                    "device": 99,  # Non-str
                    "read_byte": 100,
                },
            ],
        },
    }
    fake_synology = _FakeSynology(_util_resp(payload))
    ctx = cast("CollectorContext", _ctx(writer, fake_synology))
    result = await SynologyUtilizationCollector().run(ctx)
    assert result.ok is True

    read_bps_gauges = _gauges_named(writer, M_VOL_READ_BPS)
    assert read_bps_gauges == []


async def test_utilization_build_guards_non_dict_sections() -> None:
    """Non-dict sections -> as_dict guards prevent parsing, all families empty."""
    writer = MemoryRetainingMetricsWriter()
    payload: dict[str, object] = {
        "cpu": "x",  # Non-dict
        "memory": [],  # Non-dict
        "disk": 5,  # Non-dict
        "space": "y",  # Non-dict
        "network": "z",  # Non-list
        "nfs": "w",  # Non-list
    }
    fake_synology = _FakeSynology(_util_resp(payload))
    ctx = cast("CollectorContext", _ctx(writer, fake_synology))
    result = await SynologyUtilizationCollector().run(ctx)
    assert result.ok is True

    # Spot-check all sections empty
    assert _gauges_named(writer, M_CPU_LOAD1) == []
    assert _gauges_named(writer, M_MEM_USAGE_PERCENT) == []
    assert _gauges_named(writer, M_DISK_READ_BPS) == []
    assert _gauges_named(writer, M_NET_RX_BPS) == []
    assert _gauges_named(writer, M_VOL_READ_BPS) == []
    assert _gauges_named(writer, M_NFS_READ_OPS) == []


async def test_utilization_empty_payload() -> None:
    """Empty payload {} -> ok=True, only api_took + empty family drops."""
    writer = MemoryRetainingMetricsWriter()
    fake_synology = _FakeSynology(_util_resp({}))
    ctx = cast("CollectorContext", _ctx(writer, fake_synology))
    result = await SynologyUtilizationCollector().run(ctx)
    assert result.ok is True
    assert result.metrics_emitted == 1 + _FAMILY_COUNT  # api_took + 28 drops
    assert _gauges_named(writer, M_CPU_LOAD1) == []


async def test_utilization_nfs_non_dict_entry_skipped() -> None:
    """NFS list with non-dict entry -> as_list_of_dicts filters it, valid dict emits."""
    writer = MemoryRetainingMetricsWriter()
    payload: dict[str, object] = {
        "nfs": [
            "not a dict",  # Filtered by as_list_of_dicts
            {
                "read_OPS": 5,
                "write_OPS": 3,
                "total_OPS": 8,
                "read_max_latency": 100,
                "write_max_latency": 50,
                "total_max_latency": 150,
            },
        ],
    }
    fake_synology = _FakeSynology(_util_resp(payload))
    ctx = cast("CollectorContext", _ctx(writer, fake_synology))
    result = await SynologyUtilizationCollector().run(ctx)
    assert result.ok is True

    read_ops = _gauges_named(writer, M_NFS_READ_OPS)
    assert len(read_ops) == 1
    assert read_ops[0] == (M_NFS_READ_OPS, 5.0, {})


async def test_utilization_nfs_empty_list() -> None:
    """NFS empty list -> no OPS emitted."""
    writer = MemoryRetainingMetricsWriter()
    payload: dict[str, object] = {
        "nfs": [],
    }
    fake_synology = _FakeSynology(_util_resp(payload))
    ctx = cast("CollectorContext", _ctx(writer, fake_synology))
    result = await SynologyUtilizationCollector().run(ctx)
    assert result.ok is True

    read_ops = _gauges_named(writer, M_NFS_READ_OPS)
    assert read_ops == []


async def test_utilization_disk_total_non_dict() -> None:
    """disk.total non-dict -> skipped; disk.disk[] items still emit."""
    writer = MemoryRetainingMetricsWriter()
    payload: dict[str, object] = {
        "disk": {
            "total": "x",  # Non-dict, skipped
            "disk": [
                {
                    "device": "sda",
                    "read_byte": 100,
                },
            ],
        },
    }
    fake_synology = _FakeSynology(_util_resp(payload))
    ctx = cast("CollectorContext", _ctx(writer, fake_synology))
    result = await SynologyUtilizationCollector().run(ctx)
    assert result.ok is True

    read_bps_gauges = _gauges_named(writer, M_DISK_READ_BPS)
    assert len(read_bps_gauges) == 1
    assert read_bps_gauges[0] == (M_DISK_READ_BPS, 100.0, {"device": "sda"})


async def test_utilization_space_total_non_dict() -> None:
    """space.total non-dict -> skipped; space.volume[] items still emit."""
    writer = MemoryRetainingMetricsWriter()
    payload: dict[str, object] = {
        "space": {
            "total": "x",  # Non-dict, skipped
            "volume": [
                {
                    "display_name": "volume1",
                    "read_byte": 100,
                },
            ],
        },
    }
    fake_synology = _FakeSynology(_util_resp(payload))
    ctx = cast("CollectorContext", _ctx(writer, fake_synology))
    result = await SynologyUtilizationCollector().run(ctx)
    assert result.ok is True

    read_bps_gauges = _gauges_named(writer, M_VOL_READ_BPS)
    assert len(read_bps_gauges) == 1
    assert read_bps_gauges[0] == (M_VOL_READ_BPS, 100.0, {"volume": "volume1"})


async def test_utilization_cpu_fields_missing_and_non_numeric() -> None:
    """cpu dict with 5min_load/15min_load/system_load/other_load absent or non-numeric
    -> None-side of _scaled/as_float branches taken; those four families stay empty."""
    writer = MemoryRetainingMetricsWriter()
    payload: dict[str, object] = {
        "cpu": {
            "1min_load": 88,  # present -> load1 emits
            "user_load": 1,  # present -> user_percent emits
            # 5min_load absent  -> M_CPU_LOAD5 skipped (branch 176->179)
            "15min_load": "x",  # non-numeric -> M_CPU_LOAD15 skipped (branch 180->183)
            "system_load": "bad",  # non-numeric -> M_CPU_SYSTEM_PERCENT skipped (branch 188->191)
            "other_load": "bad",  # non-numeric -> M_CPU_OTHER_PERCENT skipped (branch 192->exit)
        },
    }
    fake_synology = _FakeSynology(_util_resp(payload))
    ctx = cast("CollectorContext", _ctx(writer, fake_synology))
    result = await SynologyUtilizationCollector().run(ctx)
    assert result.ok is True

    assert _gauges_named(writer, M_CPU_LOAD5) == []
    assert _gauges_named(writer, M_CPU_LOAD15) == []
    assert _gauges_named(writer, M_CPU_SYSTEM_PERCENT) == []
    assert _gauges_named(writer, M_CPU_OTHER_PERCENT) == []


async def test_utilization_memory_total_real_missing() -> None:
    """memory dict with total_real absent -> None-side of _scaled branch (207->210) taken;
    M_MEM_TOTAL_BYTES has no series; other memory fields present and emitted."""
    writer = MemoryRetainingMetricsWriter()
    payload: dict[str, object] = {
        "memory": {
            # total_real omitted -> M_MEM_TOTAL_BYTES skipped
            "avail_real": 256256,
            "cached": 12893092,
            "buffer": 18828,
            "real_usage": 18,
            "swap_usage": 26,
        },
    }
    fake_synology = _FakeSynology(_util_resp(payload))
    ctx = cast("CollectorContext", _ctx(writer, fake_synology))
    result = await SynologyUtilizationCollector().run(ctx)
    assert result.ok is True

    assert _gauges_named(writer, M_MEM_TOTAL_BYTES) == []
    # Confirm sibling fields were not suppressed
    assert len(_gauges_named(writer, M_MEM_AVAILABLE_BYTES)) == 1


async def test_utilization_disk_read_byte_missing() -> None:
    """disk.disk[] item with read_byte absent -> None-side of as_float branch (234->237) taken;
    M_DISK_READ_BPS has no series for that device."""
    writer = MemoryRetainingMetricsWriter()
    payload: dict[str, object] = {
        "disk": {
            "disk": [
                {
                    "device": "sda",
                    # read_byte omitted -> M_DISK_READ_BPS skipped for sda
                    "write_byte": 1000,
                    "read_access": 2,
                    "write_access": 5,
                    "utilization": 3,
                },
            ],
        },
    }
    fake_synology = _FakeSynology(_util_resp(payload))
    ctx = cast("CollectorContext", _ctx(writer, fake_synology))
    result = await SynologyUtilizationCollector().run(ctx)
    assert result.ok is True

    assert _gauges_named(writer, M_DISK_READ_BPS) == []


async def test_utilization_network_rx_missing() -> None:
    """network list item with rx absent -> None-side of as_float branch (276->279) taken;
    M_NET_RX_BPS has no series for that iface."""
    writer = MemoryRetainingMetricsWriter()
    payload: dict[str, object] = {
        "network": [
            {
                "device": "eth0",
                # rx omitted -> M_NET_RX_BPS skipped for eth0
                "tx": 500,
            },
        ],
    }
    fake_synology = _FakeSynology(_util_resp(payload))
    ctx = cast("CollectorContext", _ctx(writer, fake_synology))
    result = await SynologyUtilizationCollector().run(ctx)
    assert result.ok is True

    assert _gauges_named(writer, M_NET_RX_BPS) == []
    # tx was present; confirm it still emits
    assert len(_gauges_named(writer, M_NET_TX_BPS)) == 1


async def test_utilization_space_read_byte_missing() -> None:
    """space.volume[] item with read_byte absent -> None-side of as_float branch (301->304)
    taken; M_VOL_READ_BPS has no series for that volume."""
    writer = MemoryRetainingMetricsWriter()
    payload: dict[str, object] = {
        "space": {
            "volume": [
                {
                    "display_name": "volume1",
                    # read_byte omitted -> M_VOL_READ_BPS skipped
                    "write_byte": 2000,
                    "utilization": 5,
                },
            ],
        },
    }
    fake_synology = _FakeSynology(_util_resp(payload))
    ctx = cast("CollectorContext", _ctx(writer, fake_synology))
    result = await SynologyUtilizationCollector().run(ctx)
    assert result.ok is True

    assert _gauges_named(writer, M_VOL_READ_BPS) == []
    # write_byte was present; confirm it still emits
    assert len(_gauges_named(writer, M_VOL_WRITE_BPS)) == 1


async def test_utilization_nfs_fields_missing() -> None:
    """nfs list item that is a valid dict but ALL six numeric fields absent ->
    None-side branches (326->329, 330->333, 334->337, 338->341, 342->345, 346->324)
    all taken; all six NFS families have no series."""
    writer = MemoryRetainingMetricsWriter()
    payload: dict[str, object] = {
        "nfs": [
            {"device": "nfs"},  # dict passes as_list_of_dicts; no numeric fields
        ],
    }
    fake_synology = _FakeSynology(_util_resp(payload))
    ctx = cast("CollectorContext", _ctx(writer, fake_synology))
    result = await SynologyUtilizationCollector().run(ctx)
    assert result.ok is True

    assert _gauges_named(writer, M_NFS_READ_OPS) == []
    assert _gauges_named(writer, M_NFS_WRITE_OPS) == []
    assert _gauges_named(writer, M_NFS_TOTAL_OPS) == []
    assert _gauges_named(writer, M_NFS_READ_LATENCY_SECONDS) == []
    assert _gauges_named(writer, M_NFS_WRITE_LATENCY_SECONDS) == []
    assert _gauges_named(writer, M_NFS_TOTAL_LATENCY_SECONDS) == []
