"""Shared constants for the host-side cron-apply executor IPC (STAGE-002-009).

The monitor container writes request JSON files; a host-side systemd oneshot
service (hm-cron-apply) processes them and writes result JSON files. The
container is READ-ONLY toward crontabs — every crontab write happens on the
host inside the executor.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Final

#: JSON schema version of the request file. Bump on any breaking change.
REQUEST_SCHEMA_VERSION: Final[int] = 1

#: Subdir names inside the IPC directory.
REQUESTS_SUBDIR: Final[str] = "requests"
RESULTS_SUBDIR: Final[str] = "results"

#: Seconds the endpoint waits for a result before giving up.
RESULT_POLL_TIMEOUT_SECONDS: Final[float] = 30.0
#: Interval between result-file polls.
RESULT_POLL_INTERVAL_SECONDS: Final[float] = 0.25

#: Allowed crontab targets (validation mirror of the bash allow-list).
#: A user crontab is "crontab:<user>"; system targets are absolute paths.
USER_CRONTAB_PREFIX: Final[str] = "crontab:"
SYSTEM_CRONTAB_PATH: Final[str] = "/etc/crontab"
CRON_D_PREFIX: Final[str] = "/etc/cron.d/"

#: Operation kinds carried in a request's `operations` list. The executor
#: rejects any other value with `bad_request`. `unwrap-crontab` is reserved
#: for STAGE-002-009A and is NOT emitted by this stage.
OP_WRAP_CRONTAB: Final[str] = "wrap-crontab"
OP_WRITE_WRAPPER_SCRIPT: Final[str] = "write-wrapper-script"
OP_WRITE_TOKEN: Final[str] = "write-token"
OP_UNWRAP_CRONTAB: Final[str] = "unwrap-crontab"

#: Fixed host destinations for the file-write operations. These are the
#: canonical paths the executor validates against — the request NEVER carries
#: a destination path, only `content`. Mirror of the bash apply-script
#: constants; keep the two in sync.
WRAPPER_SCRIPT_HOST_PATH: Final[str] = "/usr/local/bin/cron-with-heartbeat.sh"
TOKEN_HOST_PATH: Final[str] = "/etc/homelab-monitor/heartbeat.token"


def get_ipc_dir() -> Path:
    """Return the in-container IPC directory (HM_CRON_APPLY_IPC_DIR, default /host-ipc)."""
    return Path(os.environ.get("HM_CRON_APPLY_IPC_DIR", "/host-ipc"))


def is_valid_target_crontab(target: str) -> bool:
    """True if `target` is a syntactically valid crontab target string.

    NOTE: this is a cheap pre-check on the monitor side; the executor performs
    the authoritative path validation. Keep the two in sync.
    """
    if target == SYSTEM_CRONTAB_PATH:
        return True
    if target.startswith(CRON_D_PREFIX):
        rest = target[len(CRON_D_PREFIX) :]
        return bool(rest) and "/" not in rest and ".." not in rest
    if target.startswith(USER_CRONTAB_PREFIX):
        user = target[len(USER_CRONTAB_PREFIX) :]
        return bool(user) and "/" not in user and ".." not in user
    return False


__all__ = [
    "CRON_D_PREFIX",
    "OP_UNWRAP_CRONTAB",
    "OP_WRAP_CRONTAB",
    "OP_WRITE_TOKEN",
    "OP_WRITE_WRAPPER_SCRIPT",
    "REQUESTS_SUBDIR",
    "REQUEST_SCHEMA_VERSION",
    "RESULTS_SUBDIR",
    "RESULT_POLL_INTERVAL_SECONDS",
    "RESULT_POLL_TIMEOUT_SECONDS",
    "SYSTEM_CRONTAB_PATH",
    "TOKEN_HOST_PATH",
    "USER_CRONTAB_PREFIX",
    "WRAPPER_SCRIPT_HOST_PATH",
    "get_ipc_dir",
    "is_valid_target_crontab",
]
