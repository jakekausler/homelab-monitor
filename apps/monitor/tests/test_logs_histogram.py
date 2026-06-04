"""Unit tests for kernel.logs.histogram (STAGE-004-019)."""

from __future__ import annotations

import re

import httpx
import pytest
from pytest_httpx import HTTPXMock

from homelab_monitor.kernel.api.schemas import LogsHistogramResponse
from homelab_monitor.kernel.config import VlQueryLimits
from homelab_monitor.kernel.logs.histogram import (
    HistogramCache,
    assign_bucket,
    bucket_count,
    coarse_bucket,
    compute_step_ms,
    fetch_histogram,
    ms_to_iso,
    parse_iso_to_ms,
    step_ms_to_duration,
)
from homelab_monitor.kernel.logs.victorialogs_client import VictoriaLogsClient

_VL_URL = "http://vl-test:9428"
_HITS_RE = re.compile(r"http://vl-test:9428/select/logsql/hits.*")


def _make_client(http: httpx.AsyncClient) -> VictoriaLogsClient:
    return VictoriaLogsClient(
        vl_url=_VL_URL,
        http_client=http,
        limits=VlQueryLimits(max_lines=200, max_bytes=1_000_000, timeout_seconds=5.0),
    )


# --- coarse_bucket ---------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("error", "error"),
        ("critical", "error"),
        ("alert", "error"),
        ("emergency", "error"),
        ("err", "error"),  # alias -> error
        ("crit", "error"),  # alias -> critical -> error
        ("panic", "error"),  # alias -> emergency -> error
        ("warn", "warn"),
        ("warning", "warn"),  # alias -> warn
        ("info", "info"),
        ("notice", "info"),
        ("debug", "info"),
        ("0", "error"),  # syslog emergency
        ("1", "error"),  # alert
        ("2", "error"),  # critical
        ("3", "error"),  # error
        ("4", "warn"),  # warn
        ("5", "info"),  # notice
        ("6", "info"),  # info
        ("7", "info"),  # debug
        (None, "info"),
        ("", "info"),  # empty -> None -> info
        ("totally-unknown", "info"),  # unknown -> normalize "info" -> info
    ],
)
def test_coarse_bucket(raw: str | None, expected: str) -> None:
    assert coarse_bucket(raw) == expected


# --- compute_step_ms / bucket_count / step_ms_to_duration ------------------


def test_compute_step_ms_even_division() -> None:
    # 60_000 ms span / 60 buckets = 1000 ms.
    assert compute_step_ms(0, 60_000, 60) == 1000  # noqa: PLR2004


def test_compute_step_ms_ceil_not_floor() -> None:
    # 100 ms / 60 buckets = 1.66 -> ceil -> 2.
    assert compute_step_ms(0, 100, 60) == 2  # noqa: PLR2004


def test_compute_step_ms_floor_at_1ms() -> None:
    assert compute_step_ms(0, 0, 60) == 1
    assert compute_step_ms(100, 50, 60) == 1  # negative span -> 1


def test_step_ms_to_duration_ms_suffix() -> None:
    assert step_ms_to_duration(1000) == "1000ms"


def test_bucket_count_inclusive_end() -> None:
    # span 60_000, step 1000 -> floor(60) + 1 = 61 (covers inclusive end).
    assert bucket_count(0, 60_000, 1000) == 61  # noqa: PLR2004


def test_bucket_count_degenerate() -> None:
    assert bucket_count(0, 0, 1000) == 1
    assert bucket_count(0, 1000, 0) == 1


# --- assign_bucket ---------------------------------------------------------


def test_assign_bucket_start_maps_to_zero() -> None:
    assert assign_bucket(1000, 1000, 100, 10) == 0


def test_assign_bucket_before_start_clamps_to_zero() -> None:
    # VL epoch-aligned ts can precede start.
    assert assign_bucket(900, 1000, 100, 10) == 0


def test_assign_bucket_at_or_past_end_clamps_to_last() -> None:
    # offset == n -> clamp n-1.
    assert assign_bucket(1000 + 10 * 100, 1000, 100, 10) == 9  # noqa: PLR2004
    assert assign_bucket(99_999, 1000, 100, 10) == 9  # noqa: PLR2004


