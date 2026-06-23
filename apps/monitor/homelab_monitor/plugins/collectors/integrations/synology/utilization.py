"""synology_utilization collector — CPU / memory / per-disk / per-NIC / per-volume / NFS.

STAGE-008-008. SINGLE DSM fetch (SYNO.Core.System.Utilization v1 get) that fans out
into 28 metric families across six sections:
  - cpu     (flat dict)  -> load1/5/15 + user/system/other percent  (no label)
  - memory  (flat dict)  -> usage/swap percent + total/avail/cached/buffer bytes (no label)
  - disk    {device}     -> read/write bytes-per-second, read/write IOPS, utilization %
  - network {iface}      -> rx/tx bytes-per-second
  - space   {volume}     -> read/write bytes-per-second, utilization %
  - nfs     (no label)   -> read/write/total OPS + read/write/total max latency seconds

UNLIKE system.py (3 independent fetches), this is ONE fetch like pool.py: a client
error / unconfigured client is ok=False (via fetch_or_result early-return); a busy
NAS or a non-dict payload is still ok=True with whatever parsed.

PARSE — defensive. Each section is read with as_dict / as_list_of_dicts; a wrong
shape degrades to None / [] (family stays empty), never a crash or pyright failure.
DSM gives load-avg as int x100 (88 -> 0.88) and memory sizes in KB; _scaled applies
the per-field factor (0.01 load, 1024.0 KB->bytes, 0.001 ms->s). as_float rejects
bool and returns None on non-finite / non-numeric.

CARDINALITY: every family is cap-routed through capped_emitter + cap_for_synology
(default 500). metrics_emitted = sum(emit_family() + 1 per family) + the one
api_took gauge from the successful fetch.
"""

from __future__ import annotations

import time
from datetime import timedelta
from typing import ClassVar, Final

from homelab_monitor.kernel.plugins.base import BaseCollector
from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.types import CollectorEvent, CollectorResult
from homelab_monitor.plugins.collectors.integrations.synology._shared import (
    as_dict,
    as_float,
    as_list_of_dicts,
    cap_for_synology,
    capped_emitter,
    client_unconfigured_result,
    fetch_or_result,
)

# --- CPU families (no label) ------------------------------------------------
M_CPU_LOAD1: Final[str] = "homelab_synology_cpu_load1"
M_CPU_LOAD5: Final[str] = "homelab_synology_cpu_load5"
M_CPU_LOAD15: Final[str] = "homelab_synology_cpu_load15"
M_CPU_USER_PERCENT: Final[str] = "homelab_synology_cpu_user_percent"
M_CPU_SYSTEM_PERCENT: Final[str] = "homelab_synology_cpu_system_percent"
M_CPU_OTHER_PERCENT: Final[str] = "homelab_synology_cpu_other_percent"

# --- Memory families (no label) ---------------------------------------------
M_MEM_USAGE_PERCENT: Final[str] = "homelab_synology_mem_usage_percent"
M_SWAP_USAGE_PERCENT: Final[str] = "homelab_synology_swap_usage_percent"
M_MEM_TOTAL_BYTES: Final[str] = "homelab_synology_mem_total_bytes"
M_MEM_AVAILABLE_BYTES: Final[str] = "homelab_synology_mem_available_bytes"
M_MEM_CACHED_BYTES: Final[str] = "homelab_synology_mem_cached_bytes"
M_MEM_BUFFER_BYTES: Final[str] = "homelab_synology_mem_buffer_bytes"

# --- Disk families (label: {device}) ----------------------------------------
M_DISK_READ_BPS: Final[str] = "homelab_synology_disk_read_bytes_per_second"
M_DISK_WRITE_BPS: Final[str] = "homelab_synology_disk_write_bytes_per_second"
M_DISK_READ_IOPS: Final[str] = "homelab_synology_disk_read_iops"
M_DISK_WRITE_IOPS: Final[str] = "homelab_synology_disk_write_iops"
M_DISK_UTILIZATION_PERCENT: Final[str] = "homelab_synology_disk_utilization_percent"

# --- Network families (label: {iface}) --------------------------------------
M_NET_RX_BPS: Final[str] = "homelab_synology_net_rx_bytes_per_second"
M_NET_TX_BPS: Final[str] = "homelab_synology_net_tx_bytes_per_second"

