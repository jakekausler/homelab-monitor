"""SelfDiskCollector — emits homelab_self_disk_* metrics for the kernel's own disk budget.

Per spec §6.4: kernel allocates a fixed disk budget (default 50 GB) split
across VictoriaMetrics, VictoriaLogs, and SQLite + audit + runbook transcripts.
This collector observes actual usage per slot, emits used/budget gauges and an
overall used-percent gauge, and (when used > 95%) signals an auto-shrink event
via a counter + audit row. Actual VM/VL data eviction is deferred to a future
stage (see TODO).
"""

from __future__ import annotations

import os
import time
from datetime import timedelta
from pathlib import Path
from typing import ClassVar

from homelab_monitor.kernel.config import load_disk_budget_config
from homelab_monitor.kernel.db.audit import audit_write
from homelab_monitor.kernel.plugins.base import BaseCollector
from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.types import CollectorResult, RunKind, TrustLevel

_BYTES_PER_GIB = 1024**3
_CRITICAL_PCT = 95.0
_SHRINK_COOLDOWN_S = 300.0  # 5 minutes


def _dir_size_bytes(path: Path) -> int:
    """Return total bytes of files under path. Does not follow symlinks."""
    if not path.exists():
        return 0
    total = 0
    for root, _dirs, files in os.walk(path, followlinks=False):
        for name in files:
            try:
                fpath = Path(root) / name
                total += fpath.stat().st_size
            except OSError:  # pragma: no cover
                continue
    return total


class SelfDiskCollector(BaseCollector):
    """Observe per-tier disk usage and emit budget metrics.

    Run interval: 60s. Tracks:

    - VM data dir: ``$HOMELAB_MONITOR_VM_DATA_DIR`` (default ``/var/vm-data``)
    - VL data dir: ``$HOMELAB_MONITOR_VL_DATA_DIR`` (default ``/var/vl-data``)
    - SQLite data dir: ``$HOMELAB_MONITOR_SQLITE_DATA_DIR`` (default ``/data/sqlite``)
    - Runbook transcripts: ``$HOMELAB_MONITOR_RUNBOOK_TRANSCRIPTS_DIR``
      (default ``/data/runbook-transcripts``)

    Missing dirs are treated as 0 bytes (graceful degradation).
    """

    name: ClassVar[str] = "self_disk"
    interval: ClassVar[timedelta] = timedelta(seconds=60)
    timeout: ClassVar[timedelta] = timedelta(seconds=10)
    concurrency_group: ClassVar[str] = "self_disk"
    run_kind: ClassVar[RunKind] = RunKind.ASYNC
    trust_level: ClassVar[TrustLevel] = TrustLevel.BUILTIN

    def __init__(self) -> None:
        super().__init__()
        self._last_decision_ts: float | None = None

    async def run(self, ctx: CollectorContext) -> CollectorResult:
        """Run a single tick. Emits 9 metrics + (conditionally) audit + counter."""
        start = time.monotonic()
        errors: list[str] = []
        emitted = 0

        try:
            cfg = load_disk_budget_config()
        except (ValueError, OSError) as exc:
            errors.append(f"config: {exc}")
            return CollectorResult(
                ok=False,
                metrics_emitted=0,
                errors=errors,
                events=[],
                duration_seconds=time.monotonic() - start,
            )

        vm_dir = Path(os.environ.get("HOMELAB_MONITOR_VM_DATA_DIR", "/var/vm-data"))
        vl_dir = Path(os.environ.get("HOMELAB_MONITOR_VL_DATA_DIR", "/var/vl-data"))
        sqlite_dir = Path(os.environ.get("HOMELAB_MONITOR_SQLITE_DATA_DIR", "/data/sqlite"))
        runbook_dir = Path(
            os.environ.get("HOMELAB_MONITOR_RUNBOOK_TRANSCRIPTS_DIR", "/data/runbook-transcripts")
        )

        # NOTE: runbook_transcripts shares cfg.sqlite_ratio with the sqlite slot
        # (the spec's "10% SQLite + audit + runbook" budget is a combined
        # allocation). Grafana panels summing budget_bytes across slots will
        # double-count this 10%. Consumers should sum only {vm, vl, sqlite}
        # OR use a unified "control_plane_used_bytes" derived metric.
        slots = {
            "vm": (vm_dir, cfg.vm_ratio),
            "vl": (vl_dir, cfg.vl_ratio),
            "sqlite": (sqlite_dir, cfg.sqlite_ratio),
            # runbook_transcripts shares the sqlite slot's budget for v1 (per spec
            # §6.4 the third slot is "sqlite + audit + runbook"); we emit a
            # SEPARATE used gauge for visibility but reuse the sqlite ratio for
            # its budget so the four budgets still sum to total_gb.
            "runbook_transcripts": (runbook_dir, cfg.sqlite_ratio),
        }

        total_budget_bytes = cfg.total_gb * _BYTES_PER_GIB
        used_total = 0
        # The "true" budget total used to compute used_pct excludes the
        # double-counted runbook_transcripts (which shares sqlite's slot).
        budget_total_for_pct = (cfg.vm_ratio + cfg.vl_ratio + cfg.sqlite_ratio) * total_budget_bytes

        for slot_name, (path, ratio) in slots.items():
            used = _dir_size_bytes(path)
            budget = total_budget_bytes * ratio
            ctx.vm.write_gauge(
                "homelab_self_disk_used_bytes",
                float(used),
                {"slot": slot_name},
            )
            ctx.vm.write_gauge(
                "homelab_self_disk_budget_bytes",
                float(budget),
                {"slot": slot_name},
            )
            emitted += 2
            if slot_name != "runbook_transcripts":
                used_total += used

        used_pct = (used_total / budget_total_for_pct * 100.0) if budget_total_for_pct > 0 else 0.0
        ctx.vm.write_gauge("homelab_self_disk_used_pct", float(used_pct), {})
        emitted += 1

        if used_pct > _CRITICAL_PCT:
            # Apply 5-minute cooldown to prevent decision spam
            now = time.monotonic()
            in_cooldown = (
                self._last_decision_ts is not None
                and (now - self._last_decision_ts) < _SHRINK_COOLDOWN_S
            )
            if not in_cooldown:
                self._last_decision_ts = now
                # TODO(future-stage): replace metric-only signaling with actual VM
                # retention reduction (`-retentionPeriod` rewrite + restart). For v1
                # we emit the counter + audit row and rely on SelfDiskCritical
                # vmalert to notify the operator.
                tier = "v1"
                ctx.vm.write_counter(
                    "homelab_self_disk_shrink_total",
                    1.0,
                    {"tier": tier},
                )
                emitted += 1
                try:
                    await audit_write(
                        ctx.db,
                        who="system:self_disk_shrinker",
                        what="auto_shrink_decision",
                        before={"used_pct": used_pct},
                        after={"tier": tier, "action": "metric_only_emitted"},
                    )
                except Exception as exc:  # pragma: no cover -- defensive: db failure mid-tick
                    errors.append(f"audit: {exc}")

        return CollectorResult(
            ok=(len(errors) == 0),
            metrics_emitted=emitted,
            errors=errors,
            events=[],
            duration_seconds=time.monotonic() - start,
        )
