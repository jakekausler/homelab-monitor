"""Host-filesystem NFS mount-health collector (STAGE-008-018).

Probes a configured list of host-filesystem mount points (typically NFS mounts
from the Synology NAS, e.g. ``/rackstation`` and nested paths like
``/rackstation/Media/TV``) by calling :func:`os.statvfs` in a DEDICATED bounded
thread pool, guarded by an :func:`asyncio.wait_for` timeout.

Why a dedicated bounded pool: a hard-mounted NFS export whose server has gone away
makes ``statvfs`` block uninterruptibly in the kernel. Running it in the default
asyncio executor would let a single wedged mount eventually starve every other
thread-pool task. ``_EXECUTOR`` caps the blast radius at 4 leaked threads.

Metric families (one series per ``mount`` label):

- ``homelab_synology_mount_up``            — 1.0 responsive, 0.0 missing/hung/error.
  ALWAYS SEEDED to 0.0 first, then overwritten to 1.0 only on a successful statvfs.
  The seed guarantees the series never goes absent, so alerting rules have a stable
  series to evaluate (mirrors the seed-then-overwrite idiom in
  ``integrations/unifi/controller_up.py``).
- ``homelab_synology_mount_probe_seconds`` — wall time of the statvfs probe (emitted
  for present mounts whether they succeed, time out, or error).
- ``homelab_synology_mount_free_bytes``    — ``f_bavail * f_frsize`` (responsive only).
- ``homelab_synology_mount_total_bytes``   — ``f_blocks * f_frsize`` (responsive only).

Failure semantics: ``ok`` is ALWAYS True. Every tick emits at least the seeds, so
the collector always did its job; an all-down round is a SUCCESSFUL probe round
whose signal is the seeds sitting at 0.0. Per-mount errors are reported in
``errors`` for observability but do not flip ``ok``.

Config: ``synology_mounts: list[str]`` — host mount-point paths to probe. Default
EMPTY (open-source-safe): an empty list is a no-op (emits nothing, ok=True).
"""

from __future__ import annotations

import asyncio
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from typing import ClassVar, Final

from pydantic import Field

from homelab_monitor.kernel.plugins.base import BaseCollector
from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.types import (
    CollectorConfig,
    CollectorResult,
    RunKind,
    TrustLevel,
)

# --- Metric names -----------------------------------------------------------
M_MOUNT_UP: Final[str] = "homelab_synology_mount_up"
M_PROBE_SECONDS: Final[str] = "homelab_synology_mount_probe_seconds"
M_FREE_BYTES: Final[str] = "homelab_synology_mount_free_bytes"
M_TOTAL_BYTES: Final[str] = "homelab_synology_mount_total_bytes"

# --- Tunables ---------------------------------------------------------------
_PROBE_TIMEOUT: Final[float] = 5.0  # seconds; statvfs hung-mount guard
_EXECUTOR_MAX_WORKERS: Final[int] = 4  # bounded blast radius for wedged statvfs
_MOUNTINFO_PATH: Final[str] = "/proc/self/mountinfo"

# Mountinfo field layout (man 5 proc, /proc/<pid>/mountinfo):
#   0:mount-id 1:parent-id 2:major:minor 3:root 4:MOUNT-POINT 5:options ...
# We only need field index 4 (the mount point / target path). A well-formed line
# has at least 5 whitespace-separated fields; shorter lines are malformed and
# skipped.
_MOUNTINFO_TARGET_FIELD: Final[int] = 4
_MOUNTINFO_MIN_FIELDS: Final[int] = 5

# Dedicated bounded pool. Module-level => created once, lives for process lifetime
# (no explicit shutdown: a wedged statvfs would block shutdown anyway; capping at
# 4 workers is the whole point). pyright-clean concrete stdlib type.
_EXECUTOR: Final[ThreadPoolExecutor] = ThreadPoolExecutor(
    max_workers=_EXECUTOR_MAX_WORKERS,
    thread_name_prefix="mount-health",
)


class SynologyMountHealthCollectorConfig(CollectorConfig):
    """Per-host overrides for the mount-health collector.

    STAGE-008-032 wired subclass-aware construction: ``PluginLoader.register()``
    validates against ``SynologyMountHealthCollector.config_class`` (this subclass)
    and merges per-collector YAML from
    ``/config/plugins/collectors/synology_mount_health.yaml`` when present. The
    collector still reads ``synology_mounts`` via ``getattr(ctx.config, ...)`` with
    the same default (defensive; a subclass instance always carries it).
    """

    # Host mount-point paths to probe. EMPTY = open-source-safe no-op.
    synology_mounts: list[str] = Field(default_factory=list)


