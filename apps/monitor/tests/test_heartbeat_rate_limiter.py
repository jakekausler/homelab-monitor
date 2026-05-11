"""Unit tests for CronRateLimiter (no I/O, fake clock)."""

from __future__ import annotations

import pytest

from homelab_monitor.kernel.heartbeat.rate_limiter import CronRateLimiter


class _FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_first_request_per_cron_succeeds() -> None:
    limiter = CronRateLimiter(capacity=5, refill_per_second=1.0)
    allowed, retry_after = limiter.try_acquire("c1")
    assert allowed is True
    assert retry_after == 0.0


def test_burst_up_to_capacity_succeeds() -> None:
    limiter = CronRateLimiter(capacity=3, refill_per_second=1.0, clock=_FakeClock())
    for _ in range(3):
        allowed, _ = limiter.try_acquire("c1")
        assert allowed is True


def test_request_over_capacity_returns_false_with_retry_after() -> None:
    clock = _FakeClock()
    limiter = CronRateLimiter(capacity=2, refill_per_second=1.0, clock=clock)
    assert limiter.try_acquire("c1")[0] is True
    assert limiter.try_acquire("c1")[0] is True
    allowed, retry_after = limiter.try_acquire("c1")
    assert allowed is False
    assert retry_after > 0


def test_tokens_refill_over_time() -> None:
    clock = _FakeClock()
    limiter = CronRateLimiter(capacity=1, refill_per_second=2.0, clock=clock)
    # Consume the only token.
    assert limiter.try_acquire("c1")[0] is True
    # Immediately try again -> denied.
    assert limiter.try_acquire("c1")[0] is False
    # Advance enough to refill 1 token (1 / 2.0 = 0.5 s).
    clock.advance(0.5)
    assert limiter.try_acquire("c1")[0] is True


def test_buckets_are_isolated_per_cron_id() -> None:
    clock = _FakeClock()
    limiter = CronRateLimiter(capacity=1, refill_per_second=1.0, clock=clock)
    assert limiter.try_acquire("alpha")[0] is True
    assert limiter.try_acquire("alpha")[0] is False
    # 'beta' starts with a fresh bucket.
    assert limiter.try_acquire("beta")[0] is True


def test_retry_after_decreases_to_zero_as_time_passes() -> None:
    clock = _FakeClock()
    limiter = CronRateLimiter(capacity=1, refill_per_second=1.0, clock=clock)
    assert limiter.try_acquire("c1")[0] is True
    _, retry_first = limiter.try_acquire("c1")
    assert retry_first == pytest.approx(1.0, rel=0.01)  # pyright: ignore[reportUnknownMemberType]
    clock.advance(0.5)
    _, retry_after_half = limiter.try_acquire("c1")
    assert retry_after_half == pytest.approx(0.5, rel=0.05)  # pyright: ignore[reportUnknownMemberType]


def test_constructor_rejects_zero_capacity() -> None:
    with pytest.raises(ValueError, match="capacity"):
        CronRateLimiter(capacity=0)


def test_constructor_rejects_zero_refill_rate() -> None:
    with pytest.raises(ValueError, match="refill_per_second"):
        CronRateLimiter(refill_per_second=0)


def test_reset_wipes_all_buckets() -> None:
    limiter = CronRateLimiter(capacity=1, refill_per_second=1.0, clock=_FakeClock())
    assert limiter.try_acquire("c1")[0] is True
    assert limiter.try_acquire("c1")[0] is False
    limiter.reset()
    assert limiter.try_acquire("c1")[0] is True