def test_assign_bucket_interior() -> None:
    assert assign_bucket(1250, 1000, 100, 10) == 2  # noqa: PLR2004


def test_assign_bucket_zero_step() -> None:
    assert assign_bucket(5000, 1000, 0, 10) == 0


# --- parse_iso_to_ms / ms_to_iso roundtrip ---------------------------------


def test_parse_iso_to_ms_naive_is_utc() -> None:
    assert parse_iso_to_ms("1970-01-01T00:00:01") == 1000  # noqa: PLR2004


def test_parse_iso_to_ms_tz_aware() -> None:
    assert parse_iso_to_ms("1970-01-01T00:00:01+00:00") == 1000  # noqa: PLR2004


def test_ms_to_iso_roundtrip() -> None:
    iso = ms_to_iso(1000)
    assert parse_iso_to_ms(iso) == 1000  # noqa: PLR2004


# --- fetch_histogram -------------------------------------------------------


def _hits_json(series: list[dict[str, object]]) -> dict[str, object]:
    return {"hits": series}


@pytest.mark.asyncio
async def test_fetch_histogram_happy_path_stacks_severities(httpx_mock: HTTPXMock) -> None:
    """Per-severity series re-binned + coarse-mapped onto start-aligned buckets."""
    start = "2026-05-19T00:00:00+00:00"
    end = "2026-05-19T00:01:00+00:00"  # 60s span
    httpx_mock.add_response(
        url=_HITS_RE,
        method="GET",
        json=_hits_json(
            [
                {
                    "fields": {"severity": "error"},
                    "timestamps": ["2026-05-19T00:00:00Z", "2026-05-19T00:00:30Z"],
                    "values": [2, 3],
                },
                {
                    "fields": {"severity": "warning"},  # alias -> warn
                    "timestamps": ["2026-05-19T00:00:00Z"],
                    "values": [7],
                },
                {
                    "fields": {"severity": "info"},
                    "timestamps": ["2026-05-19T00:00:30Z"],
                    "values": [11],
                },
            ]
        ),
    )
    async with httpx.AsyncClient() as http:
        client = _make_client(http)
        resp = await fetch_histogram(client=client, expr="*", start=start, end=end, buckets=2)
    assert isinstance(resp, LogsHistogramResponse)
    # span 60_000 / 2 buckets = 30_000 step; bucket_count = floor(60000/30000)+1 = 3.
    assert resp.bucket_duration_ms == 30_000  # noqa: PLR2004
    assert len(resp.buckets) == 3  # noqa: PLR2004
    # bucket 0 [0,30s): error 2, warn 7, info 0.
    b0 = resp.buckets[0]
    assert b0.counts_by_severity == {"error": 2, "warn": 7, "info": 0}
    assert b0.total == 9  # noqa: PLR2004
    # bucket 1 [30s,60s): error 3, warn 0, info 11.
    b1 = resp.buckets[1]
    assert b1.counts_by_severity == {"error": 3, "warn": 0, "info": 11}
    # bucket 2 [60s,90s): empty (covers inclusive end).
    assert resp.buckets[2].counts_by_severity == {"error": 0, "warn": 0, "info": 0}
    # All buckets carry all three keys.
    for b in resp.buckets:
        assert set(b.counts_by_severity) == {"error", "warn", "info"}


@pytest.mark.asyncio
async def test_fetch_histogram_empty_zero_filled(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url=_HITS_RE, method="GET", json=_hits_json([]))
    async with httpx.AsyncClient() as http:
        client = _make_client(http)
        resp = await fetch_histogram(
            client=client,
            expr="*",
            start="2026-05-19T00:00:00+00:00",
            end="2026-05-19T00:01:00+00:00",
            buckets=2,
        )
    assert len(resp.buckets) == 3  # noqa: PLR2004
    assert all(b.total == 0 for b in resp.buckets)
    assert all(b.counts_by_severity == {"error": 0, "warn": 0, "info": 0} for b in resp.buckets)


