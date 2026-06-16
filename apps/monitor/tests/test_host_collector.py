"""Tests for :class:`HostCollector` (psutil-mocked + one real-psutil smoke)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, NamedTuple

import pytest

from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.io import (
    InMemoryLogsWriter,
    InMemoryMetricsWriter,
    MemoryRetainingMetricsWriter,
)
from homelab_monitor.kernel.plugins.types import CollectorConfig
from homelab_monitor.plugins.collectors.builtin import host as host_module
from homelab_monitor.plugins.collectors.builtin.host import (
    HostCollector,
    HostCollectorConfig,
)


# psutil namedtuple shapes (replicated locally — tests should not import private psutil types)
class _VMem(NamedTuple):
    total: int
    available: int
    used: int


class _Swap(NamedTuple):
    total: int
    used: int


class _Part(NamedTuple):
    device: str
    mountpoint: str
    fstype: str


class _Usage(NamedTuple):
    total: int
    used: int
    percent: float


class _DiskIO(NamedTuple):
    read_bytes: int
    write_bytes: int


class _NetIO(NamedTuple):
    bytes_recv: int
    bytes_sent: int


class _ShwTemp(NamedTuple):
    label: str
    current: float
    high: float | None
    critical: float | None


class _MemInfo(NamedTuple):
    rss: int


class _FakeProc:
    """Minimal psutil.Process double for process_iter."""

    def __init__(self, pid: int, name: str, cpu: float, rss: int, status: str = "running") -> None:
        self.info: dict[str, Any] = {
            "pid": pid,
            "name": name,
            "cpu_percent": cpu,
            "memory_info": _MemInfo(rss=rss),
        }
        self._status = status

    def status(self) -> str:
        return self._status


def _ctx(writer: MemoryRetainingMetricsWriter, cfg: CollectorConfig) -> CollectorContext:
    """Minimal CollectorContext — only fields HostCollector reads need to be real."""
    import structlog  # noqa: PLC0415

    return CollectorContext(
        config=cfg,
        db=None,  # pyright: ignore[reportArgumentType]
        vm=writer,
        vl=InMemoryLogsWriter(),
        http=None,  # pyright: ignore[reportArgumentType]
        ssh=None,  # pyright: ignore[reportArgumentType]
        secrets=None,  # pyright: ignore[reportArgumentType]
        log=structlog.get_logger().bind(collector="host"),  # pyright: ignore[reportArgumentType]
        ha=None,
    )


def _cpu_percent_stub(interval: float | None = None, percpu: bool = False) -> float | list[float]:
    return [5.0, 10.0] if percpu else 7.5


def _sensors_temperatures_stub() -> dict[str, list[_ShwTemp]]:
    """Real-shaped psutil.sensors_temperatures() for this host's 3 chips.

    nvme reports high/critical (89.85/93.85); k10temp and amdgpu report None.
    """
    return {
        "k10temp": [_ShwTemp(label="Tctl", current=54.0, high=None, critical=None)],
        "nvme": [_ShwTemp(label="Composite", current=46.0, high=89.85, critical=93.85)],
        "amdgpu": [_ShwTemp(label="edge", current=42.0, high=None, critical=None)],
    }


def _no_sensors_stub() -> dict[str, list[_ShwTemp]]:
    """Deterministic empty sensors result (host without temperature sensors)."""
    return {}


def _patch_psutil_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Apply a uniform happy-path psutil mock used by most tests."""
    monkeypatch.setattr(host_module.psutil, "cpu_percent", _cpu_percent_stub)
    monkeypatch.setattr(host_module.psutil, "getloadavg", lambda: (0.5, 1.0, 1.5))
    monkeypatch.setattr(
        host_module.psutil, "virtual_memory", lambda: _VMem(total=1024, available=512, used=512)
    )
    monkeypatch.setattr(host_module.psutil, "swap_memory", lambda: _Swap(total=2048, used=128))

    # psutil calls disk_partitions(all=False) as a kwarg, so this stub MUST
    # accept the keyword `all` exactly. Renaming to `all_` will fail tests.
    def _disk_partitions_stub(all: bool = False) -> list[_Part]:
        del all
        return [_Part(device="/dev/sda1", mountpoint="/", fstype="ext4")]

    monkeypatch.setattr(
        host_module.psutil,
        "disk_partitions",
        _disk_partitions_stub,
    )

    def _disk_usage_stub(mp: str) -> _Usage:
        del mp
        return _Usage(total=1000, used=400, percent=40.0)

    monkeypatch.setattr(
        host_module.psutil,
        "disk_usage",
        _disk_usage_stub,
    )

    def _disk_io_counters_stub(perdisk: bool = False) -> dict[str, _DiskIO]:
        del perdisk
        return {"sda": _DiskIO(read_bytes=100, write_bytes=200)}

    monkeypatch.setattr(
        host_module.psutil,
        "disk_io_counters",
        _disk_io_counters_stub,
    )

    def _net_io_counters_stub(pernic: bool = False) -> dict[str, _NetIO]:
        del pernic
        return {"eth0": _NetIO(bytes_recv=1000, bytes_sent=500)}

    monkeypatch.setattr(
        host_module.psutil,
        "net_io_counters",
        _net_io_counters_stub,
    )
    monkeypatch.setattr(host_module.psutil, "boot_time", lambda: 1_700_000_000.0)
    monkeypatch.setattr(
        host_module.time,
        "time",
        lambda: 1_700_000_100.0,  # uptime = 100s
    )

    def _process_iter_stub(attrs: list[str] | None = None) -> list[_FakeProc]:
        del attrs
        return [
            _FakeProc(1, "init", 1.0, 1024, "sleeping"),
            _FakeProc(2, "kworker", 2.0, 2048, "running"),
            _FakeProc(3, "zombie", 0.0, 0, "zombie"),
        ]

    monkeypatch.setattr(
        host_module.psutil,
        "process_iter",
        _process_iter_stub,
    )
    monkeypatch.setattr(
        host_module.psutil,
        "sensors_temperatures",
        _no_sensors_stub,  # default: no sensors — temp-specific tests override this
    )


