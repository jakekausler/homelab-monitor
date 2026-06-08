"""Pure schedule evaluator for the expected-silence allowlist (STAGE-004-038).

No I/O, no DB. ``is_silence_allowed`` answers: "given THIS allowlist entry's
schedule, is the candidate signature EXPECTED to be silent right now?" True =>
expected-silent (suppress the alert). False => not currently in an expected-silence
window (the silent gauge may emit).

Schedule kinds:
- ``always``  : permanent exemption -> always True.
- ``window``  : schedule_value = "<start-iso>/<end-iso>"; True iff start <= now <= end.
- ``cron``    : "this signature only emits around its scheduled runs; between-runs
                silence is EXPECTED". True EXCEPT within ``cron_grace_seconds`` after
                the previous cron fire (the post-fire ACTIVE window, when the job
                SHOULD have logged). seconds_since_prev_fire > grace -> True.

All datetimes are tz-aware UTC. now MUST be tz-aware.
"""

from __future__ import annotations

from datetime import UTC, datetime

from croniter import croniter

from homelab_monitor.kernel.cron.schedule import InvalidCronExpression

DEFAULT_CRON_GRACE_SECONDS = 900


def is_silence_allowed(
    schedule_kind: str,
    schedule_value: str,
    now: datetime,
    *,
    cron_grace_seconds: int = DEFAULT_CRON_GRACE_SECONDS,
) -> bool:
    """Return True when the entry's schedule says silence is EXPECTED at ``now``.

    Raises:
        ValueError: on unknown schedule_kind or malformed schedule_value.
    """
    if now.tzinfo is None:
        msg = "now must be tz-aware"
        raise ValueError(msg)
    if schedule_kind == "always":
        return True
    if schedule_kind == "window":
        return _window_allows(schedule_value, now)
    if schedule_kind == "cron":
        return _cron_allows(schedule_value, now, cron_grace_seconds)
    msg = f"unknown schedule_kind: {schedule_kind!r}"
    raise ValueError(msg)


def _window_allows(schedule_value: str, now: datetime) -> bool:
    """Parse '<start-iso>/<end-iso>' and test membership."""
    parts = schedule_value.split("/")
    if len(parts) != 2:  # noqa: PLR2004
        msg = f"window schedule_value must be '<start-iso>/<end-iso>', got {schedule_value!r}"
        raise ValueError(msg)
    start_raw, end_raw = parts
    try:
        start = datetime.fromisoformat(start_raw)
        end = datetime.fromisoformat(end_raw)
    except ValueError as exc:
        msg = f"window schedule_value has invalid ISO datetime: {schedule_value!r} ({exc})"
        raise ValueError(msg) from exc
    # Normalize to tz-aware UTC for comparison (naive -> assume UTC).
    start = start.replace(tzinfo=UTC) if start.tzinfo is None else start.astimezone(UTC)
    end = end.replace(tzinfo=UTC) if end.tzinfo is None else end.astimezone(UTC)
    if end < start:
        msg = f"window end before start: {schedule_value!r}"
        raise ValueError(msg)
    now_utc = now.astimezone(UTC)
    return start <= now_utc <= end


def _cron_allows(schedule_value: str, now: datetime, cron_grace_seconds: int) -> bool:
    """Allowed (expected-silent) EXCEPT within cron_grace_seconds after the prev fire."""
    if not croniter.is_valid(schedule_value):
        msg = f"invalid cron expression: {schedule_value!r}"
        raise InvalidCronExpression(msg)
    now_utc = now.astimezone(UTC)
    itr = croniter(schedule_value, now_utc)
    prev_fire: datetime = itr.get_prev(datetime)
    if prev_fire.tzinfo is None:  # pragma: no cover -- croniter preserves base tz
        prev_fire = prev_fire.replace(tzinfo=UTC)
    seconds_since_prev = (now_utc - prev_fire).total_seconds()
    return seconds_since_prev > cron_grace_seconds


__all__ = ["DEFAULT_CRON_GRACE_SECONDS", "is_silence_allowed"]