# --- Volume I/O families (label: {volume}) ----------------------------------
M_VOL_READ_BPS: Final[str] = "homelab_synology_vol_io_read_bytes_per_second"
M_VOL_WRITE_BPS: Final[str] = "homelab_synology_vol_io_write_bytes_per_second"
M_VOL_UTILIZATION_PERCENT: Final[str] = "homelab_synology_vol_io_utilization_percent"

# --- NFS families (no label) ------------------------------------------------
M_NFS_READ_OPS: Final[str] = "homelab_synology_nfs_read_ops"
M_NFS_WRITE_OPS: Final[str] = "homelab_synology_nfs_write_ops"
M_NFS_TOTAL_OPS: Final[str] = "homelab_synology_nfs_total_ops"
M_NFS_READ_LATENCY_SECONDS: Final[str] = "homelab_synology_nfs_read_max_latency_seconds"
M_NFS_WRITE_LATENCY_SECONDS: Final[str] = "homelab_synology_nfs_write_max_latency_seconds"
M_NFS_TOTAL_LATENCY_SECONDS: Final[str] = "homelab_synology_nfs_total_max_latency_seconds"


def _scaled(v: object, factor: float) -> float | None:
    """Return ``as_float(v) * factor``, or None when v is unparseable.

    Used for unit conversions: KB->bytes (1024.0), ms->s (0.001), load/100 (0.01).
    Inherits as_float's rejection of bool / non-finite / non-numeric.
    """
    f = as_float(v)
    return None if f is None else f * factor


class _Built:
    """Per-tick observation lists, one per cap-routed metric family (28 total)."""

    __slots__ = (
        "cpu_load1_obs",
        "cpu_load5_obs",
        "cpu_load15_obs",
        "cpu_other_percent_obs",
        "cpu_system_percent_obs",
        "cpu_user_percent_obs",
        "disk_read_bps_obs",
        "disk_read_iops_obs",
        "disk_utilization_percent_obs",
        "disk_write_bps_obs",
        "disk_write_iops_obs",
        "mem_available_bytes_obs",
        "mem_buffer_bytes_obs",
        "mem_cached_bytes_obs",
        "mem_total_bytes_obs",
        "mem_usage_percent_obs",
        "net_rx_bps_obs",
        "net_tx_bps_obs",
        "nfs_read_latency_obs",
        "nfs_read_ops_obs",
        "nfs_total_latency_obs",
        "nfs_total_ops_obs",
        "nfs_write_latency_obs",
        "nfs_write_ops_obs",
        "swap_usage_percent_obs",
        "vol_read_bps_obs",
        "vol_utilization_percent_obs",
        "vol_write_bps_obs",
    )

    def __init__(self) -> None:
        """Initialise every observation list empty."""
        # CPU
        self.cpu_load1_obs: list[tuple[dict[str, str], float]] = []
        self.cpu_load5_obs: list[tuple[dict[str, str], float]] = []
        self.cpu_load15_obs: list[tuple[dict[str, str], float]] = []
        self.cpu_user_percent_obs: list[tuple[dict[str, str], float]] = []
        self.cpu_system_percent_obs: list[tuple[dict[str, str], float]] = []
        self.cpu_other_percent_obs: list[tuple[dict[str, str], float]] = []
        # Memory
        self.mem_usage_percent_obs: list[tuple[dict[str, str], float]] = []
        self.swap_usage_percent_obs: list[tuple[dict[str, str], float]] = []
        self.mem_total_bytes_obs: list[tuple[dict[str, str], float]] = []
        self.mem_available_bytes_obs: list[tuple[dict[str, str], float]] = []
        self.mem_cached_bytes_obs: list[tuple[dict[str, str], float]] = []
        self.mem_buffer_bytes_obs: list[tuple[dict[str, str], float]] = []
        # Disk
        self.disk_read_bps_obs: list[tuple[dict[str, str], float]] = []
        self.disk_write_bps_obs: list[tuple[dict[str, str], float]] = []
        self.disk_read_iops_obs: list[tuple[dict[str, str], float]] = []
        self.disk_write_iops_obs: list[tuple[dict[str, str], float]] = []
        self.disk_utilization_percent_obs: list[tuple[dict[str, str], float]] = []
        # Network
        self.net_rx_bps_obs: list[tuple[dict[str, str], float]] = []
        self.net_tx_bps_obs: list[tuple[dict[str, str], float]] = []
        # Volume I/O
        self.vol_read_bps_obs: list[tuple[dict[str, str], float]] = []
        self.vol_write_bps_obs: list[tuple[dict[str, str], float]] = []
        self.vol_utilization_percent_obs: list[tuple[dict[str, str], float]] = []
        # NFS
        self.nfs_read_ops_obs: list[tuple[dict[str, str], float]] = []
        self.nfs_write_ops_obs: list[tuple[dict[str, str], float]] = []
        self.nfs_total_ops_obs: list[tuple[dict[str, str], float]] = []
        self.nfs_read_latency_obs: list[tuple[dict[str, str], float]] = []
        self.nfs_write_latency_obs: list[tuple[dict[str, str], float]] = []
        self.nfs_total_latency_obs: list[tuple[dict[str, str], float]] = []


