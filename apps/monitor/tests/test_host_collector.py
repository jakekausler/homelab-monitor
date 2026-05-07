"""Tests for :class:`HostCollector` (psutil-mocked + one real-psutil smoke)."""

from __future__ import annotations

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


def _patch_psutil_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Apply a uniform happy-path psutil mock used by most tests."""
    monkeypatch.setattr(host_module.psutil, "cpu_percent", _cpu_percent_stub)
    monkeypatch.setattr(host_module.psutil, "getloadavg", lambda: (0.5, 1.0, 1.5))
    monkeypatch.setattr(
        host_module.psutil, "virtual_memory", lambda: _VMem(total=1024, available=512, used=512)
    )
    monkeypatch.setattr(host_module.psutil, "swap_memory", lambda: _Swap(total=2048, used=128))

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
    cfg = HostCollectorConfig(name="host", extra_mountpoints=["/rackstation"])
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
    cfg = HostCollectorConfig(name="host", extra_mountpoints=[])
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


@pytest.mark.asyncio
async def test_run_emits_net_io_counters_per_iface(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_psutil_happy_path(monkeypatch)
    writer = MemoryRetainingMetricsWriter()
    cfg = HostCollectorConfig(name="host")
    await HostCollector().run(_ctx(writer, cfg))
    net = [e for e in writer.snapshot() if e.name == "homelab_host_net_bytes_total"]
    pairs = {(e.labels["iface"], e.labels["direction"]) for e in net}
    assert pairs == {("eth0", "rx"), ("eth0", "tx")}


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
    cfg = HostCollectorConfig(name="host", extra_mountpoints=["/rackstation"])
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
    cfg = HostCollectorConfig(name="host")
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
    cfg = HostCollectorConfig(name="host", extra_mountpoints=[])
    result = await HostCollector().run(_ctx(writer, cfg))
    assert result.ok is False
    assert any("disk_usage" in e for e in result.errors)


@pytest.mark.asyncio
async def test_metrics_emitted_count_matches_actual_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_psutil_happy_path(monkeypatch)
    writer = MemoryRetainingMetricsWriter()
    cfg = HostCollectorConfig(name="host", extra_mountpoints=[])
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
    cfg = HostCollectorConfig(name="host", extra_mountpoints=[])
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
    cfg = HostCollectorConfig(name="host", extra_mountpoints=["/zero_extra", "/good_extra"])
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
    cfg = HostCollectorConfig(name="host", extra_mountpoints=["/shared"])
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
