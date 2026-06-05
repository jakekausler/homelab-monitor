"""VictoriaLogs retention reconciliation (STAGE-004-022).

VL retention is a STARTUP-ONLY compose flag (-retentionPeriod); there is no
runtime VL API. So the "desired" retention is persisted to app_settings and
surfaced as a PENDING value that takes effect at the next restart. The
EFFECTIVE value is whatever VL is currently running (env, or the 30-day
default). This module reconciles the two and computes VL disk usage."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import structlog
import yaml

from homelab_monitor.kernel.config import load_disk_budget_config, load_vl_retention_days
from homelab_monitor.kernel.db.repositories.app_settings_repository import (
    AppSettingsRepository,
)
from homelab_monitor.plugins.collectors.builtin.self_disk import (
    BYTES_PER_GIB,
    dir_size_bytes,
)

_log = structlog.get_logger()

#: app_settings key for the desired VL retention (days).
VL_RETENTION_DAYS_KEY = "vl_retention_days"

RetentionSource = Literal["env", "runtime", "default"]

_MIN_RETENTION_DAYS = 1
_MAX_RETENTION_DAYS = 365


@dataclass(frozen=True, slots=True)
class RetentionState:
    """Reconciled retention view returned to the API layer."""

    retention_days: int
    pending_retention_days: int | None
    retention_source: RetentionSource
    restart_required: bool


@dataclass(frozen=True, slots=True)
class VlDiskUsage:
    """VL data-dir usage vs the VL slice of the disk budget."""

    disk_used_gb: float
    disk_used_pct: float
    budget_available: bool


def reconcile_retention(
    *,
    effective_days: int,
    override: int | None,
    env_is_set: bool,
) -> RetentionState:
    """Pure reconciliation (no I/O). See Decision D2 in the spec.

    - retention_days = effective_days (what VL is actually running).
    - pending = override if override is set AND differs from effective, else None.
    - restart_required = pending is not None.
    - source = "runtime" if pending, else "env" if env_is_set, else "default".
    """
    pending = override if (override is not None and override != effective_days) else None
    restart_required = pending is not None
    source: RetentionSource
    if pending is not None:
        source = "runtime"
    elif env_is_set:
        source = "env"
    else:
        source = "default"
    return RetentionState(
        retention_days=effective_days,
        pending_retention_days=pending,
        retention_source=source,
        restart_required=restart_required,
    )


def _env_is_set() -> bool:
    return os.environ.get("HOMELAB_MONITOR_VL_RETENTION_DAYS") is not None


async def resolve_retention(repo: AppSettingsRepository) -> RetentionState:
    """Read env + stored override and reconcile them into a RetentionState.

    If the stored value is not a valid integer (corrupt DB), treats it as no
    override (override=None) and logs a warning rather than propagating a 500."""
    effective_days = load_vl_retention_days()
    raw = await repo.get(VL_RETENTION_DAYS_KEY)
    override: int | None = None
    if raw is not None:
        try:
            override = int(raw)
        except ValueError:
            _log.warning(
                "vl_retention.corrupt_override",
                raw=raw,
                action="treating as no override",
            )
    return reconcile_retention(
        effective_days=effective_days,
        override=override,
        env_is_set=_env_is_set(),
    )


class RetentionRangeError(ValueError):
    """Raised when a requested retention is outside [1, 365]."""


async def persist_retention(repo: AppSettingsRepository, retention_days: int) -> RetentionState:
    """Validate + persist the desired retention, then return the reconciled state.

    See Decision D3. If retention_days == effective, the override is CLEARED
    (delete) so it stops nagging; otherwise it is upserted. Raises
    RetentionRangeError on out-of-range (defensive; the API layer also enforces
    the range via Field(ge,le))."""
    if retention_days < _MIN_RETENTION_DAYS or retention_days > _MAX_RETENTION_DAYS:
        msg = f"retention_days must be in [{_MIN_RETENTION_DAYS}, {_MAX_RETENTION_DAYS}]"
        raise RetentionRangeError(msg)
    effective_days = load_vl_retention_days()
    if retention_days == effective_days:
        await repo.delete(VL_RETENTION_DAYS_KEY)
    else:
        await repo.set(VL_RETENTION_DAYS_KEY, str(retention_days))
    return await resolve_retention(repo)


def compute_vl_disk_usage() -> VlDiskUsage:
    """Compute VL data-dir bytes vs the VL slice of the disk budget.

    used_bytes = size of HOMELAB_MONITOR_VL_DATA_DIR (default /var/vl-data).
    budget_bytes = total_gb * 1024**3 * vl_ratio (GiB convention, matching
    self_disk.py). disk_used_gb is GiB. disk_used_pct guards divide-by-zero.

    On config error (ValueError, OSError) degrades gracefully: returns
    disk_used_gb=0.0, disk_used_pct=0.0 so the settings endpoint never 500s."""
    vl_dir = Path(os.environ.get("HOMELAB_MONITOR_VL_DATA_DIR", "/var/vl-data"))
    used_bytes = dir_size_bytes(vl_dir)
    try:
        cfg = load_disk_budget_config()
    except (ValueError, OSError, yaml.YAMLError) as exc:
        _log.warning("vl_retention.disk_budget_config_error", error=str(exc))
        return VlDiskUsage(disk_used_gb=0.0, disk_used_pct=0.0, budget_available=False)
    budget_bytes = cfg.total_gb * BYTES_PER_GIB * cfg.vl_ratio
    disk_used_gb = used_bytes / BYTES_PER_GIB
    disk_used_pct = (100.0 * used_bytes / budget_bytes) if budget_bytes > 0 else 0.0
    return VlDiskUsage(
        disk_used_gb=disk_used_gb,
        disk_used_pct=disk_used_pct,
        budget_available=budget_bytes > 0,
    )


__all__ = [
    "VL_RETENTION_DAYS_KEY",
    "RetentionRangeError",
    "RetentionSource",
    "RetentionState",
    "VlDiskUsage",
    "compute_vl_disk_usage",
    "persist_retention",
    "reconcile_retention",
    "resolve_retention",
]
