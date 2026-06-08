"""Unit tests for the pure silence-schedule evaluator (STAGE-004-038)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from homelab_monitor.kernel.cron.schedule import InvalidCronExpression
from homelab_monitor.kernel.logs.silence_schedule import is_silence_allowed


def test_always_returns_true() -> None:
    now = datetime(2026, 6, 7, 12, 0, tzinfo=UTC)
    assert is_silence_allowed("always", "", now) is True


def test_naive_now_raises() -> None:
    with pytest.raises(ValueError, match="tz-aware"):
        is_silence_allowed("always", "", datetime(2026, 6, 7, 12, 0))


def test_unknown_kind_raises() -> None:
    now = datetime(2026, 6, 7, 12, 0, tzinfo=UTC)
    with pytest.raises(ValueError, match="unknown schedule_kind"):
        is_silence_allowed("bogus", "", now)


def test_window_inside_returns_true() -> None:
    now = datetime(2026, 6, 7, 12, 0, tzinfo=UTC)
    val = "2026-06-07T00:00:00+00:00/2026-06-08T00:00:00+00:00"
    assert is_silence_allowed("window", val, now) is True


def test_window_outside_returns_false() -> None:
    now = datetime(2026, 6, 9, 12, 0, tzinfo=UTC)
    val = "2026-06-07T00:00:00+00:00/2026-06-08T00:00:00+00:00"
    assert is_silence_allowed("window", val, now) is False


def test_window_malformed_no_slash_raises() -> None:
    now = datetime(2026, 6, 7, 12, 0, tzinfo=UTC)
    with pytest.raises(ValueError, match="<start-iso>/<end-iso>"):
        is_silence_allowed("window", "2026-06-07T00:00:00+00:00", now)


def test_window_bad_iso_raises() -> None:
    now = datetime(2026, 6, 7, 12, 0, tzinfo=UTC)
    with pytest.raises(ValueError, match="invalid ISO"):
        is_silence_allowed("window", "not-a-date/also-bad", now)


def test_window_end_before_start_raises() -> None:
    now = datetime(2026, 6, 7, 12, 0, tzinfo=UTC)
    val = "2026-06-08T00:00:00+00:00/2026-06-07T00:00:00+00:00"
    with pytest.raises(ValueError, match="end before start"):
        is_silence_allowed("window", val, now)


def test_window_naive_iso_treated_as_utc() -> None:
    now = datetime(2026, 6, 7, 12, 0, tzinfo=UTC)
    val = "2026-06-07T00:00:00/2026-06-08T00:00:00"  # naive -> assumed UTC
    assert is_silence_allowed("window", val, now) is True


def test_cron_within_grace_returns_false() -> None:
    # Cron fires hourly at :00. now = 12:05 -> 300s since prev fire (12:00) < 900s grace -> False.
    now = datetime(2026, 6, 7, 12, 5, tzinfo=UTC)
    assert is_silence_allowed("cron", "0 * * * *", now, cron_grace_seconds=900) is False


def test_cron_outside_grace_returns_true() -> None:
    # now = 12:30 -> 1800s since prev fire (12:00) > 900s grace -> True (silence expected).
    now = datetime(2026, 6, 7, 12, 30, tzinfo=UTC)
    assert is_silence_allowed("cron", "0 * * * *", now, cron_grace_seconds=900) is True


def test_cron_invalid_raises() -> None:
    now = datetime(2026, 6, 7, 12, 0, tzinfo=UTC)
    with pytest.raises(InvalidCronExpression):
        is_silence_allowed("cron", "not a cron", now)


__all__: list[str] = []
