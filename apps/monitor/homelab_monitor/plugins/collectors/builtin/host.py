"""Host collector — emits CPU, memory, swap, disk, network, uptime, processes for the host.

Uses ``psutil`` to gather metrics directly. Writes through ``ctx.vm`` (a
:class:`~homelab_monitor.kernel.plugins.io.MetricsWriter`). Top-N process
families use ``ctx.vm.replace_family(...)`` for atomic family-level updates
(epoch semantics) — see D4 in STAGE-001-012.

Real VM-backed writer lands in STAGE-001-015. This collector is backend-agnostic.
"""

from __future__ import annotations

import re
import time
from datetime import timedelta
from typing import ClassVar, NamedTuple

import psutil
from pydantic import Field

from homelab_monitor.kernel.plugins.base import BaseCollector
from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.io import MemoryRetainingMetricsWriter
from homelab_monitor.kernel.plugins.types import (
    CollectorConfig,
    CollectorResult,
    RunKind,
    TrustLevel,
)


class _ProcRow(NamedTuple):
    """Single process row captured by ``_collect_process_states``."""

    pid: int
    name: str
    cpu_percent: float
    rss: int


_TRACKED_STATES: tuple[str, ...] = ("running", "sleeping", "zombie")


def _build_exclude_pattern(devs: list[str]) -> re.Pattern[str] | None:
    """Compile a regex matching device names that begin with any of ``devs``.

    Returns None when input is empty (no exclusions). Anchored at start.
    Each device prefix is regex-escaped for literal matching.
    """
    if not devs:
        return None
    return re.compile(r"^(?:" + "|".join(re.escape(d) for d in devs) + r")")


class HostCollectorConfig(CollectorConfig):
    """Per-host collector overrides.

    NOTE: PluginLoader.register() currently constructs a base ``CollectorConfig``
    (not this subclass) — see ``kernel/plugins/loader.py``. The collector reads
    these extra fields from ``ctx.config`` via ``getattr`` with the same defaults
    declared here. STAGE-014 will introduce YAML loading and switch to subclass-
    aware construction.
    """

    extra_mountpoints: list[str] = Field(default_factory=lambda: ["/rackstation"])
    top_n_processes: int = Field(default=10, ge=1, le=100)
    exclude_disk_devs: list[str] = Field(default_factory=lambda: ["loop", "ram"])


