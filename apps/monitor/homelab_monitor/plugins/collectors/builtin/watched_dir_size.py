"""WatchedDirSizeCollector — emits homelab_host_directory_* metrics.

Measures recursive byte size of a configured list of host directories. Each
configured directory is mounted READ-ONLY into the container at
``/host-watch/<name>`` (see scripts/generate-watched-dirs-mounts.sh); the
collector walks the container path but emits the FRIENDLY host path as the
``path`` label (mirrors HostCollector's disk_mountpoint_relabel idea).

Walks run in a thread executor (sync os.walk must not block the event loop).
Each walk has a monotonic deadline (25s, inside the 30s collector timeout);
hitting it truncates the size (a lower bound) and sets the truncated gauge.
``-x`` semantics: subdirectories on a different filesystem than the top dir are
pruned (no cross-filesystem descent). Subdirectories that could not be stat'd or
listed (permission denied or vanished mid-walk) are skipped gracefully and counted.

This collector runs slower and with longer timeout than HostCollector (60s interval
with 30s timeout vs HostCollector's 10s interval with 5s timeout) because a recursive
walk of large trees is expensive. The extended timeout isolation prevents one slow walk
from timing out the HostCollector's queries. Config is read from ctx.config via getattr
with the baked default below (STAGE-008-032 added per-collector YAML loading via config_class).
"""

from __future__ import annotations

import asyncio
import os
import time
from datetime import timedelta
from typing import ClassVar

from pydantic import BaseModel, Field

from homelab_monitor.kernel.plugins.base import BaseCollector
from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.types import (
    CollectorConfig,
    CollectorResult,
    RunKind,
    TrustLevel,
)

_GIB = 1024**3
# Walk budget per directory. Kept inside the 30s collector timeout so the
# executor thread always returns before the scheduler cancels the tick.
WALK_BUDGET_S = 25.0

HOST_WATCH_PREFIX = "/host-watch"


class WatchedDirectory(BaseModel):
    """A single watched directory + its absolute warn/crit byte thresholds.

    ``path`` is the FRIENDLY host path (e.g. ``/var``), used as the emitted
    ``path`` label. The container reads the mirror mount at
    ``/host-watch/<name>``. warn/crit thresholds are informational here (the
    real alerting lives in deploy/vmalert/metrics/watched_directories.yaml);
    they document the intended thresholds and keep config + rules in sync.
    """

    model_config = {"extra": "forbid"}

    path: str
    warn_bytes: int = Field(gt=0)
    crit_bytes: int = Field(gt=0)


def _default_watched_directories() -> list[WatchedDirectory]:
    return [
        WatchedDirectory(path="/tmp", warn_bytes=1 * _GIB, crit_bytes=4 * _GIB),
        WatchedDirectory(path="/var", warn_bytes=10 * _GIB, crit_bytes=25 * _GIB),
    ]


class WatchedDirSizeCollectorConfig(CollectorConfig):
    """Per-host overrides for the watched-directory size collector.

    STAGE-008-032 wired subclass-aware construction: ``PluginLoader.register()``
    validates against ``WatchedDirSizeCollector.config_class`` (this subclass) and
    merges per-collector YAML from ``/config/plugins/collectors/watched_dir_size.yaml``
    when present. The collector still reads ``watched_directories`` via
    ``getattr(ctx.config, ...)`` with the default below (defensive).
    """

    watched_directories: list[WatchedDirectory] = Field(
        default_factory=_default_watched_directories
    )


def container_name(friendly_path: str) -> str:
    """Derive the /host-watch mount name for a friendly host path.

    Strip leading '/', reject '/' (root), replace remaining '/' with '-'.
    '/var' -> 'var'; '/var/log' -> 'var-log'; '/tmp' -> 'tmp'.
    """
    stripped = friendly_path.strip("/")
    if not stripped:
        raise ValueError(f"watched directory path may not be '/': {friendly_path!r}")
    return stripped.replace("/", "-")


def container_path(friendly_path: str) -> str:
    """Map a friendly host path to its container mirror under /host-watch."""
    return f"{HOST_WATCH_PREFIX}/{container_name(friendly_path)}"


