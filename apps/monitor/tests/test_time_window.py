"""Unit tests for logs.time_window.parse_and_validate_window (STAGE-004-008)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from homelab_monitor.kernel.api.errors import HttpProblem
from homelab_monitor.kernel.logs.time_window import (
    FUTURE_SKEW_GRACE,
    MAX_RANGE_DAYS,
    parse_and_validate_window,
)

HTTP_BAD_REQUEST = 400


def test_valid_window_returns_strings_unchanged() -> None:
    start = "2026-05-01T00:00:00+00:00"
    end = "2026-05-02T00:00:00+00:00"
    assert parse_and_validate_window(start, end) == (start, end)


def test_naive_datetimes_are_accepted() -> None:
    # naive inputs are UTC-normalized internally; returned strings unchanged.
    start = "2026-05-01T00:00:00"
    end = "2026-05-01T01:00:00"
    assert parse_and_validate_window(start, end) == (start, end)


def test_bad_iso_raises_invalid_time_format() -> None:
    with pytest.raises(HttpProblem) as exc:
        parse_and_validate_window("nope", "2026-05-02T00:00:00+00:00")
    assert exc.value.status_code == HTTP_BAD_REQUEST
    assert exc.value.code == "invalid_time_format"


def test_start_equal_end_raises_invalid_range() -> None:
    same = "2026-05-01T00:00:00+00:00"
    with pytest.raises(HttpProblem) as exc:
        parse_and_validate_window(same, same)
    assert exc.value.code == "invalid_range"


def test_start_after_end_raises_invalid_range() -> None:
    with pytest.raises(HttpProblem) as exc:
        parse_and_validate_window("2026-05-02T00:00:00+00:00", "2026-05-01T00:00:00+00:00")
    assert exc.value.code == "invalid_range"


def test_span_over_max_raises_range_too_wide() -> None:
    with pytest.raises(HttpProblem) as exc:
        parse_and_validate_window("2026-01-01T00:00:00+00:00", "2026-03-01T00:00:00+00:00")
    assert exc.value.code == "range_too_wide"


def test_span_exactly_max_is_allowed() -> None:
    # Exactly 30 days is allowed (boundary: > MAX, not >=).
    start = "2026-01-01T00:00:00+00:00"
    end = f"2026-01-{1 + MAX_RANGE_DAYS:02d}T00:00:00+00:00"
    assert parse_and_validate_window(start, end) == (start, end)


def _now_anchor() -> datetime:
    """Fixed UTC instant used as the 'now' anchor in future-date tests."""
    return datetime(2026, 5, 30, 12, 0, 0, tzinfo=UTC)


def test_future_end_raises_range_in_future() -> None:
    anchor = _now_anchor()
    start = "2026-05-30T10:00:00+00:00"
    end = "2026-05-30T13:00:00+00:00"  # 1 h after anchor
    with pytest.raises(HttpProblem) as exc:
        parse_and_validate_window(start, end, now=anchor)
    assert exc.value.status_code == HTTP_BAD_REQUEST
    assert exc.value.code == "range_in_future"


def test_future_start_raises_range_in_future() -> None:
    anchor = _now_anchor()
    start = "2026-05-30T13:00:00+00:00"
    end = "2026-05-30T14:00:00+00:00"
    with pytest.raises(HttpProblem) as exc:
        parse_and_validate_window(start, end, now=anchor)
    assert exc.value.status_code == HTTP_BAD_REQUEST
    assert exc.value.code == "range_in_future"


def test_end_equal_to_now_is_allowed() -> None:
    anchor = _now_anchor()
    start = "2026-05-30T11:00:00+00:00"
    end = "2026-05-30T12:00:00+00:00"  # == anchor
    assert parse_and_validate_window(start, end, now=anchor) == (start, end)


def test_end_one_second_before_now_is_allowed() -> None:
    anchor = _now_anchor()
    start = "2026-05-30T10:00:00+00:00"
    end = "2026-05-30T11:59:59+00:00"
    assert parse_and_validate_window(start, end, now=anchor) == (start, end)


def test_future_check_fires_after_ordering_check() -> None:
    anchor = _now_anchor()
    future_start = "2026-05-30T14:00:00+00:00"
    future_end = "2026-05-30T13:00:00+00:00"  # end < start AND both future
    with pytest.raises(HttpProblem) as exc:
        parse_and_validate_window(future_start, future_end, now=anchor)
    assert exc.value.code == "invalid_range"  # ordering fires first


def test_grace_constant_is_five_seconds() -> None:
    # Pin the contract: the clock-skew grace is 5 seconds.
    assert timedelta(seconds=5) == FUTURE_SKEW_GRACE


def test_end_within_skew_grace_is_allowed() -> None:
    # Browser clock leads the server by a few seconds: end slightly in the
    # future (within the 5s grace) must be accepted.
    anchor = _now_anchor()
    start = "2026-05-30T11:00:00+00:00"
    end = "2026-05-30T12:00:03+00:00"  # 3 s after anchor (within grace)
    assert parse_and_validate_window(start, end, now=anchor) == (start, end)


def test_end_beyond_skew_grace_raises_range_in_future() -> None:
    anchor = _now_anchor()
    start = "2026-05-30T11:00:00+00:00"
    end = "2026-05-30T12:00:10+00:00"  # 10 s after anchor (beyond 5s grace)
    with pytest.raises(HttpProblem) as exc:
        parse_and_validate_window(start, end, now=anchor)
    assert exc.value.status_code == HTTP_BAD_REQUEST
    assert exc.value.code == "range_in_future"


def test_start_beyond_skew_grace_raises_range_in_future() -> None:
    anchor = _now_anchor()
    start = "2026-05-30T12:00:10+00:00"  # 10 s after anchor (beyond grace)
    end = "2026-05-30T12:00:20+00:00"
    with pytest.raises(HttpProblem) as exc:
        parse_and_validate_window(start, end, now=anchor)
    assert exc.value.status_code == HTTP_BAD_REQUEST
    assert exc.value.code == "range_in_future"