def _parse_cpu(built: _Built, cpu: dict[str, object]) -> None:
    """Append CPU load-avg (x0.01) + user/system/other percent observations (no label)."""
    load1 = _scaled(cpu.get("1min_load"), 0.01)
    if load1 is not None:
        built.cpu_load1_obs.append(({}, load1))

    load5 = _scaled(cpu.get("5min_load"), 0.01)
    if load5 is not None:
        built.cpu_load5_obs.append(({}, load5))

    load15 = _scaled(cpu.get("15min_load"), 0.01)
    if load15 is not None:
        built.cpu_load15_obs.append(({}, load15))

    user = as_float(cpu.get("user_load"))
    if user is not None:
        built.cpu_user_percent_obs.append(({}, user))

    system = as_float(cpu.get("system_load"))
    if system is not None:
        built.cpu_system_percent_obs.append(({}, system))

    other = as_float(cpu.get("other_load"))
    if other is not None:
        built.cpu_other_percent_obs.append(({}, other))


def _parse_memory(built: _Built, mem: dict[str, object]) -> None:
    """Append memory/swap usage percent + total/avail/cached/buffer bytes (KB->bytes)."""
    usage = as_float(mem.get("real_usage"))
    if usage is not None:
        built.mem_usage_percent_obs.append(({}, usage))

    swap = as_float(mem.get("swap_usage"))
    if swap is not None:
        built.swap_usage_percent_obs.append(({}, swap))

    total = _scaled(mem.get("total_real"), 1024.0)
    if total is not None:
        built.mem_total_bytes_obs.append(({}, total))

    avail = _scaled(mem.get("avail_real"), 1024.0)
    if avail is not None:
        built.mem_available_bytes_obs.append(({}, avail))

    cached = _scaled(mem.get("cached"), 1024.0)
    if cached is not None:
        built.mem_cached_bytes_obs.append(({}, cached))

    buffer = _scaled(mem.get("buffer"), 1024.0)
    if buffer is not None:
        built.mem_buffer_bytes_obs.append(({}, buffer))


def _parse_disk_obj(built: _Built, obj: dict[str, object]) -> None:
    """Append per-disk observations for ONE disk dict (disk.total OR a disk.disk[] item).

    PRIMARY KEY: ``device`` (str). A non-str device -> the whole obj is skipped.
    """
    device = obj.get("device")
    if not isinstance(device, str):
        return
    labels = {"device": device}

    read_bps = as_float(obj.get("read_byte"))
    if read_bps is not None:
        built.disk_read_bps_obs.append((labels, read_bps))

    write_bps = as_float(obj.get("write_byte"))
    if write_bps is not None:
        built.disk_write_bps_obs.append((labels, write_bps))

    read_iops = as_float(obj.get("read_access"))
    if read_iops is not None:
        built.disk_read_iops_obs.append((labels, read_iops))

    write_iops = as_float(obj.get("write_access"))
    if write_iops is not None:
        built.disk_write_iops_obs.append((labels, write_iops))

    util = as_float(obj.get("utilization"))
    if util is not None:
        built.disk_utilization_percent_obs.append((labels, util))