class WalkResult:
    """Plain result holder for a single recursive walk."""

    __slots__ = ("total_bytes", "truncated", "unreadable_subdirs")

    def __init__(self) -> None:
        self.total_bytes: int = 0
        self.truncated: bool = False
        self.unreadable_subdirs: int = 0


def walk_dir(cpath: str) -> WalkResult:
    """Recursively sum file sizes under cpath (sync; run in executor).

    - Missing/inaccessible top dir -> all-zero result (graceful).
    - Per-iteration monotonic deadline -> sets truncated, breaks early.
    - st_dev pruning -> no cross-filesystem descent (like `du -x`).
    - onerror handler -> counts unreadable subdirs (PermissionError), never raises.
    """
    result = WalkResult()

    try:
        top_st = os.stat(cpath)
    except OSError:
        # Missing mount / inaccessible top dir -> size 0, not an error.
        return result
    top_dev = top_st.st_dev

    def _onerror(_exc: OSError) -> None:
        # os.walk could not list a subdir (permission denied or vanished). Count + skip.
        result.unreadable_subdirs += 1

    deadline = time.monotonic() + WALK_BUDGET_S

    for root, dirnames, filenames in os.walk(cpath, followlinks=False, onerror=_onerror):
        if time.monotonic() >= deadline:
            result.truncated = True
            break

        # st_dev pruning: drop subdirs on a different device than the top dir.
        pruned: list[str] = []
        for d in dirnames:
            sub = os.path.join(root, d)
            try:
                if os.lstat(sub).st_dev == top_dev:
                    pruned.append(d)
            except OSError:
                result.unreadable_subdirs += 1
        dirnames[:] = pruned

        for name in filenames:
            fpath = os.path.join(root, name)
            try:
                result.total_bytes += os.lstat(fpath).st_size
            except OSError:
                # vanished/unreadable file mid-walk -> skip silently.
                continue

    return result


class WatchedDirSizeCollector(BaseCollector):
    """Emit homelab_host_directory_* gauges for each configured watched dir.

    Longer timeout than HostCollector (30s vs HostCollector's 5s) because recursive
    walks are expensive and need isolation. Each configured directory is walked in a
    thread executor with a monotonic deadline. Never fails the whole tick on a
    single unreadable directory; per-path errors are recorded and surfaced via
    the truncated / unreadable_subdirs gauges.
    """

    name: ClassVar[str] = "watched_dir_size"
    interval: ClassVar[timedelta] = timedelta(seconds=60)
    timeout: ClassVar[timedelta] = timedelta(seconds=30)
    concurrency_group: ClassVar[str] = "watched_dir_size"
    run_kind: ClassVar[RunKind] = RunKind.ASYNC
    trust_level: ClassVar[TrustLevel] = TrustLevel.BUILTIN
    config_class: ClassVar[type[CollectorConfig]] = WatchedDirSizeCollectorConfig

    async def run(self, ctx: CollectorContext) -> CollectorResult:
        """Walk each configured directory and emit its three gauges."""
        start = time.monotonic()
        errors: list[str] = []
        emitted = 0

        watched: list[WatchedDirectory] = list(
            getattr(ctx.config, "watched_directories", _default_watched_directories())
        )

        loop = asyncio.get_running_loop()

        for entry in watched:
            friendly = entry.path
            try:
                cpath = container_path(friendly)
            except ValueError as exc:
                errors.append(str(exc))
                continue

            try:
                walk = await loop.run_in_executor(None, walk_dir, cpath)
            except Exception as exc:  # pragma: no cover -- defensive: executor failure
                errors.append(f"{friendly}: {exc}")
                continue

            ctx.vm.write_gauge(
                "homelab_host_directory_bytes",
                float(walk.total_bytes),
                {"path": friendly},
            )
            ctx.vm.write_gauge(
                "homelab_host_directory_walk_truncated",
                1.0 if walk.truncated else 0.0,
                {"path": friendly},
            )
            ctx.vm.write_gauge(
                "homelab_host_directory_unreadable_subdirs",
                float(walk.unreadable_subdirs),
                {"path": friendly},
            )
            emitted += 3

        return CollectorResult(
            ok=(len(errors) == 0),
            metrics_emitted=emitted,
            errors=errors,
            events=[],
            duration_seconds=time.monotonic() - start,
        )