@pytest.mark.asyncio
async def test_fetch_histogram_rebins_epoch_aligned_before_start(httpx_mock: HTTPXMock) -> None:
    """A VL timestamp BEFORE start (epoch grid) clamps to bucket 0."""
    httpx_mock.add_response(
        url=_HITS_RE,
        method="GET",
        json=_hits_json(
            [
                {
                    "fields": {"severity": "info"},
                    "timestamps": ["2026-05-18T23:59:59Z"],  # 1s before start
                    "values": [4],
                }
            ]
        ),
    )
    async with httpx.AsyncClient() as http:
        client = _make_client(http)
        resp = await fetch_histogram(
            client=client,
            expr="*",
            start="2026-05-19T00:00:00+00:00",
            end="2026-05-19T00:01:00+00:00",
            buckets=2,
        )
    assert resp.buckets[0].counts_by_severity["info"] == 4  # noqa: PLR2004


@pytest.mark.asyncio
async def test_fetch_histogram_none_severity_to_info(httpx_mock: HTTPXMock) -> None:
    """A fields:{} (no severity) series coarse-maps to info."""
    httpx_mock.add_response(
        url=_HITS_RE,
        method="GET",
        json=_hits_json([{"fields": {}, "timestamps": ["2026-05-19T00:00:00Z"], "values": [5]}]),
    )
    async with httpx.AsyncClient() as http:
        client = _make_client(http)
        resp = await fetch_histogram(
            client=client,
            expr="*",
            start="2026-05-19T00:00:00+00:00",
            end="2026-05-19T00:01:00+00:00",
            buckets=2,
        )
    assert resp.buckets[0].counts_by_severity["info"] == 5  # noqa: PLR2004


@pytest.mark.asyncio
async def test_fetch_histogram_propagates_vl_error(httpx_mock: HTTPXMock) -> None:
    from homelab_monitor.kernel.logs.victorialogs_client import (  # noqa: PLC0415
        VictoriaLogsClientError,
    )

    httpx_mock.add_response(url=_HITS_RE, method="GET", status_code=500, text="err")
    async with httpx.AsyncClient() as http:
        client = _make_client(http)
        with pytest.raises(VictoriaLogsClientError):
            await fetch_histogram(
                client=client,
                expr="*",
                start="2026-05-19T00:00:00+00:00",
                end="2026-05-19T00:01:00+00:00",
                buckets=2,
            )


# --- HistogramCache --------------------------------------------------------


def _empty_resp() -> LogsHistogramResponse:
    return LogsHistogramResponse(buckets=[], bucket_duration_ms=1000)


def test_histogram_cache_hit_within_ttl() -> None:
    now = [0.0]
    cache = HistogramCache(ttl_seconds=30, clock=lambda: now[0])
    key = HistogramCache.make_key(expr="*", start="a", end="b", buckets=60)
    val = _empty_resp()
    cache.put(key, val)
    assert cache.get(key) is val


def test_histogram_cache_miss_after_ttl() -> None:
    now = [0.0]
    cache = HistogramCache(ttl_seconds=30, clock=lambda: now[0])
    key = HistogramCache.make_key(expr="*", start="a", end="b", buckets=60)
    cache.put(key, _empty_resp())
    now[0] = 30.0
    assert cache.get(key) is None
    assert cache.get(key) is None  # evicted; still None


def test_histogram_cache_miss_on_missing_key() -> None:
    cache = HistogramCache()
    key = HistogramCache.make_key(expr="*", start="a", end="b", buckets=60)
    assert cache.get(key) is None


def test_histogram_cache_key_hashes_expr() -> None:
    k1 = HistogramCache.make_key(expr="a", start="s", end="e", buckets=60)
    k2 = HistogramCache.make_key(expr="b", start="s", end="e", buckets=60)
    k3 = HistogramCache.make_key(expr="a", start="s", end="e", buckets=60)
    assert k1 != k2
    assert k1 == k3
    assert k1[0] != "a"  # expr hashed, not raw
