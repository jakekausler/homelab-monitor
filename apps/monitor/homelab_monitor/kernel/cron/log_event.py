"""Types + matching logic for B-mode cron log events (STAGE-002-008).

A ``CronLogEvent`` is the structured event that Vector's VRL transform produces
and POSTs to ``/api/internal/cron-events``. ``match_cron_event`` resolves an
event to candidate cron fingerprints by ``(host, log_match_key)``.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum


@dataclass(slots=True, frozen=True)
class CronLogEvent:
    """One structured cron log event from Vector.

    Attributes:
        host: hostname the cron ran on (Vector ``get_hostname!()``).
        command: raw command string as logged (NOT scrubbed, NOT canonical).
        user: the cron user (``root``, etc.); may be empty if unparseable.
        timestamp: UTC ISO-8601 string of the log entry.
        exit_code: the wrapper-tagged exit code, or ``None`` for a vanilla
            (bare-dispatch) cron line that carries no exit code.
        journal_cursor: the journald ``__CURSOR`` for idempotency. ``None`` only
            on the syslog-fallback path (see ``synthesize_cursor``).
    """

    host: str
    command: str
    user: str
    timestamp: str
    exit_code: int | None
    journal_cursor: str | None


class CronEventDisposition(StrEnum):
    """What the ingest endpoint decided to do with one event."""

    OBSERVED_RUN = "observed_run"  # bare line -> neutral record_observed_run
    STATE_OK = "state_ok"  # exit=0 -> record_ok
    STATE_FAIL = "state_fail"  # exit!=0 -> record_fail
    REPLAY_SKIPPED = "replay_skipped"  # cursor already processed
    NO_MATCH = "no_match"  # 0 cron rows matched
    AMBIGUOUS = "ambiguous"  # 2+ cron rows matched -> skip


def synthesize_cursor(event: CronLogEvent) -> str:
    """Return a deterministic content-derived dedup key for a cron event.

    This is the PRIMARY production dedup path, not a fallback. Although journald
    assigns every entry a real ``__CURSOR``, Vector's journald source consumes
    the cursor internally for checkpointing and does NOT emit it as event data
    (see the comment in deploy/vector/vector.toml.template), so
    ``event.journal_cursor`` is ``None`` for journald events too. The syslog
    file source likewise carries no cursor. In both cases the ingest endpoint
    falls through to this function.

    The key is derived from host + timestamp + command + exit_code. KNOWN
    LIMITATION: two distinct runs of the same command on the same host within
    the same timestamp resolution collide on this hash, and the second is
    dropped as a replay. Real ``__CURSOR`` values would not collide, but they
    are not available as event data here.
    """
    raw = f"{event.host}|{event.timestamp}|{event.command}|{event.exit_code}"
    return "syslog|" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


__all__ = ["CronEventDisposition", "CronLogEvent", "synthesize_cursor"]
