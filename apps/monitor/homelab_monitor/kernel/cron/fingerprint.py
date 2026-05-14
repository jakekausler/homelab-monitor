"""Deterministic fingerprint + name-derivation helpers for the cron registry.

The fingerprint is the content-addressable identity of a cron row:
``SHA256(json_canonical({host, source_path, schedule, command}))``. Same
inputs → same fingerprint, regardless of caller dict ordering, Python's
hash randomization, or arbitrary Unicode in field values.

F19 precedent: cf. ``alerts.fingerprinting.quarantine_fingerprint``. Both
use JSON serialization with ``sort_keys=True`` + ``separators=(",", ":")``
so a field value containing a delimiter cannot collide with a different
(field, value) tuple. The cron variant additionally uses
``ensure_ascii=False`` so Unicode command paths hash to the same bytes as
their source text (instead of escaping to ASCII first, which would lose
information visible to the wrapper installer).

NULL ``source_path`` serializes as JSON ``null``, distinct from the empty
string ``""`` (per D2+D4 design interaction). Wrapper installers MUST send
``null`` for remote-only crons; sending ``""`` produces a different
fingerprint and breaks convergence with discovery.

``derive_name`` produces a default cron name from the command. The default
is intentionally minimal — interpreter-prefixed commands (e.g.,
``python3 /opt/sync.py``) will name to ``python3``; the user is expected to
edit the field on the detail page. We chose this over an interpreter-list
heuristic because the heuristic introduces maintenance burden for a
field that the user is going to edit anyway (D3).
"""

from __future__ import annotations

import hashlib
import json


def compute_fingerprint(host: str, source_path: str | None, schedule: str, command: str) -> str:
    """Return the SHA256 hex fingerprint for a cron's identity tuple.

    Args:
        host: Logical host name (e.g., ``"homelab-host"``).
        source_path: Disk source (``/etc/crontab``, ``/etc/cron.d/foo``,
            ``crontab:<user>``), or ``None`` for remote-only crons.
        schedule: Cron expression (raw, not canonical). For cadence-only
            rows, pass ``""``.
        command: Cron command string, as it appears on disk.

    Returns:
        64-character lowercase hex string.
    """
    payload = json.dumps(
        {
            "host": host,
            "source_path": source_path,
            "schedule": schedule,
            "command": command,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


# Shell wrappers, operators, and builtins commonly used in cron commands
# before the actual identifying binary/script. Skipped during name derivation
# so that e.g. `test -x /usr/sbin/anacron || run-parts ...` derives `anacron`
# instead of `test`.
_SHELL_WRAPPERS: frozenset[str] = frozenset(
    {
        "bash",
        "sh",
        "zsh",
        "ksh",
        "dash",
        "fish",
        "cd",
        "test",
        "[",
        "[[",
        "command",
        "if",
        "then",
        "else",
        "elif",
        "fi",
        "nohup",
        "sudo",
        "env",
        "exec",
        "time",
        "&&",
        "||",
        ";",
        "|",
        "&",
    }
)


def derive_name(command: str) -> str:
    """Return the default ``name`` value for a cron with the given command.

    Algorithm: scan whitespace-delimited tokens left-to-right; skip shell
    wrappers (`bash`, `test`, `cd`, `[`, `command`, etc.), operators
    (`&&`, `||`), and flag-like tokens (starting with `-`). Return the
    basename of the first remaining token. This handles conditional guards
    like `test -x /usr/bin/foo && /usr/bin/foo` (returns `foo`) and shell
    wrappers like `bash /opt/script.sh` (returns `script.sh`). Empty or
    all-skipped commands return ``"cron"``.
    """
    if not command.strip():
        return "cron"

    for token in command.split():
        candidate = token.rsplit("/", 1)[-1]
        if candidate in _SHELL_WRAPPERS:
            continue
        if candidate.startswith("-"):
            continue
        if "=" in candidate:  # skip env-var assignments like FOO=bar
            continue
        if not candidate or candidate in (".", ".."):
            continue
        return candidate

    return "cron"


__all__ = ["compute_fingerprint", "derive_name"]
