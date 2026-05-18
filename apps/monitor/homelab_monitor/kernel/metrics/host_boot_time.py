"""Read host boot time from /host/proc/stat (the bind-mounted host /proc).

The monitor runs in a container; psutil.boot_time() reads the CONTAINER's
/proc and returns container start time, not host boot time. The compose
file bind-mounts the host /proc read-only at /host/proc so the real host
btime is available. Dev rigs and unit tests have no /host/proc — callers
fall back gracefully (return None).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

_DEFAULT_HOST_PROC = "/host/proc"
_BTIME_FIELDS = 2  # "btime <epoch>"


def _proc_stat_path() -> Path:
    """Path to the host proc stat file. Override dir via HM_HOST_PROC_DIR."""
    base = os.environ.get("HM_HOST_PROC_DIR", _DEFAULT_HOST_PROC)
    return Path(base) / "stat"


def read_host_btime() -> float | None:
    """Return host boot time as a Unix epoch float, or None if unavailable.

    None is returned when /host/proc/stat is absent (dev rig), unreadable,
    or has no btime line. Callers MUST handle None (skip @reboot lateness /
    fall back to psutil for host uptime).
    """
    path = _proc_stat_path()
    try:
        text = path.read_text(encoding="utf-8")
    except (FileNotFoundError, PermissionError, OSError):
        return None
    for line in text.splitlines():
        if line.startswith("btime "):
            parts = line.split()
            if len(parts) >= _BTIME_FIELDS:
                try:
                    return float(parts[1])
                except ValueError:
                    return None
    return None


def read_host_btime_dt() -> datetime | None:
    """Host boot time as a tz-aware UTC datetime, or None if unavailable."""
    epoch = read_host_btime()
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, tz=UTC)