class HostCollector(BaseCollector):
    """Emit ``homelab_host_*`` metrics for the host the monitor runs on.

    Each section catches its own psutil errors so a single failing call does not
    kill the whole tick. Aggregated errors land in ``CollectorResult.errors``;
    ``ok=False`` if any section failed.

    KeyboardInterrupt / SystemExit are deliberately NOT caught — they propagate
    to the scheduler so shutdown is respected.
    """

    name: ClassVar[str] = "host"
    interval: ClassVar[timedelta] = timedelta(seconds=10)
    timeout: ClassVar[timedelta] = timedelta(seconds=5)
    concurrency_group: ClassVar[str] = "host"
    run_kind: ClassVar[RunKind] = RunKind.ASYNC
    trust_level: ClassVar[TrustLevel] = TrustLevel.BUILTIN

    async def run(self, ctx: CollectorContext) -> CollectorResult:
        """Run a single host-metric tick. See class docstring for failure semantics."""
        start = time.monotonic()
        errors: list[str] = []
        emitted = 0

        extra_mountpoints: list[str] = list(
            getattr(ctx.config, "extra_mountpoints", ["/rackstation"])
        )
        top_n: int = int(getattr(ctx.config, "top_n_processes", 10))
        exclude_disk_devs: list[str] = list(
            getattr(ctx.config, "exclude_disk_devs", ["loop", "ram"])
        )

        for n, errs in [
            self._collect_cpu(ctx),
            self._collect_load_average(ctx),
            self._collect_memory(ctx),
            self._collect_swap(ctx),
            self._collect_disk_usage(ctx, extra_mountpoints, exclude_disk_devs),
            self._collect_disk_io(ctx, exclude_disk_devs),
            self._collect_net_io(ctx),
            self._collect_uptime(ctx),
        ]:
            emitted += n
            errors.extend(errs)

        n, errs, proc_rows = self._collect_process_states(ctx)
        emitted += n
        errors.extend(errs)

        n, errs = self._collect_top_processes(ctx, top_n, proc_rows)
        emitted += n
        errors.extend(errs)

        return CollectorResult(
            ok=(len(errors) == 0),
            metrics_emitted=emitted,
            errors=errors,
            events=[],
            duration_seconds=time.monotonic() - start,
        )

    def _collect_cpu(self, ctx: CollectorContext) -> tuple[int, list[str]]:
        """Emit aggregate + per-core CPU percent.

        FIRST-CALL CAVEAT: psutil.cpu_percent(interval=None) returns 0.0
        on the first call after module import (no prior delta to compare).
        The first tick after process boot will emit 0.0 for all CPU
        metrics. Consumers must tolerate 0.0 readings during the first
        ~10 seconds of operation.
        """
        try:
            agg = float(psutil.cpu_percent(interval=None, percpu=False))
            ctx.vm.write_gauge("homelab_host_cpu_percent", agg, {"cpu": "all"})
            per_core = psutil.cpu_percent(interval=None, percpu=True)
            for idx, val in enumerate(per_core):
                ctx.vm.write_gauge("homelab_host_cpu_percent", float(val), {"cpu": str(idx)})
            return 1 + len(per_core), []
        except Exception as exc:  # pragma: no cover -- defensive psutil failure
            return 0, [f"cpu: {exc}"]

    def _collect_load_average(self, ctx: CollectorContext) -> tuple[int, list[str]]:
        try:
            one, five, fifteen = psutil.getloadavg()
            for period, val in (("1m", one), ("5m", five), ("15m", fifteen)):
                ctx.vm.write_gauge("homelab_host_load_average", float(val), {"period": period})
            return 3, []
        except Exception as exc:  # pragma: no cover -- defensive psutil failure
            return 0, [f"load_average: {exc}"]

    def _collect_memory(self, ctx: CollectorContext) -> tuple[int, list[str]]:
        try:
            mem = psutil.virtual_memory()
            ctx.vm.write_gauge("homelab_host_memory_bytes", float(mem.used), {"type": "used"})
            ctx.vm.write_gauge(
                "homelab_host_memory_bytes", float(mem.available), {"type": "available"}
            )
            ctx.vm.write_gauge("homelab_host_memory_bytes", float(mem.total), {"type": "total"})
            return 3, []
        except Exception as exc:  # pragma: no cover -- defensive psutil failure
            return 0, [f"memory: {exc}"]

    def _collect_swap(self, ctx: CollectorContext) -> tuple[int, list[str]]:
        try:
            sw = psutil.swap_memory()
            ctx.vm.write_gauge("homelab_host_swap_bytes", float(sw.used), {"type": "used"})
            ctx.vm.write_gauge("homelab_host_swap_bytes", float(sw.total), {"type": "total"})
            return 2, []
        except Exception as exc:  # pragma: no cover -- defensive psutil failure
            return 0, [f"swap: {exc}"]

    def _collect_disk_usage(
        self,
        ctx: CollectorContext,
        extra_mountpoints: list[str],
        exclude_disk_devs: list[str],
    ) -> tuple[int, list[str]]:
        exclude_pattern = _build_exclude_pattern(exclude_disk_devs)
        emitted = 0
        errors: list[str] = []
        # mountpoints already emitted from psutil.disk_partitions(); deduplicates
        # against extra_mountpoints
        seen: set[str] = set()

        try:
            for part in psutil.disk_partitions(all=False):
                if exclude_pattern is not None and exclude_pattern.match(part.device):
                    continue
                try:
                    usage = psutil.disk_usage(part.mountpoint)
                except (FileNotFoundError, PermissionError, OSError) as exc:
                    errors.append(f"disk_usage {part.mountpoint}: {exc}")
                    continue
                if usage.total == 0:
                    continue
                ctx.vm.write_gauge(
                    "homelab_host_disk_bytes",
                    float(usage.used),
                    {"mountpoint": part.mountpoint, "type": "used"},
                )
                ctx.vm.write_gauge(
                    "homelab_host_disk_bytes",
                    float(usage.total),
                    {"mountpoint": part.mountpoint, "type": "total"},
                )
                emitted += 2
                seen.add(part.mountpoint)
        except Exception as exc:  # pragma: no cover -- defensive psutil failure
            errors.append(f"disk_partitions: {exc}")

        for mp in extra_mountpoints:
            if mp in seen:
                continue
            try:
                usage = psutil.disk_usage(mp)
            except (FileNotFoundError, PermissionError, OSError):
                continue
            if usage.total == 0:
                continue
            ctx.vm.write_gauge(
                "homelab_host_disk_bytes",
                float(usage.used),
                {"mountpoint": mp, "type": "used"},
            )
            ctx.vm.write_gauge(
                "homelab_host_disk_bytes",
                float(usage.total),
                {"mountpoint": mp, "type": "total"},
            )
            emitted += 2

        return emitted, errors

    def _collect_disk_io(
        self,
        ctx: CollectorContext,
        exclude_disk_devs: list[str],
    ) -> tuple[int, list[str]]:
        exclude_pattern = _build_exclude_pattern(exclude_disk_devs)
        try:
            io_per_disk = psutil.disk_io_counters(perdisk=True) or {}
            emitted = 0
            for disk_name, ctr in io_per_disk.items():
                if exclude_pattern is not None and exclude_pattern.match(disk_name):
                    continue
                ctx.vm.write_counter(
                    "homelab_host_disk_io_bytes_total",
                    float(ctr.read_bytes),
                    {"disk": disk_name, "direction": "read"},
                )
                ctx.vm.write_counter(
                    "homelab_host_disk_io_bytes_total",
                    float(ctr.write_bytes),
                    {"disk": disk_name, "direction": "write"},
                )
                emitted += 2
            return emitted, []
        except Exception as exc:  # pragma: no cover -- defensive psutil failure
            return 0, [f"disk_io_counters: {exc}"]

    def _collect_net_io(self, ctx: CollectorContext) -> tuple[int, list[str]]:
        try:
            net_per_iface = psutil.net_io_counters(pernic=True) or {}
            emitted = 0
            for iface, ctr in net_per_iface.items():
                ctx.vm.write_counter(
                    "homelab_host_net_bytes_total",
                    float(ctr.bytes_recv),
                    {"iface": iface, "direction": "rx"},
                )
                ctx.vm.write_counter(
                    "homelab_host_net_bytes_total",
                    float(ctr.bytes_sent),
                    {"iface": iface, "direction": "tx"},
                )
                emitted += 2
            return emitted, []
        except Exception as exc:  # pragma: no cover -- defensive psutil failure
            return 0, [f"net_io_counters: {exc}"]

    def _collect_uptime(self, ctx: CollectorContext) -> tuple[int, list[str]]:
        try:
            uptime = time.time() - psutil.boot_time()
            ctx.vm.write_gauge("homelab_host_uptime_seconds", float(uptime), {})
            return 1, []
        except Exception as exc:  # pragma: no cover -- defensive psutil failure
            return 0, [f"uptime: {exc}"]

    def _collect_process_states(
        self, ctx: CollectorContext
    ) -> tuple[int, list[str], list[_ProcRow]]:
        """Iterate processes once; emit state counts and return proc_rows.

        Returns a 3-tuple: (metrics_emitted, errors, proc_rows). proc_rows is
        passed explicitly to ``_collect_top_processes`` to avoid cross-tick
        contamination from instance-level state.
        """
        state_counts: dict[str, int] = {s: 0 for s in _TRACKED_STATES}
        proc_rows: list[_ProcRow] = []
        try:
            for proc in psutil.process_iter(["pid", "name", "cpu_percent", "memory_info"]):
                try:
                    info = proc.info  # pyright: ignore[reportAttributeAccessIssue]  # set by process_iter(attrs=...) at runtime
                    status = proc.status()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
                if status in state_counts:
                    state_counts[status] += 1
                pid = int(info.get("pid") or 0)
                pname = str(info.get("name") or "")
                cpu_pct = float(info.get("cpu_percent") or 0.0)
                mem_info = info.get("memory_info")
                rss = int(getattr(mem_info, "rss", 0) or 0)
                proc_rows.append(_ProcRow(pid=pid, name=pname, cpu_percent=cpu_pct, rss=rss))

            for state in _TRACKED_STATES:
                ctx.vm.write_gauge(
                    "homelab_host_processes_total",
                    float(state_counts[state]),
                    {"state": state},
                )
            return len(_TRACKED_STATES), [], proc_rows
        except Exception as exc:  # pragma: no cover -- defensive psutil failure
            return 0, [f"processes: {exc}"], []

    def _collect_top_processes(
        self,
        ctx: CollectorContext,
        top_n: int,
        proc_rows: list[_ProcRow],
    ) -> tuple[int, list[str]]:
        """Emit top-N CPU and top-N RSS families using proc_rows from _collect_process_states.

        ``proc_rows`` is passed explicitly (not read from instance state) to avoid
        cross-tick contamination if ``_collect_process_states`` raised before assigning.
        """
        if not isinstance(ctx.vm, MemoryRetainingMetricsWriter) or not proc_rows:
            return 0, []

        top_cpu = sorted(proc_rows, key=lambda r: r.cpu_percent, reverse=True)[:top_n]
        top_mem = sorted(proc_rows, key=lambda r: r.rss, reverse=True)[:top_n]
        cpu_entries: list[tuple[float, dict[str, str]]] = [
            (r.cpu_percent, {"name": r.name, "pid": str(r.pid)}) for r in top_cpu
        ]
        mem_entries: list[tuple[float, dict[str, str]]] = [
            (float(r.rss), {"name": r.name, "pid": str(r.pid)}) for r in top_mem
        ]
        ctx.vm.replace_family("homelab_host_top_processes_cpu_percent", cpu_entries)
        ctx.vm.replace_family("homelab_host_top_processes_memory_bytes", mem_entries)
        return len(cpu_entries) + len(mem_entries), []
