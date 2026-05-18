"""Cron-line parser (STAGE-002-007 D8).

Hand-rolled to handle the two crontab variants:
- SYSTEM_WITH_USER_FIELD: /etc/crontab + /etc/cron.d/* (has USER column)
- USER_CRONTAB: /var/spool/cron/crontabs/* (no USER column; filename is user)

Validates schedules via croniter. Returns `list[ParsedCronEntry]` + `list[CronScanError]`.
Caller (CronDiscoverer) handles file I/O and source_path mapping.

Edge-case table (per D8):
| Input                           | Action                              |
|---------------------------------|-------------------------------------|
| Blank line                      | skip                                |
| `#` comment                     | skip                                |
| `KEY=VALUE` env var             | skip                                |
| `@reboot CMD`                   | track; @reboot schedule             |
| `@hourly/@daily/@weekly/...`    | track; croniter validates           |
| 5-field expr + cmd (USER_CRONTAB) | track; user from filename         |
| 6-field (SYSTEM_WITH_USER_FIELD) | track; user from field            |
| Malformed schedule              | error; partial=True                 |
"""

from __future__ import annotations

import re

from croniter import croniter

from homelab_monitor.kernel.cron.discovery_types import (
    CronScanError,
    CronSourceKind,
    ParsedCronEntry,
)
from homelab_monitor.kernel.cron.wrapper_constants import unwrap_command

# matches KEY=value or KEY = value (cron does NOT support inline comments after =)
_ENV_VAR_RE = re.compile(r"^[A-Z_][A-Z0-9_]*\s*=")
# @hourly, @daily, @weekly, @monthly, @yearly, @annually, @midnight, @reboot
_NICKNAMES = frozenset(
    {"@hourly", "@daily", "@weekly", "@monthly", "@yearly", "@annually", "@midnight", "@reboot"}
)


def parse_one_line(*, line: str, source_kind: CronSourceKind) -> tuple[str, str] | None:
    """Parse a single raw crontab line → (schedule, command), or None if the
    line is blank / a comment / an env-var / unparseable.

    The command is UNWRAPPED if it is a wrapper invocation.
    Returns None for skip-lines (blank, comment, env-var) and on parse errors.
    Does NOT validate the schedule via croniter (caller handles that).
    """
    if not line or line.startswith("#"):
        return None
    if _ENV_VAR_RE.match(line):
        return None

    try:
        if line.startswith("@"):
            schedule, command = _parse_nickname_line(line=line, source_kind=source_kind)
        else:
            schedule, command = _parse_fielded_line(line=line, source_kind=source_kind)
    except ValueError:
        return None

    # Unwrap the command if it's a wrapper invocation
    command = unwrap_command(command)
    return schedule, command


def parse_cron_file(
    *,
    content: str,
    source_kind: CronSourceKind,
    host: str,
    host_source_path: str,
) -> tuple[list[ParsedCronEntry], list[CronScanError]]:
    """Parse a crontab-format file's text content.

    Args:
        content: File content as a string (caller already decoded UTF-8 with
            errors="replace" to avoid scan failure on a corrupt byte).
        source_kind: Tells the parser whether lines have a USER field.
        host: Logical host name (used for fingerprint identity).
        host_source_path: HOST path stored on the ParsedCronEntry (e.g.
            ``/etc/cron.d/certbot``, ``crontab:alice``).

    Returns:
        (entries, errors). `errors` is per-line parse errors. Caller decides
        partial-flag semantics.
    """
    entries: list[ParsedCronEntry] = []
    errors: list[CronScanError] = []

    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        # Skip blank lines, comments, and env vars
        if not line or line.startswith("#"):
            continue
        if _ENV_VAR_RE.match(line):
            continue

        # Parse the line — report errors rather than silently dropping them
        try:
            if line.startswith("@"):
                schedule, command = _parse_nickname_line(line=line, source_kind=source_kind)
            else:
                schedule, command = _parse_fielded_line(line=line, source_kind=source_kind)
        except ValueError as exc:
            errors.append(CronScanError(host_source_path=host_source_path, error=str(exc)))
            continue

        # Unwrap the command if it is a wrapper invocation
        command = unwrap_command(command)

        # Validate schedule via croniter (skip for @reboot)
        if schedule != "@reboot" and not croniter.is_valid(schedule):
            errors.append(
                CronScanError(
                    host_source_path=host_source_path,
                    error=f"invalid schedule: {schedule!r}",
                )
            )
            continue

        entries.append(
            ParsedCronEntry(
                host=host,
                host_source_path=host_source_path,
                schedule=schedule,
                command=command,
            )
        )

    return entries, errors


def _parse_nickname_line(*, line: str, source_kind: CronSourceKind) -> tuple[str, str]:
    """Parse a `@nickname [user] CMD` line.

    SYSTEM_WITH_USER_FIELD: the user field (if present) is skipped; only the
    nickname and command are extracted. USER_CRONTAB: the command is extracted.
    """
    parts = line.split(maxsplit=2)
    if len(parts) < 2:  # noqa: PLR2004 -- "@nickname X" minimum
        msg = f"malformed nickname line (no command): {line!r}"
        raise ValueError(msg)
    nickname = parts[0]
    if nickname not in _NICKNAMES:
        msg = f"unknown cron nickname: {nickname!r}"
        raise ValueError(msg)

    if source_kind == CronSourceKind.USER_CRONTAB:
        # @nickname CMD...
        command = " ".join(parts[1:])
        return nickname, command

    # SYSTEM_WITH_USER_FIELD: may have optional user field.
    # If 3 parts: @nickname USER CMD
    # If 2 parts: @nickname CMD (no user field, falls back to root)
    command = parts[2] if len(parts) == 3 else parts[1]  # noqa: PLR2004
    return nickname, command


def _parse_fielded_line(*, line: str, source_kind: CronSourceKind) -> tuple[str, str]:
    """Parse a 5-field schedule line.

    USER_CRONTAB:               `m h dom mon dow CMD...`         → 6 tokens minimum
    SYSTEM_WITH_USER_FIELD:     `m h dom mon dow USER CMD...`    → 7 tokens (user skipped)
    """
    parts = line.split(maxsplit=6 if source_kind == CronSourceKind.SYSTEM_WITH_USER_FIELD else 5)
    min_tokens = 7 if source_kind == CronSourceKind.SYSTEM_WITH_USER_FIELD else 6
    if len(parts) < min_tokens:
        msg = f"malformed line (expected {min_tokens} tokens, got {len(parts)}): {line!r}"
        raise ValueError(msg)
    schedule = " ".join(parts[:5])
    command = parts[6] if source_kind == CronSourceKind.SYSTEM_WITH_USER_FIELD else parts[5]
    return schedule, command


__all__ = ["parse_cron_file", "parse_one_line"]
