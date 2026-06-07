"""Tests for LogWindowFetcher (STAGE-004-031).

Fake VL client: _FakeVlClient — yields a configurable list of VlLogLine,
records call args, can raise VictoriaLogsClientError on demand.
Deterministic clocks: a mutable list-backed float for `clock`; a fixed
datetime for `wall_clock`.
asyncio_mode = "auto" (pyproject.toml) — no @pytest.mark.asyncio needed.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

from homelab_monitor.kernel.logs.log_window_fetcher import (
    LogWindowFetcher,
    _default_wall_clock,  # pyright: ignore[reportPrivateUsage]
)
from homelab_monitor.kernel.logs.victorialogs_client import (
    VictoriaLogsClientError,
    VlLogLine,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FIXED_WALL = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def _make_vl_line(n: int) -> VlLogLine:
    return VlLogLine(
        timestamp=f"2026-01-01T12:00:{n:02d}Z",
        message=f"msg {n}",
        stream="journal",
        fields={},
    )


def _line_at(second: int) -> VlLogLine:
    """A VL line whose timestamp encodes `second` (0..3599) past 12:00:00."""
    mm, ss = divmod(second, 60)
    return VlLogLine(
        timestamp=f"2026-01-01T{12 + mm // 60:02d}:{mm % 60:02d}:{ss:02d}Z",
        message=f"sec {second}",
        stream="journal",
        fields={},
    )


class _FakeVlClient:
    """Stand-in for VictoriaLogsClient exposing only stream_query.

    ``lines`` yielded in order; ``fail`` (if True) raises VictoriaLogsClientError
    before yielding any line.  Records every call's args.
    """

    def __init__(
        self,
        lines: list[VlLogLine],
        *,
        fail: bool = False,
    ) -> None:
        self._lines = lines
        self._fail = fail
        self.calls: list[dict[str, object]] = []

    async def stream_query(
        self, *, expr: str, start: str, end: str, limit: int
    ) -> AsyncIterator[VlLogLine]:
        self.calls.append({"expr": expr, "start": start, "end": end, "limit": limit})
        if self._fail:
            raise VictoriaLogsClientError("fake boom", 503)
        for line in self._lines:
            yield line


def _make_fetcher(
    fake: _FakeVlClient,
    *,
    clock_val: list[float] | None = None,
    cache_ttl_s: int = 300,
    max_cache_entries: int = 1000,
) -> LogWindowFetcher:
    """Build a fetcher with deterministic clocks."""
    _cv = clock_val if clock_val is not None else [0.0]

    def _clock() -> float:
        return _cv[0]

    def _wall() -> datetime:
        return _FIXED_WALL

    return LogWindowFetcher(
        fake,  # type: ignore[arg-type]
        cache_ttl_s=cache_ttl_s,
        max_cache_entries=max_cache_entries,
        clock=_clock,
        wall_clock=_wall,
    )


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

# Branch map:
#   T1 — happy path: success, no truncation, no clamp, aware tz
#   T2 — truncated=True (N+1 lines yielded)
#   T3 — limit cap (limit>1000 → effective_limit=1000)
#   T4 — window clamp (total>3600 → proportional scale)
#   T5 — naive datetime treated as UTC
#   T6 — cache HIT (within TTL, call-count stays 1, queried_at unchanged)
#   T7 — cache MISS after TTL expiry (clock advances, re-fetch)
#   T8 — LRU eviction (max_cache_entries=2, 3 distinct keys → first evicted)
#   T9 — VL error → degraded result, NOT cached (second call re-attempts)
#   T10 — one-sided AFTER window, VL returns >limit lines NEWEST-FIRST → keep
#         the OLDEST effective_limit (nearest the anchor at window START).
#   T11 — one-sided BEFORE window, VL returns >limit lines NEWEST-FIRST → keep
#         the NEWEST effective_limit (nearest the anchor at window END).


async def test_happy_path() -> None:
    """T1: N<limit lines → correct lines, truncated=False, degraded=False, queried_at set."""
    lines = [_make_vl_line(i) for i in range(5)]
    fake = _FakeVlClient(lines)
    fetcher = _make_fetcher(fake)
    anchor = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)

    result = await fetcher.fetch("*", anchor, window_before_s=60, window_after_s=60, limit=200)

    assert len(result.lines) == 5  # noqa: PLR2004
    assert result.truncated is False
    assert result.degraded is False
    assert result.queried_at == _FIXED_WALL
    assert result.window_start == anchor - timedelta(seconds=60)
    assert result.window_end == anchor + timedelta(seconds=60)
    # stream_query called once
    assert len(fake.calls) == 1
    assert fake.calls[0]["limit"] == 1001  # noqa: PLR2004  (_MAX_LIMIT + 1, oversample)


async def test_truncation() -> None:
    """T2: effective_limit+1 lines → truncated=True, only effective_limit returned."""
    limit = 10
    lines = [_make_vl_line(i) for i in range(limit + 1)]  # 11 lines
    fake = _FakeVlClient(lines)
    fetcher = _make_fetcher(fake)
    anchor = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)

    result = await fetcher.fetch("*", anchor, limit=limit)

    assert len(result.lines) == limit
    assert result.truncated is True


async def test_limit_cap() -> None:
    """T3: limit=5000 → stream_query called with limit=1001 (_MAX_LIMIT+1)."""
    fake = _FakeVlClient([_make_vl_line(0)])
    fetcher = _make_fetcher(fake)
    anchor = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)

    await fetcher.fetch("*", anchor, limit=5000)

    assert fake.calls[0]["limit"] == 1001  # noqa: PLR2004  (_MAX_LIMIT + 1, oversample; independent of caller limit)


async def test_window_clamp() -> None:
    """T4: total=4200>3600 → proportional clamp; stream_query start/end match."""
    before_s = 3000
    after_s = 1200
    # scale = 3600/4200; before_clamped = int(3000 * scale) = int(2571.4) = 2571
    # after_clamped = 3600 - 2571 = 1029
    expected_before = int(before_s * 3600 / 4200)  # 2571
    expected_after = 3600 - expected_before  # 1029

    fake = _FakeVlClient([])
    fetcher = _make_fetcher(fake)
    anchor = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)

    result = await fetcher.fetch("*", anchor, window_before_s=before_s, window_after_s=after_s)

    assert result.window_start == anchor - timedelta(seconds=expected_before)
    assert result.window_end == anchor + timedelta(seconds=expected_after)
    # ISO strings passed to stream_query match
    assert fake.calls[0]["start"] == result.window_start.isoformat()
    assert fake.calls[0]["end"] == result.window_end.isoformat()


async def test_naive_datetime_treated_as_utc() -> None:
    """T5: naive anchor_ts → treated as UTC; window_start.tzinfo is UTC."""
    fake = _FakeVlClient([])
    fetcher = _make_fetcher(fake)
    naive_anchor = datetime(2026, 1, 1, 12, 0, 0)  # no tzinfo

    result = await fetcher.fetch("*", naive_anchor)

    assert result.window_start.tzinfo is UTC
    assert result.window_end.tzinfo is UTC
    expected_start = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC) - timedelta(seconds=60)
    assert result.window_start == expected_start


async def test_cache_hit_within_ttl() -> None:
    """T6: second identical call within TTL → HIT, call-count stays 1, queried_at same."""
    lines = [_make_vl_line(0)]
    fake = _FakeVlClient(lines)
    clock_val = [0.0]
    fetcher = _make_fetcher(fake, clock_val=clock_val, cache_ttl_s=300)
    anchor = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)

    r1 = await fetcher.fetch("*", anchor)
    clock_val[0] = 100.0  # still within TTL of 300
    r2 = await fetcher.fetch("*", anchor)

    assert len(fake.calls) == 1
    assert r1.queried_at == r2.queried_at == _FIXED_WALL
    assert r2.lines == r1.lines


async def test_cache_miss_after_ttl_expiry() -> None:
    """T7: clock advances past TTL → MISS, re-fetch, call-count 2."""
    lines = [_make_vl_line(0)]
    fake = _FakeVlClient(lines)
    clock_val = [0.0]
    fetcher = _make_fetcher(fake, clock_val=clock_val, cache_ttl_s=300)
    anchor = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)

    await fetcher.fetch("*", anchor)
    clock_val[0] = 301.0  # past TTL
    await fetcher.fetch("*", anchor)

    assert len(fake.calls) == 2  # noqa: PLR2004


async def test_lru_eviction() -> None:
    """T8: max_cache_entries=2; after 3 distinct fetches, first entry is evicted."""
    fake = _FakeVlClient([_make_vl_line(0)])
    clock_val = [0.0]
    fetcher = _make_fetcher(fake, clock_val=clock_val, max_cache_entries=2)

    anchor1 = datetime(2026, 1, 1, 10, 0, 0, tzinfo=UTC)
    anchor2 = datetime(2026, 1, 1, 11, 0, 0, tzinfo=UTC)
    anchor3 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)

    await fetcher.fetch("*", anchor1)  # call 1 — fills slot 1
    await fetcher.fetch("*", anchor2)  # call 2 — fills slot 2
    await fetcher.fetch("*", anchor3)  # call 3 — evicts anchor1, fills slot 2's position

    assert len(fake.calls) == 3  # noqa: PLR2004

    # anchor1 was evicted → re-fetch triggers a 4th call
    await fetcher.fetch("*", anchor1)
    assert len(fake.calls) == 4  # noqa: PLR2004


async def test_vl_error_degrades_not_cached() -> None:
    """T9: VL error → degraded result; not cached → second call re-attempts (call-count 2)."""
    fake = _FakeVlClient([], fail=True)
    fetcher = _make_fetcher(fake)
    anchor = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)

    result = await fetcher.fetch("*", anchor)

    assert result.degraded is True
    assert result.lines == []
    assert result.truncated is False
    assert result.queried_at == _FIXED_WALL
    assert result.window_start is not None
    assert result.window_end is not None

    # Not cached — second call re-attempts
    result2 = await fetcher.fetch("*", anchor)
    assert result2.degraded is True
    assert len(fake.calls) == 2  # noqa: PLR2004


async def test_one_sided_after_keeps_nearest_anchor() -> None:
    """T10: AFTER-side window, VL yields lines NEWEST-FIRST. fetch() must keep the
    OLDEST effective_limit (nearest the anchor, which sits at the window START)."""
    limit = 3
    # 6 lines at seconds 1..6 after the anchor. VL yields NEWEST-FIRST: 6,5,4,3,2,1.
    newest_first = [_line_at(s) for s in (6, 5, 4, 3, 2, 1)]
    fake = _FakeVlClient(newest_first)
    fetcher = _make_fetcher(fake)
    anchor = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)

    result = await fetcher.fetch("*", anchor, window_before_s=0, window_after_s=1800, limit=limit)

    # Nearest the anchor (start) = seconds 1,2,3 (oldest), ascending.
    assert [ln.message for ln in result.lines] == ["sec 1", "sec 2", "sec 3"]
    assert result.truncated is True  # 6 > 3
    # Oversampled fetch cap was used.
    assert fake.calls[0]["limit"] == 1001  # noqa: PLR2004


async def test_one_sided_before_keeps_nearest_anchor() -> None:
    """T11: BEFORE-side window, VL yields lines NEWEST-FIRST. fetch() must keep the
    NEWEST effective_limit (nearest the anchor, which sits at the window END)."""
    limit = 3
    # 6 lines at seconds 1..6 (i.e. anchor-6 .. anchor-1). VL yields NEWEST-FIRST.
    newest_first = [_line_at(s) for s in (6, 5, 4, 3, 2, 1)]
    fake = _FakeVlClient(newest_first)
    fetcher = _make_fetcher(fake)
    anchor = datetime(2026, 1, 1, 12, 0, 10, tzinfo=UTC)  # after all planted lines

    result = await fetcher.fetch("*", anchor, window_before_s=1800, window_after_s=0, limit=limit)

    # Nearest the anchor (end) = the 3 newest = seconds 4,5,6, ascending.
    assert [ln.message for ln in result.lines] == ["sec 4", "sec 5", "sec 6"]
    assert result.truncated is True


async def test_duplicate_timestamps_memoize_cache() -> None:
    """Timestamp memoization: lines with duplicate _time strings share one parse."""
    limit = 10
    # 4 lines: 2 with same timestamp, then 2 more with same timestamp
    lines = [
        VlLogLine(
            timestamp="2026-01-01T12:00:05Z",
            message="msg a",
            stream="journal",
            fields={},
        ),
        VlLogLine(
            timestamp="2026-01-01T12:00:05Z",
            message="msg b",
            stream="journal",
            fields={},
        ),
        VlLogLine(
            timestamp="2026-01-01T12:00:10Z",
            message="msg c",
            stream="journal",
            fields={},
        ),
        VlLogLine(
            timestamp="2026-01-01T12:00:10Z",
            message="msg d",
            stream="journal",
            fields={},
        ),
    ]
    fake = _FakeVlClient(lines)
    fetcher = _make_fetcher(fake)
    anchor = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)

    result = await fetcher.fetch("*", anchor, limit=limit)

    # All 4 lines returned, sorted ascending by timestamp (memoized parse reused).
    assert len(result.lines) == 4  # noqa: PLR2004
    # Lines with same timestamp stay in their original order (stable sort).
    assert [ln.message for ln in result.lines] == ["msg a", "msg b", "msg c", "msg d"]
    assert result.truncated is False


def test_default_wall_clock_returns_aware_utc() -> None:
    """The production default wall clock returns a tz-aware UTC datetime."""
    now = _default_wall_clock()
    assert now.tzinfo is UTC