def parse_mountinfo(text: str) -> set[str]:
    """Extract the set of mount-target paths from ``/proc/self/mountinfo`` text.

    Pure function (no I/O) so it is trivially unit-testable. Handles arbitrary
    mount-target depth (e.g. ``/rackstation/Media/TV``). Malformed lines — fewer
    than ``_MOUNTINFO_MIN_FIELDS`` whitespace-separated fields, including blank
    lines — are skipped.
    """
    targets: set[str] = set()
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < _MOUNTINFO_MIN_FIELDS:
            continue
        targets.add(fields[_MOUNTINFO_TARGET_FIELD])
    return targets


def read_mountinfo(path: str = _MOUNTINFO_PATH) -> str:
    """Read the raw mountinfo file. Separated for test monkeypatching."""
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def present_mounts() -> set[str]:
    """Return the set of currently-mounted target paths. Never blocks on NFS.

    Reading ``/proc/self/mountinfo`` enumerates the kernel mount table; it does
    NOT stat the underlying filesystems, so a wedged hard-NFS mount still appears
    here without hanging.
    """
    return parse_mountinfo(read_mountinfo())


def statvfs(path: str) -> os.statvfs_result:
    """Thin wrapper around :func:`os.statvfs`. Separated for test monkeypatching.

    This is what runs inside ``_EXECUTOR``; tests monkeypatch ``mod.statvfs`` to a
    fake so the real filesystem is never touched.
    """
    return os.statvfs(path)


class SynologyMountHealthCollector(BaseCollector):
    """Probe host NFS mount points for liveness + free/total space.

    See module docstring for metric families and failure semantics.
    """

    name: ClassVar[str] = "synology_mount_health"
    interval: ClassVar[timedelta] = timedelta(seconds=60)
    timeout: ClassVar[timedelta] = timedelta(seconds=30)
    concurrency_group: ClassVar[str] = "host"
    run_kind: ClassVar[RunKind] = RunKind.ASYNC
    trust_level: ClassVar[TrustLevel] = TrustLevel.BUILTIN
    config_class: ClassVar[type[CollectorConfig]] = SynologyMountHealthCollectorConfig

    async def run(self, ctx: CollectorContext) -> CollectorResult:
        """Run a single mount-health tick. ``ok`` is always True (see module doc)."""
        start = time.monotonic()
        errors: list[str] = []
        emitted = 0

        mounts: list[str] = list(getattr(ctx.config, "synology_mounts", []))
        if not mounts:
            # No-op: nothing configured (open-source-safe default).
            return CollectorResult(
                ok=True,
                metrics_emitted=0,
                errors=errors,
                events=[],
                duration_seconds=time.monotonic() - start,
            )

        present = present_mounts()
        loop = asyncio.get_running_loop()

        for mount in mounts:
            # SEED 0.0 always — guarantees the series is never absent.
            ctx.vm.write_gauge(M_MOUNT_UP, 0.0, {"mount": mount})
            emitted += 1

            if mount not in present:
                # Configured but not mounted: leave mount_up at 0, no probe.
                continue

            t0 = time.monotonic()
            try:
                st = await asyncio.wait_for(
                    loop.run_in_executor(_EXECUTOR, statvfs, mount),
                    timeout=_PROBE_TIMEOUT,
                )
            except (TimeoutError, OSError) as exc:
                # Hung (TimeoutError) or error (OSError): emit the probe duration so
                # we can see how long it took to fail; mount_up stays at the 0 seed.
                elapsed = time.monotonic() - t0
                ctx.vm.write_gauge(M_PROBE_SECONDS, elapsed, {"mount": mount})
                emitted += 1
                errors.append(f"{mount}: {exc}")
                continue

            # Responsive.
            elapsed = time.monotonic() - t0
            ctx.vm.write_gauge(M_PROBE_SECONDS, elapsed, {"mount": mount})
            emitted += 1
            ctx.vm.write_gauge(M_MOUNT_UP, 1.0, {"mount": mount})  # OVERWRITE seed
            emitted += 1
            ctx.vm.write_gauge(M_FREE_BYTES, float(st.f_bavail * st.f_frsize), {"mount": mount})
            emitted += 1
            ctx.vm.write_gauge(M_TOTAL_BYTES, float(st.f_blocks * st.f_frsize), {"mount": mount})
            emitted += 1

        return CollectorResult(
            ok=True,
            metrics_emitted=emitted,
            errors=errors,
            events=[],
            duration_seconds=time.monotonic() - start,
        )
