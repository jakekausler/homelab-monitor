"""Shared constants for the cron heartbeat-wrapper install path (STAGE-002-009).

The wrapper invocation prefix is the ONLY contract between three components:
- installer.py rewrites a crontab line to `<WRAPPER_PATH> -- <original command>`
- the wrapper script (cron-with-heartbeat.sh) is installed at WRAPPER_PATH
- cron_parser.py unwraps any line starting with `<WRAPPER_PATH> -- ` so the
  fingerprint is computed from the INNER command (Option D convergence).

Change WRAPPER_PATH or WRAPPER_SEPARATOR and you change all three at once.
"""

from __future__ import annotations

from typing import Final

#: Absolute path the wrapper script is installed to on the host.
WRAPPER_PATH: Final[str] = "/usr/local/bin/cron-with-heartbeat.sh"

#: Separator token between the wrapper path and the original command.
WRAPPER_SEPARATOR: Final[str] = "--"

#: The exact prefix a wrapped crontab command begins with:
#: ``/usr/local/bin/cron-with-heartbeat.sh -- ``  (trailing space included).
WRAPPER_INVOCATION_PREFIX: Final[str] = f"{WRAPPER_PATH} {WRAPPER_SEPARATOR} "

#: Absolute path of the bearer-token file the wrapper reads at runtime.
TOKEN_FILE_PATH: Final[str] = "/etc/homelab-monitor/heartbeat.token"

#: Directory holding the token file (created 0755 by the installer).
TOKEN_FILE_DIR: Final[str] = "/etc/homelab-monitor"


def unwrap_command(command: str) -> str:
    """Return the inner original command if ``command`` is wrapper-invoked.

    If ``command`` begins with ``WRAPPER_INVOCATION_PREFIX``, strip the prefix
    and return the remainder byte-exact. Otherwise return ``command`` unchanged.
    Idempotent only for a single layer (a double-wrapped line is not expected;
    the installer refuses to wrap an already-wrapped line — see installer.py).
    """
    if command.startswith(WRAPPER_INVOCATION_PREFIX):
        return command[len(WRAPPER_INVOCATION_PREFIX) :]
    return command


def is_wrapped(command: str) -> bool:
    """True if ``command`` is already a wrapper invocation."""
    return command.startswith(WRAPPER_INVOCATION_PREFIX)


__all__ = [
    "TOKEN_FILE_DIR",
    "TOKEN_FILE_PATH",
    "WRAPPER_INVOCATION_PREFIX",
    "WRAPPER_PATH",
    "WRAPPER_SEPARATOR",
    "is_wrapped",
    "unwrap_command",
]