@pytest.mark.asyncio
async def test_run_emits_cpu_metrics(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_psutil_happy_path(monkeypatch)
    writer = MemoryRetainingMetricsWriter()
    cfg = HostCollectorConfig(name="host")
    result = await HostCollector().run(_ctx(writer, cfg))
    assert result.ok
    cpu_entries = [e for e in writer.snapshot() if e.name == "homelab_host_cpu_percent"]
    cpu_labels = {tuple(sorted(e.labels.items())) for e in cpu_entries}
    assert (("cpu", "all"),) in cpu_labels
    assert (("cpu", "0"),) in cpu_labels
    assert (("cpu", "1"),) in cpu_labels


@pytest.mark.asyncio
async def test_run_emits_load_average(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_psutil_happy_path(monkeypatch)
    writer = MemoryRetainingMetricsWriter()
    cfg = HostCollectorConfig(name="host")
    await HostCollector().run(_ctx(writer, cfg))
    la = [e for e in writer.snapshot() if e.name == "homelab_host_load_average"]
    periods = {e.labels["period"] for e in la}
    assert periods == {"1m", "5m", "15m"}


@pytest.mark.asyncio
async def test_run_emits_memory_metrics(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_psutil_happy_path(monkeypatch)
    writer = MemoryRetainingMetricsWriter()
    cfg = HostCollectorConfig(name="host")
    await HostCollector().run(_ctx(writer, cfg))
    mem = [e for e in writer.snapshot() if e.name == "homelab_host_memory_bytes"]
    types_ = {e.labels["type"] for e in mem}
    assert types_ == {"used", "available", "total"}


@pytest.mark.asyncio
async def test_run_emits_swap_metrics(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_psutil_happy_path(monkeypatch)
    writer = MemoryRetainingMetricsWriter()
    cfg = HostCollectorConfig(name="host")
    await HostCollector().run(_ctx(writer, cfg))
    sw = [e for e in writer.snapshot() if e.name == "homelab_host_swap_bytes"]
    types_ = {e.labels["type"] for e in sw}
    assert types_ == {"used", "total"}


@pytest.mark.asyncio
async def test_run_emits_disk_metrics_with_extra_mountpoints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_psutil_happy_path(monkeypatch)
    writer = MemoryRetainingMetricsWriter()
    cfg = HostCollectorConfig(
        name="host", extra_mountpoints=["/rackstation"], disk_mountpoint_relabel={}
    )
    await HostCollector().run(_ctx(writer, cfg))
    disk = [e for e in writer.snapshot() if e.name == "homelab_host_disk_bytes"]
    mountpoints = {e.labels["mountpoint"] for e in disk}
    assert "/" in mountpoints
    assert "/rackstation" in mountpoints


@pytest.mark.asyncio
async def test_run_excludes_loop_and_ram_devices(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_psutil_happy_path(monkeypatch)

    def _parts(all: bool = False) -> list[_Part]:
        del all
        return [
            _Part(device="/dev/sda1", mountpoint="/", fstype="ext4"),
            _Part(device="loop0", mountpoint="/snap/x", fstype="squashfs"),
            _Part(device="ramdisk0", mountpoint="/ram", fstype="tmpfs"),
        ]

    monkeypatch.setattr(host_module.psutil, "disk_partitions", _parts)
    writer = MemoryRetainingMetricsWriter()
    cfg = HostCollectorConfig(name="host", extra_mountpoints=[], disk_mountpoint_relabel={})
    await HostCollector().run(_ctx(writer, cfg))
    disk_mps = {
        e.labels["mountpoint"] for e in writer.snapshot() if e.name == "homelab_host_disk_bytes"
    }
    assert disk_mps == {"/"}


@pytest.mark.asyncio
async def test_run_emits_disk_io_counters_per_disk(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_psutil_happy_path(monkeypatch)
    writer = MemoryRetainingMetricsWriter()
    cfg = HostCollectorConfig(name="host")
    await HostCollector().run(_ctx(writer, cfg))
    io = [e for e in writer.snapshot() if e.name == "homelab_host_disk_io_bytes_total"]
    pairs = {(e.labels["disk"], e.labels["direction"]) for e in io}
    assert pairs == {("sda", "read"), ("sda", "write")}
    # Set-semantics: snapshot value equals the raw psutil counter, not a multiple.
    by_dir = {e.labels["direction"]: e.value for e in io}
    assert by_dir["read"] == 100.0  # noqa: PLR2004
    assert by_dir["write"] == 200.0  # noqa: PLR2004
    assert all(e.kind == "gauge" for e in io)


@pytest.mark.asyncio
async def test_run_emits_net_io_counters_per_iface(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_psutil_happy_path(monkeypatch)
    writer = MemoryRetainingMetricsWriter()
    cfg = HostCollectorConfig(name="host")
    await HostCollector().run(_ctx(writer, cfg))
    net = [e for e in writer.snapshot() if e.name == "homelab_host_net_bytes_total"]
    pairs = {(e.labels["iface"], e.labels["direction"]) for e in net}
    assert pairs == {("eth0", "rx"), ("eth0", "tx")}
    by_dir = {e.labels["direction"]: e.value for e in net}
    assert by_dir["rx"] == 1000.0  # noqa: PLR2004
    assert by_dir["tx"] == 500.0  # noqa: PLR2004
    assert all(e.kind == "gauge" for e in net)


@pytest.mark.asyncio
async def test_run_emits_uptime_seconds(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_psutil_happy_path(monkeypatch)
    writer = MemoryRetainingMetricsWriter()
    cfg = HostCollectorConfig(name="host")
    await HostCollector().run(_ctx(writer, cfg))
    up = [e for e in writer.snapshot() if e.name == "homelab_host_uptime_seconds"]
    assert len(up) == 1
    assert up[0].value == 100.0  # noqa: PLR2004


@pytest.mark.asyncio
async def test_run_emits_processes_total_by_state(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_psutil_happy_path(monkeypatch)
    writer = MemoryRetainingMetricsWriter()
    cfg = HostCollectorConfig(name="host")
    await HostCollector().run(_ctx(writer, cfg))
    states = {
        e.labels["state"]: e.value
        for e in writer.snapshot()
        if e.name == "homelab_host_processes_total"
    }
    assert states == {"running": 1.0, "sleeping": 1.0, "zombie": 1.0}


@pytest.mark.asyncio
async def test_run_uses_replace_family_for_top_n_processes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_psutil_happy_path(monkeypatch)

    def _many_procs(attrs: list[str] | None = None) -> list[_FakeProc]:
        del attrs
        return [_FakeProc(pid=i, name=f"p{i}", cpu=float(i), rss=i * 100) for i in range(15)]

    monkeypatch.setattr(host_module.psutil, "process_iter", _many_procs)
    writer = MemoryRetainingMetricsWriter()
    cfg = HostCollectorConfig(name="host", top_n_processes=10)
    await HostCollector().run(_ctx(writer, cfg))
    cpu_top = [e for e in writer.snapshot() if e.name == "homelab_host_top_processes_cpu_percent"]
    mem_top = [e for e in writer.snapshot() if e.name == "homelab_host_top_processes_memory_bytes"]
    assert len(cpu_top) == 10  # noqa: PLR2004
    assert len(mem_top) == 10  # noqa: PLR2004


@pytest.mark.asyncio
async def test_run_handles_extra_mountpoint_unmounted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_psutil_happy_path(monkeypatch)

    def _disk_usage(mp: str) -> _Usage:
        if mp == "/rackstation":
            raise FileNotFoundError(mp)
        return _Usage(total=1000, used=400, percent=40.0)

    monkeypatch.setattr(host_module.psutil, "disk_usage", _disk_usage)
    writer = MemoryRetainingMetricsWriter()
    cfg = HostCollectorConfig(
        name="host", extra_mountpoints=["/rackstation"], disk_mountpoint_relabel={}
    )
    result = await HostCollector().run(_ctx(writer, cfg))
    # No error: silent skip on extra_mountpoints unmounted
    assert result.ok
    mps = {e.labels["mountpoint"] for e in writer.snapshot() if e.name == "homelab_host_disk_bytes"}
    assert "/rackstation" not in mps
    assert "/" in mps


@pytest.mark.asyncio
async def test_run_returns_ok_true_when_all_succeed(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_psutil_happy_path(monkeypatch)
    writer = MemoryRetainingMetricsWriter()
    cfg = HostCollectorConfig(name="host", disk_mountpoint_relabel={})
    result = await HostCollector().run(_ctx(writer, cfg))
    assert result.ok is True
    assert result.errors == []
    assert result.duration_seconds >= 0.0


@pytest.mark.asyncio
async def test_run_returns_ok_false_when_disk_usage_partition_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_psutil_happy_path(monkeypatch)

    def _disk_usage_raise(mp: str) -> _Usage:
        # Raise for "/" — the partition we DO want to read; ensures the section
        # records an error (FileNotFoundError on a partition-listed mountpoint
        # is treated as an error, unlike on extra_mountpoints which silent-skip).
        del mp
        raise OSError("simulated failure")

    monkeypatch.setattr(host_module.psutil, "disk_usage", _disk_usage_raise)
    writer = MemoryRetainingMetricsWriter()
    cfg = HostCollectorConfig(name="host", extra_mountpoints=[], disk_mountpoint_relabel={})
    result = await HostCollector().run(_ctx(writer, cfg))
    assert result.ok is False
    assert any("disk_usage" in e for e in result.errors)


@pytest.mark.asyncio
async def test_metrics_emitted_count_matches_actual_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_psutil_happy_path(monkeypatch)
    writer = MemoryRetainingMetricsWriter()
    cfg = HostCollectorConfig(name="host", extra_mountpoints=[], disk_mountpoint_relabel={})
    result = await HostCollector().run(_ctx(writer, cfg))
    # `recorded` includes append-only history. For one fresh tick with no
    # repeats, len(recorded) == metrics_emitted.
    assert result.metrics_emitted == len(writer.recorded)


@pytest.mark.asyncio
async def test_disk_partitions_with_total_zero_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regular partitions with total=0 disk_usage are silently skipped."""
    _patch_psutil_happy_path(monkeypatch)

    def _parts(all: bool = False) -> list[_Part]:
        del all
        return [
            _Part(device="/dev/sda1", mountpoint="/", fstype="ext4"),
            _Part(device="/dev/sdb1", mountpoint="/zero", fstype="ext4"),
        ]

    monkeypatch.setattr(host_module.psutil, "disk_partitions", _parts)

    def _disk_usage(mp: str) -> _Usage:
        if mp == "/zero":
            return _Usage(total=0, used=0, percent=0.0)
        return _Usage(total=1000, used=400, percent=40.0)

    monkeypatch.setattr(host_module.psutil, "disk_usage", _disk_usage)
    writer = MemoryRetainingMetricsWriter()
    cfg = HostCollectorConfig(name="host", extra_mountpoints=[], disk_mountpoint_relabel={})
    result = await HostCollector().run(_ctx(writer, cfg))
    assert result.ok
    mountpoints = {
        e.labels["mountpoint"] for e in writer.snapshot() if e.name == "homelab_host_disk_bytes"
    }
    assert "/" in mountpoints
    assert "/zero" not in mountpoints


@pytest.mark.asyncio
async def test_extra_mountpoints_with_total_zero_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Extra mountpoints with total=0 disk_usage are silently skipped."""
    _patch_psutil_happy_path(monkeypatch)

    def _disk_usage(mp: str) -> _Usage:
        if mp == "/zero_extra":
            return _Usage(total=0, used=0, percent=0.0)
        return _Usage(total=1000, used=400, percent=40.0)

    monkeypatch.setattr(host_module.psutil, "disk_usage", _disk_usage)
    writer = MemoryRetainingMetricsWriter()
    cfg = HostCollectorConfig(
        name="host", extra_mountpoints=["/zero_extra", "/good_extra"], disk_mountpoint_relabel={}
    )
    result = await HostCollector().run(_ctx(writer, cfg))
    assert result.ok
    mountpoints = {
        e.labels["mountpoint"] for e in writer.snapshot() if e.name == "homelab_host_disk_bytes"
    }
    assert "/zero_extra" not in mountpoints
    assert "/good_extra" in mountpoints


@pytest.mark.asyncio
async def test_extra_mountpoint_already_in_partitions_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Extra mountpoint that's also in disk_partitions is skipped via `mp in seen` guard."""
    _patch_psutil_happy_path(monkeypatch)

    # Override disk_partitions to include /shared
    def _partitions(all: bool = False) -> list[_Part]:
        del all
        return [_Part(device="/dev/sda1", mountpoint="/shared", fstype="ext4")]

    monkeypatch.setattr(host_module.psutil, "disk_partitions", _partitions)

    writer = MemoryRetainingMetricsWriter()
    # /shared is in BOTH the partitions list AND extra_mountpoints — duplicate
    cfg = HostCollectorConfig(
        name="host", extra_mountpoints=["/shared"], disk_mountpoint_relabel={}
    )
    result = await HostCollector().run(_ctx(writer, cfg))
    assert result.ok

    # /shared appears exactly ONCE in homelab_host_disk_bytes (not duplicated)
    shared_used_entries = [
        e
        for e in writer.snapshot()
        if e.name == "homelab_host_disk_bytes"
        and e.labels.get("mountpoint") == "/shared"
        and e.labels.get("type") == "used"
    ]
    assert len(shared_used_entries) == 1


@pytest.mark.asyncio
async def test_disk_relabel_default_allowlists_and_relabels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default relabel map: only /config and /host-compose are emitted, relabeled."""
    _patch_psutil_happy_path(monkeypatch)

    def _parts(all: bool = False) -> list[_Part]:
        del all
        return [
            _Part(device="/dev/sda1", mountpoint="/config", fstype="ext4"),
            _Part(device="/dev/sdb1", mountpoint="/host-compose", fstype="ext4"),
            _Part(device="/dev/sdc1", mountpoint="/etc/hosts", fstype="ext4"),
            _Part(device="/dev/sdd1", mountpoint="/run/secrets", fstype="tmpfs"),
        ]

    monkeypatch.setattr(host_module.psutil, "disk_partitions", _parts)
    writer = MemoryRetainingMetricsWriter()
    # Default disk_mountpoint_relabel={"/config": "/", "/host-compose": "/storage"}
    cfg = HostCollectorConfig(name="host", extra_mountpoints=[])
    await HostCollector().run(_ctx(writer, cfg))
    disk_mps = {
        e.labels["mountpoint"] for e in writer.snapshot() if e.name == "homelab_host_disk_bytes"
    }
    # /config → "/" and /host-compose → "/storage"; junk paths are filtered
    assert disk_mps == {"/", "/storage"}
    # Raw container paths must NOT appear
    assert "/config" not in disk_mps
    assert "/host-compose" not in disk_mps
    assert "/etc/hosts" not in disk_mps
    assert "/run/secrets" not in disk_mps


@pytest.mark.asyncio
async def test_disk_relabel_empty_map_emits_all_unrelabeled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty relabel map disables allowlist; all non-excluded mountpoints emitted raw."""
    _patch_psutil_happy_path(monkeypatch)

    def _parts(all: bool = False) -> list[_Part]:
        del all
        return [
            _Part(device="/dev/sda1", mountpoint="/", fstype="ext4"),
            _Part(device="/dev/sdb1", mountpoint="/data", fstype="ext4"),
        ]

    monkeypatch.setattr(host_module.psutil, "disk_partitions", _parts)
    writer = MemoryRetainingMetricsWriter()
    cfg = HostCollectorConfig(name="host", extra_mountpoints=[], disk_mountpoint_relabel={})
    await HostCollector().run(_ctx(writer, cfg))
    disk_mps = {
        e.labels["mountpoint"] for e in writer.snapshot() if e.name == "homelab_host_disk_bytes"
    }
    assert disk_mps == {"/", "/data"}


@pytest.mark.asyncio
async def test_disk_relabel_key_fails_disk_usage_handled_gracefully(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Allowlisted mountpoint that raises disk_usage is recorded as error, others proceed."""
    _patch_psutil_happy_path(monkeypatch)

    def _parts(all: bool = False) -> list[_Part]:
        del all
        return [
            _Part(device="/dev/sda1", mountpoint="/config", fstype="ext4"),
            _Part(device="/dev/sdb1", mountpoint="/host-compose", fstype="ext4"),
        ]

    def _disk_usage(mp: str) -> _Usage:
        if mp == "/config":
            raise OSError("simulated failure")
        return _Usage(total=1000, used=400, percent=40.0)

    monkeypatch.setattr(host_module.psutil, "disk_partitions", _parts)
    monkeypatch.setattr(host_module.psutil, "disk_usage", _disk_usage)
    writer = MemoryRetainingMetricsWriter()
    cfg = HostCollectorConfig(name="host", extra_mountpoints=[])
    result = await HostCollector().run(_ctx(writer, cfg))
    # Error recorded for /config; /host-compose → /storage still emitted
    assert result.ok is False
    assert any("disk_usage /config" in e for e in result.errors)
    disk_mps = {
        e.labels["mountpoint"] for e in writer.snapshot() if e.name == "homelab_host_disk_bytes"
    }
    assert "/storage" in disk_mps
    assert "/" not in disk_mps  # /config failed, never mapped to /


@pytest.mark.asyncio
async def test_disk_relabel_extra_mountpoints_in_map_are_emitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """extra_mountpoints that ARE in the relabel map are emitted with friendly label."""
    _patch_psutil_happy_path(monkeypatch)

    def _parts(all: bool = False) -> list[_Part]:
        del all
        return []  # No disk_partitions results

    monkeypatch.setattr(host_module.psutil, "disk_partitions", _parts)
    writer = MemoryRetainingMetricsWriter()
    cfg = HostCollectorConfig(
        name="host",
        extra_mountpoints=["/host-compose"],
        disk_mountpoint_relabel={"/host-compose": "/storage"},
    )
    await HostCollector().run(_ctx(writer, cfg))
    disk_mps = {
        e.labels["mountpoint"] for e in writer.snapshot() if e.name == "homelab_host_disk_bytes"
    }
    assert disk_mps == {"/storage"}


@pytest.mark.asyncio
async def test_process_iter_with_no_such_process_caught(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """process_iter with NoSuchProcess exception is caught and skipped."""
    _patch_psutil_happy_path(monkeypatch)

    class _DeadProc:
        """Process that raises NoSuchProcess on .status()."""

        def __init__(self) -> None:
            self.info: dict[str, Any] = {
                "pid": 999,
                "name": "dead",
                "cpu_percent": 0.0,
                "memory_info": _MemInfo(rss=0),
            }

        def status(self) -> str:
            import psutil as psutil_mod  # noqa: PLC0415

            raise psutil_mod.NoSuchProcess(pid=999, name="dead")

    def _procs(attrs: list[str] | None = None) -> list[_DeadProc | _FakeProc]:
        del attrs
        return [
            _DeadProc(),
            _FakeProc(1, "init", 1.0, 1024, "sleeping"),
        ]

    monkeypatch.setattr(host_module.psutil, "process_iter", _procs)
    writer = MemoryRetainingMetricsWriter()
    cfg = HostCollectorConfig(name="host")
    result = await HostCollector().run(_ctx(writer, cfg))
    assert result.ok
    # Verify the dead process is not in output; only "init" should be there
    top_pids = {
        e.labels.get("pid")
        for e in writer.snapshot()
        if e.name == "homelab_host_top_processes_cpu_percent"
    }
    assert "999" not in top_pids


@pytest.mark.asyncio
async def test_base_metrics_writer_top_n_processes_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HostCollector with base InMemoryMetricsWriter completes; top-N families silently skipped."""
    from typing import cast  # noqa: PLC0415

    _patch_psutil_happy_path(monkeypatch)
    writer = InMemoryMetricsWriter()  # base class, NOT retaining
    cfg = HostCollectorConfig(name="host", top_n_processes=10)
    result = await HostCollector().run(_ctx(cast(MemoryRetainingMetricsWriter, writer), cfg))
    assert result.ok
    names = {e.name for e in writer.recorded}
    # Top-N families absent (defensive guard at host.py:301 short-circuits)
    assert "homelab_host_top_processes_cpu_percent" not in names
    assert "homelab_host_top_processes_memory_bytes" not in names
    # Other metrics still emitted
    assert "homelab_host_cpu_percent" in names


# Real-psutil smoke test — may fail on container runners without procfs access.
# Not marked @pytest.mark.smoke because the marker is not yet registered in pyproject.toml.
@pytest.mark.asyncio
async def test_run_with_real_psutil() -> None:
    """Smoke test against real psutil — values vary per host, just confirm types/keys."""
    writer = MemoryRetainingMetricsWriter()
    cfg = HostCollectorConfig(name="host", extra_mountpoints=[])
    result = await HostCollector().run(_ctx(writer, cfg))
    # ok may be False if a single section failed on this CI host (e.g., no disks);
    # the smoke test asserts the collector ran and produced *some* metrics.
    assert result.metrics_emitted > 0
    assert any(e.name == "homelab_host_cpu_percent" for e in writer.snapshot())
    assert any(e.name == "homelab_host_memory_bytes" for e in writer.snapshot())


def test_build_exclude_pattern_empty_returns_none() -> None:
    """_build_exclude_pattern returns None for empty input."""
    from homelab_monitor.plugins.collectors.builtin.host import (  # noqa: PLC0415
        _build_exclude_pattern,  # pyright: ignore[reportPrivateUsage]
    )

    assert _build_exclude_pattern([]) is None


# ---------------------------------------------------------------------------
# T4 — host.py uptime bugfix (STAGE-002-010)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_emits_uptime_from_host_btime_when_proc_available(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """With HM_HOST_PROC_DIR pointing at a fake stat file, uptime is computed
    from host btime rather than psutil.boot_time()."""
    from pathlib import Path  # noqa: PLC0415

    _patch_psutil_happy_path(monkeypatch)

    # Write a fake /proc/stat with a different btime than psutil's mock (1_700_000_000)
    # psutil mock: boot=1_700_000_000, time=1_700_000_100 → uptime 100s
    # host btime:  1_700_000_050 → uptime from host = time(1_700_000_100) - 1_700_000_050 = 50s
    host_proc_dir = Path(str(tmp_path)) / "proc"
    host_proc_dir.mkdir()
    (host_proc_dir / "stat").write_text("cpu  0\nbtime 1700000050\n")

    monkeypatch.setenv("HM_HOST_PROC_DIR", str(host_proc_dir))

    writer = MemoryRetainingMetricsWriter()
    cfg = HostCollectorConfig(name="host")
    result = await HostCollector().run(_ctx(writer, cfg))

    assert result.ok
    up = [e for e in writer.snapshot() if e.name == "homelab_host_uptime_seconds"]
    assert len(up) == 1
    # uptime = time.time() - host_btime = 1_700_000_100 - 1_700_000_050 = 50s
    assert up[0].value == 50.0  # noqa: PLR2004


@pytest.mark.asyncio
async def test_run_emits_uptime_seconds_fallback_to_psutil_when_no_host_proc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without /host/proc (default env), uptime falls back to psutil.boot_time().

    This is the existing test_run_emits_uptime_seconds under a new name to confirm
    the fallback path still works after the host.py bugfix.
    """
    _patch_psutil_happy_path(monkeypatch)
    # Ensure HM_HOST_PROC_DIR points at something that doesn't exist
    monkeypatch.setenv("HM_HOST_PROC_DIR", "/nonexistent-proc-dir-for-test")

    writer = MemoryRetainingMetricsWriter()
    cfg = HostCollectorConfig(name="host")
    result = await HostCollector().run(_ctx(writer, cfg))

    assert result.ok
    up = [e for e in writer.snapshot() if e.name == "homelab_host_uptime_seconds"]
    assert len(up) == 1
    assert up[0].value == 100.0  # noqa: PLR2004  (psutil mock: time=100_offset - boot=0_offset)


# ---------------------------------------------------------------------------
# STAGE-005A-009 — host temperature collection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_emits_temperature_series_with_chip_sensor_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path: 3 temp series emitted; only nvme emits high/crit limit metrics."""
    _patch_psutil_happy_path(monkeypatch)
    monkeypatch.setattr(host_module.psutil, "sensors_temperatures", _sensors_temperatures_stub)
    writer = MemoryRetainingMetricsWriter()
    cfg = HostCollectorConfig(name="host")
    result = await HostCollector().run(_ctx(writer, cfg))
    assert result.ok

    temps = [e for e in writer.snapshot() if e.name == "homelab_host_temperature_celsius"]
    by_labels = {(e.labels["chip"], e.labels["sensor"]): e.value for e in temps}
    assert by_labels == {
        ("k10temp", "Tctl"): 54.0,
        ("nvme", "Composite"): 46.0,
        ("amdgpu", "edge"): 42.0,
    }

    # Only nvme reports non-None high/critical -> only nvme emits the limit metrics.
    highs = [e for e in writer.snapshot() if e.name == "homelab_host_temperature_high_celsius"]
    crits = [e for e in writer.snapshot() if e.name == "homelab_host_temperature_critical_celsius"]
    assert {(e.labels["chip"], e.labels["sensor"]) for e in highs} == {("nvme", "Composite")}
    assert {(e.labels["chip"], e.labels["sensor"]) for e in crits} == {("nvme", "Composite")}
    assert highs[0].value == 89.85  # noqa: PLR2004
    assert crits[0].value == 93.85  # noqa: PLR2004


@pytest.mark.asyncio
async def test_run_temperature_empty_label_falls_back_to_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A chip whose entry has an empty label gets sensor='<index>'."""
    _patch_psutil_happy_path(monkeypatch)

    def _stub() -> dict[str, list[_ShwTemp]]:
        return {
            "acpitz": [
                _ShwTemp(label="", current=40.0, high=None, critical=None),
                _ShwTemp(label="", current=41.0, high=None, critical=None),
            ]
        }

    monkeypatch.setattr(host_module.psutil, "sensors_temperatures", _stub)
    writer = MemoryRetainingMetricsWriter()
    cfg = HostCollectorConfig(name="host")
    await HostCollector().run(_ctx(writer, cfg))
    sensors = {
        e.labels["sensor"]
        for e in writer.snapshot()
        if e.name == "homelab_host_temperature_celsius"
    }
    assert sensors == {"0", "1"}


@pytest.mark.asyncio
async def test_run_temperature_exclude_drops_chip_and_chip_sensor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """exclude_temp_sensors drops by bare chip prefix AND by chip:sensor prefix."""
    _patch_psutil_happy_path(monkeypatch)
    monkeypatch.setattr(host_module.psutil, "sensors_temperatures", _sensors_temperatures_stub)
    writer = MemoryRetainingMetricsWriter()
    # "amdgpu" drops the whole amdgpu chip; "nvme:Composite" drops that one sensor.
    cfg = HostCollectorConfig(name="host", exclude_temp_sensors=["amdgpu", "nvme:Composite"])
    await HostCollector().run(_ctx(writer, cfg))
    pairs = {
        (e.labels["chip"], e.labels["sensor"])
        for e in writer.snapshot()
        if e.name == "homelab_host_temperature_celsius"
    }
    assert pairs == {("k10temp", "Tctl")}
    # nvme excluded -> its high/crit limit metrics must also be absent.
    assert not [e for e in writer.snapshot() if e.name == "homelab_host_temperature_high_celsius"]


@pytest.mark.asyncio
async def test_run_temperature_absent_sensors_temperatures_is_not_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When psutil lacks sensors_temperatures, emit nothing and stay ok."""
    _patch_psutil_happy_path(monkeypatch)
    monkeypatch.delattr(host_module.psutil, "sensors_temperatures", raising=False)
    writer = MemoryRetainingMetricsWriter()
    cfg = HostCollectorConfig(name="host")
    result = await HostCollector().run(_ctx(writer, cfg))
    assert result.ok is True
    assert not [e for e in writer.snapshot() if e.name == "homelab_host_temperature_celsius"]


@pytest.mark.asyncio
async def test_run_temperature_psutil_raises_records_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A raised sensors_temperatures() is recorded as an error, ok becomes False."""
    _patch_psutil_happy_path(monkeypatch)

    def _raise() -> dict[str, list[_ShwTemp]]:
        raise OSError("simulated sensor failure")

    monkeypatch.setattr(host_module.psutil, "sensors_temperatures", _raise)
    writer = MemoryRetainingMetricsWriter()
    cfg = HostCollectorConfig(name="host")
    result = await HostCollector().run(_ctx(writer, cfg))
    assert result.ok is False
    assert any("temperatures" in e for e in result.errors)


@pytest.mark.asyncio
async def test_run_temperature_empty_dict_is_not_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty sensors dict emits nothing and stays ok (host without sensors)."""
    _patch_psutil_happy_path(monkeypatch)
    monkeypatch.setattr(host_module.psutil, "sensors_temperatures", _no_sensors_stub)
    writer = MemoryRetainingMetricsWriter()
    cfg = HostCollectorConfig(name="host")
    result = await HostCollector().run(_ctx(writer, cfg))
    assert result.ok is True
    assert not [e for e in writer.snapshot() if e.name == "homelab_host_temperature_celsius"]
