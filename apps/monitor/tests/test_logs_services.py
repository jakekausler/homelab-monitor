"""Tests for services discovery cache and composition."""

from __future__ import annotations

from homelab_monitor.kernel.api.routers.logs import (
    _compose_services_expr,  # pyright: ignore[reportPrivateUsage]
)
from homelab_monitor.kernel.api.schemas import LogsServicesResponse, ServiceCount
from homelab_monitor.kernel.logs.services import ServicesCache
from homelab_monitor.kernel.logs.victorialogs_client import logsql_quote_phrase


class TestComposServicesExpr:
    """Unit tests for _compose_services_expr composition logic."""

    def test_none_services_returns_expr_unchanged(self) -> None:
        """Absent services_csv returns expr unchanged."""
        result = _compose_services_expr("*", None)
        assert result == "*"

    def test_empty_services_returns_expr_unchanged(self) -> None:
        """Empty services_csv returns expr unchanged."""
        result = _compose_services_expr("*", "")
        assert result == "*"

    def test_whitespace_services_returns_expr_unchanged(self) -> None:
        """Whitespace-only services_csv returns expr unchanged."""
        result = _compose_services_expr("*", "  ,  ")
        assert result == "*"

    def test_single_service(self) -> None:
        """Single service is quoted and wrapped."""
        result = _compose_services_expr("foo", "svc")
        assert result == '(service:"svc") AND (foo)'

    def test_multiple_services(self) -> None:
        """Multiple services are OR'd together."""
        result = _compose_services_expr("foo", "a,b")
        assert result == '(service:"a" OR service:"b") AND (foo)'

    def test_escaping_backslash_and_quote(self) -> None:
        """Service values with backslash and quote are escaped correctly."""
        # Use logsql_quote_phrase to build the expected value
        quoted = logsql_quote_phrase('we"ird\\x')
        result = _compose_services_expr("foo", 'we"ird\\x')
        expected = f"({f'service:{quoted}'}) AND (foo)"
        assert result == expected
        # Verify the exact string
        assert result == '(service:"we\\"ird\\\\x") AND (foo)'

    def test_whitespace_trimming(self) -> None:
        """Leading/trailing whitespace in service values is trimmed."""
        result = _compose_services_expr("foo", " a , b ")
        assert result == '(service:"a" OR service:"b") AND (foo)'


class TestServicesCacheClockInjection:
    """Unit tests for ServicesCache with injectable clock."""

    def test_cache_hit_within_ttl(self) -> None:
        """Cache hit returns value when within TTL."""
        now = [0.0]
        cache = ServicesCache(ttl_seconds=30, clock=lambda: now[0])

        value = LogsServicesResponse(services=[], truncated=False)
        key = ("2026-01-01", "2026-01-02", 100)
        cache.put(key, value)

        # Still within TTL at now=0
        result = cache.get(key)
        assert result is value

    def test_cache_hit_near_ttl_expiry(self) -> None:
        """Cache hit just before TTL expiration."""
        now = [0.0]
        cache = ServicesCache(ttl_seconds=30, clock=lambda: now[0])

        value = LogsServicesResponse(services=[], truncated=False)
        key = ("2026-01-01", "2026-01-02", 100)
        cache.put(key, value)

        # Advance to 29.9s (still within TTL)
        now[0] = 29.9
        result = cache.get(key)
        assert result is value

    def test_cache_miss_after_ttl_expiry(self) -> None:
        """Cache miss returns None when TTL expires."""
        now = [0.0]
        cache = ServicesCache(ttl_seconds=30, clock=lambda: now[0])

        value = LogsServicesResponse(services=[], truncated=False)
        key = ("2026-01-01", "2026-01-02", 100)
        cache.put(key, value)

        # Advance past TTL
        now[0] = 30.0
        result = cache.get(key)
        assert result is None

        # Entry is evicted; second get also returns None
        result = cache.get(key)
        assert result is None

    def test_cache_miss_on_missing_key(self) -> None:
        """Cache miss on key that was never put."""
        cache = ServicesCache()
        key = ("2026-01-01", "2026-01-02", 100)
        result = cache.get(key)
        assert result is None

    def test_cache_different_keys_isolated(self) -> None:
        """Different keys are stored independently."""
        now = [0.0]
        cache = ServicesCache(ttl_seconds=30, clock=lambda: now[0])

        value1 = LogsServicesResponse(
            services=[ServiceCount(service="a", count=1)], truncated=False
        )
        value2 = LogsServicesResponse(
            services=[ServiceCount(service="b", count=2)], truncated=False
        )

        key1 = ("2026-01-01", "2026-01-02", 100)
        key2 = ("2026-01-03", "2026-01-04", 100)

        cache.put(key1, value1)
        cache.put(key2, value2)

        assert cache.get(key1) is value1
        assert cache.get(key2) is value2
