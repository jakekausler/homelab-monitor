"""Pure cron-schedule helpers built on croniter.

No I/O, no DB. Used by:
- the migration 0007 backfill (re-implemented locally to avoid kernel-import
  ordering issues during alembic upgrade)
- ``CronRepo.update_cron`` to canonicalize the
  ``schedule`` field on every write
- ``GET /api/crons/{id}/preview-runs`` and ``GET /api/crons/preview-runs``
  to materialize the next N expected fire times for the UI's schedule
  preview widget

All returned timestamps are ISO-8601 UTC with timezone offset (e.g.
``2026-05-11T13:45:00+00:00``). Timezone handling is defined as: croniter
operates in UTC unless given a tz-aware base. Per spec §16, all internal
timestamps are UTC; display layer (UI) converts to local TZ on render.
"""

from __future__ import annotations

from datetime import UTC, datetime

from croniter import CroniterBadCronError, croniter


class InvalidCronExpression(ValueError):
    """Raised when a cron expression cannot be parsed by croniter.

    Subclasses ``ValueError`` so Pydantic field_validator callers can
    surface the message in 422 responses without extra wrapping.
    """


def canonicalize_schedule(expr: str) -> str:
    """Validate and return the canonical 5-field form of ``expr``.

    Examples::

        canonicalize_schedule("@hourly")     # -> "0 * * * *"
        canonicalize_schedule("*/5 * * * *") # -> "*/5 * * * *"
        canonicalize_schedule("17 4 * * 1-5")# -> "17 4 * * 1-5"

    Raises:
        InvalidCronExpression: if croniter rejects the expression.
    """
    if not expr.strip():
        msg = "schedule must be a non-empty string"
        raise InvalidCronExpression(msg)
    if not croniter.is_valid(expr):
        msg = f"invalid cron expression: {expr!r}"
        raise InvalidCronExpression(msg)
    try:
        iterator = croniter(expr)
    except CroniterBadCronError as exc:  # pragma: no cover -- is_valid guarded above
        msg = f"invalid cron expression: {expr!r} ({exc})"
        raise InvalidCronExpression(msg) from exc
    # croniter.expressions is a list of 5 (or 6 with seconds) strings — the
    # canonical normalized fields after parsing aliases like @hourly.
    return " ".join(iterator.expressions)


def compute_next_runs(expr: str, *, count: int = 3, base: datetime | None = None) -> list[str]:
    """Return the next ``count`` ISO-8601 UTC fire times after ``base``.

    Args:
        expr: A cron expression accepted by croniter.
        count: How many future fire times to return (1..10 enforced by callers).
        base: Anchor datetime; defaults to ``datetime.now(tz=UTC)``. Must be
            tz-aware if supplied — naive datetimes raise ValueError.

    Returns:
        A list of ``count`` ISO-8601 UTC timestamps with offset.

    Raises:
        InvalidCronExpression: if ``expr`` is not a valid cron expression.
        ValueError: if ``base`` is naive (no tzinfo).
    """
    if not croniter.is_valid(expr):
        msg = f"invalid cron expression: {expr!r}"
        raise InvalidCronExpression(msg)
    if base is None:
        base = datetime.now(tz=UTC)
    elif base.tzinfo is None:
        msg = "base datetime must be tz-aware"
        raise ValueError(msg)
    iterator = croniter(expr, base)
    out: list[str] = []
    for _ in range(count):
        nxt = iterator.get_next(datetime)
        # croniter returns a datetime in the base's tz; ensure UTC for storage.
        if nxt.tzinfo is None:  # pragma: no cover -- croniter preserves base tz
            nxt = nxt.replace(tzinfo=UTC)
        out.append(nxt.astimezone(UTC).isoformat())
    return out


def compute_average_interval_seconds(expr: str, *, base: datetime | None = None) -> int:
    """Return an integer-seconds estimate of the average interval between
    consecutive fires of ``expr``.

    Used by ``CronRepo.update_cron`` to mirror a cron
    schedule into the ``cadence_seconds`` column so callers reading
    cadence directly get a sensible fast-lookup value without re-parsing.

    Approach: ask croniter for the next 11 fire times after ``base`` and
    average the 10 deltas. Handles non-uniform schedules better than
    ``next - prev``.

    Raises:
        InvalidCronExpression: if ``expr`` is not a valid cron expression.
    """
    if not croniter.is_valid(expr):
        msg = f"invalid cron expression: {expr!r}"
        raise InvalidCronExpression(msg)
    if base is None:
        base = datetime.now(tz=UTC)
    iterator = croniter(expr, base)
    samples: list[datetime] = [iterator.get_next(datetime) for _ in range(11)]
    deltas = [(samples[i + 1] - samples[i]).total_seconds() for i in range(len(samples) - 1)]
    avg = sum(deltas) / len(deltas)
    return max(1, round(avg))
