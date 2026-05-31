"""Shared ISO-8601 time-window parsing + validation for log endpoints.

Extracted from logs.py (/api/logs/query) so the docker logs endpoint can
reuse identical validation. The raised HttpProblem shapes are part of the
public API contract — do NOT change status_code/code/message here without
updating both consumers and their tests.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from homelab_monitor.kernel.api.errors import HttpProblem

# Max custom range width. Mirrors /api/logs/query (_MAX_RANGE_DAYS).
MAX_RANGE_DAYS = 30


def parse_and_validate_window(
    start: str, end: str, *, now: datetime | None = None
) -> tuple[str, str]:
    """Parse + validate an ISO-8601 [start, end] window.

    Rules (identical to /api/logs/query, STAGE-004-008 extraction):
      - both must be ISO-8601 parseable (datetime.fromisoformat).
      - naive datetimes are UTC-normalized.
      - start must be strictly before end.
      - neither start nor end may be in the future.
      - (end - start) must not exceed MAX_RANGE_DAYS (30 days).

    ``now`` is injectable for deterministic testing; defaults to
    ``datetime.now(UTC)``. Callers that omit it get the real wall clock.

    Returns the ORIGINAL start/end strings unchanged (paginate_older consumes
    the raw ISO strings, matching the existing call site). Raises HttpProblem
    on any violation.
    """
    try:
        start_dt = datetime.fromisoformat(start)
        end_dt = datetime.fromisoformat(end)
    except ValueError as exc:
        raise HttpProblem(
            status_code=400,
            code="invalid_time_format",
            message="start and end must be ISO-8601 timestamps",
        ) from exc

    if start_dt.tzinfo is None:
        start_dt = start_dt.replace(tzinfo=UTC)
    if end_dt.tzinfo is None:
        end_dt = end_dt.replace(tzinfo=UTC)

    if start_dt >= end_dt:
        raise HttpProblem(
            status_code=400,
            code="invalid_range",
            message="end must be after start",
        )

    _now = now if now is not None else datetime.now(UTC)
    if start_dt > _now or end_dt > _now:
        raise HttpProblem(
            status_code=400,
            code="range_in_future",
            message="start and end cannot be in the future",
        )

    if (end_dt - start_dt) > timedelta(days=MAX_RANGE_DAYS):
        raise HttpProblem(
            status_code=400,
            code="range_too_wide",
            message=f"time range cannot exceed {MAX_RANGE_DAYS} days",
        )

    return start, end
