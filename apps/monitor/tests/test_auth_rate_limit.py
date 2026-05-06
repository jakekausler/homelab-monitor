"""Tests for kernel/auth/rate_limit.py — login rate limiting."""

from __future__ import annotations

import time

from homelab_monitor.kernel.auth.rate_limit import InProcessLoginRateLimiter


def test_rate_limit_5_attempts_allowed() -> None:
    """5 login attempts from same IP allowed; 6th denied."""
    limiter = InProcessLoginRateLimiter()
    ip = "192.168.1.1"
    for i in range(5):
        result = limiter.check_and_record(ip)
        assert result is True, f"Attempt {i + 1} should be allowed"
    # 6th should be denied
    result = limiter.check_and_record(ip)
    assert result is False


def test_rate_limit_different_ips_independent() -> None:
    """Two different IPs have independent rate limits."""
    limiter = InProcessLoginRateLimiter()
    ip1 = "192.168.1.1"
    ip2 = "192.168.1.2"

    # Fill up ip1
    for _ in range(5):
        limiter.check_and_record(ip1)

    # ip1 should be blocked, ip2 should work
    assert not limiter.check_and_record(ip1)
    assert limiter.check_and_record(ip2)


def test_rate_limit_window_expiry_allows_retry() -> None:
    """After window expires, IP is allowed again."""
    # Create limiter with short window (1 second)
    limiter = InProcessLoginRateLimiter(max_attempts=1, window_seconds=1)
    ip = "192.168.1.1"

    # First attempt allowed
    assert limiter.check_and_record(ip)
    # Second attempt blocked
    assert not limiter.check_and_record(ip)

    # Wait for window to expire
    time.sleep(1.1)
    # Should be allowed again
    assert limiter.check_and_record(ip)


def test_rate_limit_custom_params() -> None:
    """Custom max_attempts and window_seconds respected."""
    limiter = InProcessLoginRateLimiter(max_attempts=2, window_seconds=10)
    ip = "192.168.1.1"

    # 2 attempts allowed
    assert limiter.check_and_record(ip)
    assert limiter.check_and_record(ip)
    # 3rd denied
    assert not limiter.check_and_record(ip)


def test_rate_limiter_protocol_satisfied() -> None:
    """InProcessLoginRateLimiter satisfies LoginRateLimiter protocol."""
    limiter = InProcessLoginRateLimiter()
    # Just verify it's callable and has the right signature
    assert callable(limiter.check_and_record)
    result = limiter.check_and_record("test_ip")
    assert isinstance(result, bool)