def _parse_disk(built: _Built, disk: dict[str, object]) -> None:
    """Parse the aggregate ``disk.total`` dict + each ``disk.disk[]`` per-device dict."""
    total = as_dict(disk.get("total"))
    if total is not None:
        _parse_disk_obj(built, total)
    for item in as_list_of_dicts(disk.get("disk")):
        _parse_disk_obj(built, item)


def _parse_network(built: _Built, net_list: list[dict[str, object]]) -> None:
    """Append per-iface rx/tx observations.

    PRIMARY KEY: ``device`` (str) -> {iface}. The list includes the aggregate
    element {device:"total"}, which naturally becomes {iface:"total"}.
    """
    for item in net_list:
        device = item.get("device")
        if not isinstance(device, str):
            continue
        labels = {"iface": device}

        rx = as_float(item.get("rx"))
        if rx is not None:
            built.net_rx_bps_obs.append((labels, rx))

        tx = as_float(item.get("tx"))
        if tx is not None:
            built.net_tx_bps_obs.append((labels, tx))


def _parse_space_obj(built: _Built, obj: dict[str, object]) -> None:
    """Append per-volume observations for ONE space dict (space.total OR a volume item).

    LABEL: prefer ``display_name`` ("volume1") when a str; else fall back to
    ``device`` ("dm-1" / "total"). If NEITHER is a str, skip the obj entirely.
    """
    display = obj.get("display_name")
    device = obj.get("device")
    if isinstance(display, str):
        label = display
    elif isinstance(device, str):
        label = device
    else:
        return
    labels = {"volume": label}

    read_bps = as_float(obj.get("read_byte"))
    if read_bps is not None:
        built.vol_read_bps_obs.append((labels, read_bps))

    write_bps = as_float(obj.get("write_byte"))
    if write_bps is not None:
        built.vol_write_bps_obs.append((labels, write_bps))

    util = as_float(obj.get("utilization"))
    if util is not None:
        built.vol_utilization_percent_obs.append((labels, util))


def _parse_space(built: _Built, space: dict[str, object]) -> None:
    """Parse the aggregate ``space.total`` dict + each ``space.volume[]`` per-vol dict."""
    total = as_dict(space.get("total"))
    if total is not None:
        _parse_space_obj(built, total)
    for item in as_list_of_dicts(space.get("volume")):
        _parse_space_obj(built, item)


def _parse_nfs(built: _Built, nfs_list: list[dict[str, object]]) -> None:
    """Append NFS read/write/total OPS + read/write/total max-latency (ms->s) (no label)."""
    for item in nfs_list:
        read_ops = as_float(item.get("read_OPS"))
        if read_ops is not None:
            built.nfs_read_ops_obs.append(({}, read_ops))

        write_ops = as_float(item.get("write_OPS"))
        if write_ops is not None:
            built.nfs_write_ops_obs.append(({}, write_ops))

        total_ops = as_float(item.get("total_OPS"))
        if total_ops is not None:
            built.nfs_total_ops_obs.append(({}, total_ops))

        read_lat = _scaled(item.get("read_max_latency"), 0.001)
        if read_lat is not None:
            built.nfs_read_latency_obs.append(({}, read_lat))

        write_lat = _scaled(item.get("write_max_latency"), 0.001)
        if write_lat is not None:
            built.nfs_write_latency_obs.append(({}, write_lat))

        total_lat = _scaled(item.get("total_max_latency"), 0.001)
        if total_lat is not None:
            built.nfs_total_latency_obs.append(({}, total_lat))


def _build(payload: dict[str, object]) -> _Built:
    """Single pass over the utilization payload -> populated observation lists."""
    built = _Built()

    cpu = as_dict(payload.get("cpu"))
    if cpu is not None:
        _parse_cpu(built, cpu)

    mem = as_dict(payload.get("memory"))
    if mem is not None:
        _parse_memory(built, mem)

    disk = as_dict(payload.get("disk"))
    if disk is not None:
        _parse_disk(built, disk)

    _parse_network(built, as_list_of_dicts(payload.get("network")))

    space = as_dict(payload.get("space"))
    if space is not None:
        _parse_space(built, space)

    _parse_nfs(built, as_list_of_dicts(payload.get("nfs")))

    return built


