"""Unit tests for the croniter-backed schedule helpers."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from homelab_monitor.kernel.cron.schedule import (
    InvalidCronExpression,
    canonicalize_schedule,
    compute_average_interval_seconds,
    compute_next_runs,
)


def test_canonicalize_at_hourly() -> None:
    assert canonicalize_schedule("@hourly") == "0 * * * *"


def test_canonicalize_at_daily() -> None:
    assert canonicalize_schedule("@daily") == "0 0 * * *"


def test_canonicalize_keeps_5_field_form() -> None:
    assert canonicalize_schedule("*/5 * * * *") == "*/5 * * * *"


def test_canonicalize_invalid_raises() -> None:
    with pytest.raises(InvalidCronExpression):
        canonicalize_schedule("garbage cron")


def test_canonicalize_empty_raises() -> None:
    with pytest.raises(InvalidCronExpression):
        canonicalize_schedule("")


def test_compute_next_runs_basic() -> None:
    runs = compute_next_runs("0 * * * *", count=3)
    assert len(runs) == 3  # noqa: PLR2004
    # All ISO with +00:00
    for r in runs:
        assert r.endswith("+00:00")


def test_compute_next_runs_with_base_time() -> None:
    base = datetime(2026, 5, 11, 12, 0, 0, tzinfo=UTC)
    runs = compute_next_runs("0 * * * *", count=2, base=base)
    assert runs == [
        "2026-05-11T13:00:00+00:00",
        "2026-05-11T14:00:00+00:00",
    ]


def test_compute_next_runs_invalid_raises() -> None:
    with pytest.raises(InvalidCronExpression):
        compute_next_runs("garbage", count=1)


def test_compute_next_runs_naive_base_raises() -> None:
    naive = datetime(2026, 5, 11, 12, 0, 0)
    with pytest.raises(ValueError, match="tz-aware"):
        compute_next_runs("* * * * *", count=1, base=naive)


def test_compute_average_interval_seconds_for_every_minute() -> None:
    """`* * * * *` fires every 60 seconds."""
    assert compute_average_interval_seconds("* * * * *") == 60  # noqa: PLR2004


def test_compute_average_interval_seconds_for_every_5_minutes() -> None:
    """`*/5 * * * *` fires every 300 seconds."""
    assert compute_average_interval_seconds("*/5 * * * *") == 300  # noqa: PLR2004


def test_compute_average_interval_seconds_for_hourly() -> None:
    """`0 * * * *` fires every 3600 seconds."""
    assert compute_average_interval_seconds("0 * * * *") == 3600  # noqa: PLR2004


def test_compute_average_interval_seconds_invalid_raises() -> None:
    """Invalid cron expression raises InvalidCronExpression."""
    with pytest.raises(InvalidCronExpression):
        compute_average_interval_seconds("not a cron expression")


def test_compute_average_interval_with_explicit_base() -> None:
    """Explicit base parameter (covers schedule.py:114->116 branch)."""
    base = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    result = compute_average_interval_seconds("* * * * *", base=base)
    assert result == 60  # noqa: PLR2004
