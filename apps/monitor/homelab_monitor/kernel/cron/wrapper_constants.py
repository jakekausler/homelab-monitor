"""Shared constants for the cron heartbeat-wrapper install path.

STAGE-002-012: the wrapper became a GENERIC shared script. The crontab line is
now `<WRAPPER_PATH> <fingerprint> -- <command>` — the fingerprint is a POSITIONAL
ARGUMENT, not baked into the script. So the wrapped-line prefix is no longer a
single fixed string: it is `<WRAPPER_PATH> ` ... ` -- ` with the fingerprint in
between. unwrap_command / is_wrapped match that shape.
"""

from __future__ import annotations

import re
from typing import Final

#: Absolute path the wrapper script is installed to on the host.
WRAPPER_PATH: Final[str] = "/usr/local/bin/cron-with-heartbeat.sh"

#: Separator token between the wrapper args and the original command.
WRAPPER_SEPARATOR: Final[str] = "--"

#: Absolute path of the bearer-token file the wrapper reads at runtime.
TOKEN_FILE_PATH: Final[str] = "/etc/homelab-monitor/heartbeat.token"

#: Directory holding the token + wrapper.env files.
TOKEN_FILE_DIR: Final[str] = "/etc/homelab-monitor"

#: Absolute path of the wrapper env file (HEARTBEAT_URL_BASE=...). Mode 644 —
#: it holds only a non-secret URL base.
WRAPPER_ENV_PATH: Final[str] = "/etc/homelab-monitor/wrapper.env"

#: The run-log-capable generic wrapper format version. A semver STRING. The
#: pre-run-log baked-fingerprint wrapper recorded NO wrapper_format_version.
WRAPPER_FORMAT_VERSION: Final[str] = "1.1.0"

#: Matches a wrapped crontab command (NEW format):
#:   <WRAPPER_PATH> <fingerprint> -- <inner command>
#: group 'fp' = the fingerprint argument; group 'cmd' = the inner command.
#: The fingerprint is a single shell token (no whitespace). The ` -- ` separator
#: with surrounding spaces delimits it from the inner command.
_WRAPPED_RE: Final[re.Pattern[str]] = re.compile(
    r"^"
    + re.escape(WRAPPER_PATH)
    + r" (?P<fp>\S+) "
    + re.escape(WRAPPER_SEPARATOR)
    + r" (?P<cmd>.*)$",
    re.DOTALL,
)

#: Matches a LEGACY wrapped crontab command (old baked-fingerprint format):
#:   <WRAPPER_PATH> -- <inner command>
#: group 'cmd' = the inner command. No fingerprint group (legacy format baked
#: the fingerprint into the wrapper script, not as a separate argument).
#: Used for format-migration detection: a legacy-wrapped line is invisible to
#: the NEW _WRAPPED_RE pattern and vice-versa — they match mutually-exclusive shapes.
_LEGACY_WRAPPED_RE: Final[re.Pattern[str]] = re.compile(
    r"^" + re.escape(WRAPPER_PATH) + r" " + re.escape(WRAPPER_SEPARATOR) + r" (?P<cmd>.*)$",
    re.DOTALL,
)


def build_invocation_prefix(fingerprint: str) -> str:
    """Return the exact prefix a wrapped crontab command begins with for this
    fingerprint: ``<WRAPPER_PATH> <fingerprint> -- `` (trailing space included)."""
    return f"{WRAPPER_PATH} {fingerprint} {WRAPPER_SEPARATOR} "


def unwrap_command(command: str) -> str:
    """Return the inner original command if ``command`` is a wrapper invocation.

    Recognizes BOTH the NEW format ``<WRAPPER_PATH> <fingerprint> -- <cmd>``
    and the LEGACY format ``<WRAPPER_PATH> -- <cmd>``. Strips the wrapper
    prefix regardless of format. Otherwise returns ``command`` unchanged.
    Single-layer only.
    """
    m = _WRAPPED_RE.match(command)
    if m is not None:
        return m.group("cmd")
    m = _LEGACY_WRAPPED_RE.match(command)
    if m is not None:
        return m.group("cmd")
    return command


def is_wrapped(command: str) -> bool:
    """True if ``command`` is a wrapper invocation (either NEW or LEGACY format)."""
    return _WRAPPED_RE.match(command) is not None or _LEGACY_WRAPPED_RE.match(command) is not None


def wrapped_fingerprint(command: str) -> str | None:
    """Return the fingerprint argument from a NEW-format wrapped command.

    Returns None for LEGACY-format wrapped commands (they embed no fingerprint)
    and for unwrapped commands.
    """
    m = _WRAPPED_RE.match(command)
    return None if m is None else m.group("fp")


def is_legacy_wrapped(command: str) -> bool:
    """True if ``command`` is a LEGACY-format wrapper invocation.

    A legacy-wrapped command matches _LEGACY_WRAPPED_RE but NOT _WRAPPED_RE.
    Returns False for NEW-format wrapped commands and for unwrapped commands.
    """
    return _LEGACY_WRAPPED_RE.match(command) is not None and _WRAPPED_RE.match(command) is None


__all__ = [
    "TOKEN_FILE_DIR",
    "TOKEN_FILE_PATH",
    "WRAPPER_ENV_PATH",
    "WRAPPER_FORMAT_VERSION",
    "WRAPPER_PATH",
    "WRAPPER_SEPARATOR",
    "build_invocation_prefix",
    "is_legacy_wrapped",
    "is_wrapped",
    "unwrap_command",
    "wrapped_fingerprint",
]