def _emit(
    ctx: CollectorContext, built: _Built, events: list[CollectorEvent], emitted: list[int]
) -> None:
    """Cap-route every family through one CappedEmitter."""
    emitter = capped_emitter(ctx, events)

    def family(name: str, obs: list[tuple[dict[str, str], float]]) -> None:
        emitted[0] += emitter.emit_family(name, cap_for_synology(name), obs) + 1

    # CPU
    family(M_CPU_LOAD1, built.cpu_load1_obs)
    family(M_CPU_LOAD5, built.cpu_load5_obs)
    family(M_CPU_LOAD15, built.cpu_load15_obs)
    family(M_CPU_USER_PERCENT, built.cpu_user_percent_obs)
    family(M_CPU_SYSTEM_PERCENT, built.cpu_system_percent_obs)
    family(M_CPU_OTHER_PERCENT, built.cpu_other_percent_obs)
    # Memory
    family(M_MEM_USAGE_PERCENT, built.mem_usage_percent_obs)
    family(M_SWAP_USAGE_PERCENT, built.swap_usage_percent_obs)
    family(M_MEM_TOTAL_BYTES, built.mem_total_bytes_obs)
    family(M_MEM_AVAILABLE_BYTES, built.mem_available_bytes_obs)
    family(M_MEM_CACHED_BYTES, built.mem_cached_bytes_obs)
    family(M_MEM_BUFFER_BYTES, built.mem_buffer_bytes_obs)
    # Disk
    family(M_DISK_READ_BPS, built.disk_read_bps_obs)
    family(M_DISK_WRITE_BPS, built.disk_write_bps_obs)
    family(M_DISK_READ_IOPS, built.disk_read_iops_obs)
    family(M_DISK_WRITE_IOPS, built.disk_write_iops_obs)
    family(M_DISK_UTILIZATION_PERCENT, built.disk_utilization_percent_obs)
    # Network
    family(M_NET_RX_BPS, built.net_rx_bps_obs)
    family(M_NET_TX_BPS, built.net_tx_bps_obs)
    # Volume I/O
    family(M_VOL_READ_BPS, built.vol_read_bps_obs)
    family(M_VOL_WRITE_BPS, built.vol_write_bps_obs)
    family(M_VOL_UTILIZATION_PERCENT, built.vol_utilization_percent_obs)
    # NFS
    family(M_NFS_READ_OPS, built.nfs_read_ops_obs)
    family(M_NFS_WRITE_OPS, built.nfs_write_ops_obs)
    family(M_NFS_TOTAL_OPS, built.nfs_total_ops_obs)
    family(M_NFS_READ_LATENCY_SECONDS, built.nfs_read_latency_obs)
    family(M_NFS_WRITE_LATENCY_SECONDS, built.nfs_write_latency_obs)
    family(M_NFS_TOTAL_LATENCY_SECONDS, built.nfs_total_latency_obs)


class SynologyUtilizationCollector(BaseCollector):
    """Emit CPU / memory / per-disk / per-NIC / per-volume / NFS metrics.

    Polls once per 60-s tick in the ``synology`` concurrency group from a SINGLE
    SYNO.Core.System.Utilization fetch. Like pool.py, only a client error or an
    unconfigured client is ok=False; a non-dict payload is ok=True (no metrics).
    """

    name: ClassVar[str] = "synology_utilization"
    interval: ClassVar[timedelta] = timedelta(seconds=60)
    timeout: ClassVar[timedelta] = timedelta(seconds=30)
    concurrency_group: ClassVar[str] = "synology"

    async def run(self, ctx: CollectorContext) -> CollectorResult:
        """Fetch utilization, parse all six sections, emit cap-routed families."""
        start = time.monotonic()
        if ctx.synology is None:
            return client_unconfigured_result(start)

        emitted: list[int] = [0]
        resp = fetch_or_result(ctx, await ctx.synology.system_utilization(), start, emitted)
        if isinstance(resp, CollectorResult):
            return resp

        events: list[CollectorEvent] = []
        payload = as_dict(resp.payload)
        if payload is not None:
            built = _build(payload)
            _emit(ctx, built, events, emitted)

        return CollectorResult(
            ok=True,
            metrics_emitted=emitted[0],
            errors=[],
            events=events,
            duration_seconds=time.monotonic() - start,
        )
