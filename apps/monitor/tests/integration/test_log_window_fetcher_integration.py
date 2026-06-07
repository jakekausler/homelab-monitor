"""Integration test: LogWindowFetcher against real VictoriaLogs (STAGE-004-031).

Validates the behaviours that unit tests with a fake VL client cannot confirm:
  1. Real window filtering: planted lines inside the window appear; lines
     outside the window (anchor ± 2h) are excluded.
  2. Truncation against real data: limit=2 with 5 in-window lines → truncated=True.
  3. Cache hit: two identical fetches return equal results with identical queried_at.
  4. Window-scoping: narrow window returns fewer lines than wide window.
  5. Degraded path: unreachable VL URL → degraded=True, lines=[], no raise.

Unlike most integration tests this module does NOT need the full rig
(monitor + fixture-host + noisy-logger). It only needs VictoriaLogs.
Bring up just VL with:

    docker compose -f deploy/compose/docker-compose.test.yml up -d victorialogs

Then run with:

    VL_URL=http://127.0.0.1:9428 pytest tests/integration/test_log_window_fetcher_integration.py \
        -m integration

All tests auto-skip fast when VictoriaLogs is unreachable.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from homelab_monitor.kernel.config import VlQueryLimits
from homelab_monitor.kernel.logs.log_window_fetcher import LogWindowFetcher
from homelab_monitor.kernel.logs.victorialogs_client import VictoriaLogsClient

from .helpers.rig_health import require_rig_components
from .helpers.vl_planter import plant_log_lines

_VL_INGEST_BUDGET_S = 30.0
_VL_INGEST_POLL_S = 2.0

# Unique service label isolates all tests in this module from other rig traffic.
_SERVICE = "lwf-itest"


def _vl_url() -> str:
    return os.environ.get("VL_URL", "http://victorialogs:9428").rstrip("/")


def _make_vl_client(vl_url: str, http_client: httpx.AsyncClient) -> VictoriaLogsClient:
    limits = VlQueryLimits(max_lines=50_000, max_bytes=50 * 1024 * 1024, timeout_seconds=30.0)
    return VictoriaLogsClient(vl_url=vl_url, http_client=http_client, limits=limits)


def _wait_for_vl_ingest(vl_url: str, marker: str, expected_count: int) -> None:
    """Poll VL until at least expected_count lines with marker are visible."""
    deadline = time.time() + _VL_INGEST_BUDGET_S
    last_count = 0
    while time.time() < deadline:
        now = datetime.now(UTC)
        start = (now - timedelta(hours=3)).isoformat()
        end = now.isoformat()
        params: dict[str, Any] = {
            "query": f'service:{_SERVICE} "{marker}"',
            "start": start,
            "end": end,
            "limit": str(expected_count + 10),
        }
        try:
            resp = httpx.get(f"{vl_url}/select/logsql/query", params=params, timeout=10.0)
            if resp.status_code == 200:  # noqa: PLR2004
                lines = [ln for ln in resp.text.splitlines() if ln.strip()]
                last_count = len(lines)
                if last_count >= expected_count:
                    return
        except httpx.RequestError:
            pass
        time.sleep(_VL_INGEST_POLL_S)
    msg = (
        f"VL ingest wait: marker {marker!r} did not surface {expected_count} lines "
        f"within {_VL_INGEST_BUDGET_S}s (last count: {last_count})."
    )
    raise AssertionError(msg)


@pytest.mark.integration
@pytest.mark.slow
def test_lwf_real_window_filtering() -> None:
    """Lines inside the window are returned; lines 2h outside are excluded.

    Plants 10 in-window lines (anchor ± 30s) and 2 far-outside lines
    (anchor - 2h, anchor + 2h). A 60/60 window fetch must return only the
    in-window lines, degraded=False, truncated=False.
    """
    require_rig_components("victorialogs")

    vl = _vl_url()
    marker = f"lwf-filter-{uuid.uuid4().hex}"
    anchor = datetime.now(UTC) - timedelta(seconds=5)

    # Plant 10 in-window lines across anchor ± 30 s
    plant_log_lines(
        host="rig-lwf-host",
        service=_SERVICE,
        severity="info",
        message=f"lwf window-filter in-window marker={marker}",
        count=10,
        base_time=anchor - timedelta(seconds=29),
        interval_ms=600,  # 10 x 600ms = 6s total, fits within +-30s
        vl_url=vl,
    )
    # Plant 1 line 2 h BEFORE the anchor (outside the window)
    plant_log_lines(
        host="rig-lwf-host",
        service=_SERVICE,
        severity="info",
        message=f"lwf window-filter out-before marker={marker}",
        count=1,
        base_time=anchor - timedelta(hours=2),
        vl_url=vl,
    )
    # Plant 1 line 2 h AFTER the anchor (outside the window)
    plant_log_lines(
        host="rig-lwf-host",
        service=_SERVICE,
        severity="info",
        message=f"lwf window-filter out-after marker={marker}",
        count=1,
        base_time=anchor + timedelta(hours=2),
        vl_url=vl,
    )

    _wait_for_vl_ingest(vl, marker, 10)

    async def _fetch() -> Any:  # noqa: ANN401
        async with httpx.AsyncClient() as http_client:
            vl_client = _make_vl_client(vl, http_client)
            fetcher = LogWindowFetcher(vl_client)
            return await fetcher.fetch(
                logs_ql=f'service:"{_SERVICE}" "{marker}"',
                anchor_ts=anchor,
                window_before_s=60,
                window_after_s=60,
                limit=200,
            )

    result = asyncio.run(_fetch())

    assert result.degraded is False, f"Expected degraded=False, got degraded={result.degraded}"
    assert result.truncated is False, f"Expected truncated=False, got truncated={result.truncated}"
    assert len(result.lines) == 10, (  # noqa: PLR2004
        f"Expected 10 in-window lines, got {len(result.lines)}"
    )
    # Confirm the out-of-window messages are NOT in the result
    messages = [ln.message for ln in result.lines]
    assert not any("out-before" in m for m in messages), (
        f"Out-of-window before-anchor line should not appear: {messages}"
    )
    assert not any("out-after" in m for m in messages), (
        f"Out-of-window after-anchor line should not appear: {messages}"
    )
    # Confirm window_start / window_end are correctly set
    expected_start = anchor - timedelta(seconds=60)
    expected_end = anchor + timedelta(seconds=60)
    assert abs((result.window_start - expected_start).total_seconds()) < 1, (
        f"window_start mismatch: got {result.window_start}, expected ~{expected_start}"
    )
    assert abs((result.window_end - expected_end).total_seconds()) < 1, (
        f"window_end mismatch: got {result.window_end}, expected ~{expected_end}"
    )


@pytest.mark.integration
@pytest.mark.slow
def test_lwf_truncation() -> None:
    """limit=2 with 5 in-window lines → truncated=True, exactly 2 lines returned."""
    require_rig_components("victorialogs")

    vl = _vl_url()
    marker = f"lwf-trunc-{uuid.uuid4().hex}"
    anchor = datetime.now(UTC) - timedelta(seconds=5)

    plant_log_lines(
        host="rig-lwf-host",
        service=_SERVICE,
        severity="info",
        message=f"lwf truncation test marker={marker}",
        count=5,
        base_time=anchor - timedelta(seconds=10),
        interval_ms=200,
        vl_url=vl,
    )
    _wait_for_vl_ingest(vl, marker, 5)

    async def _fetch() -> Any:  # noqa: ANN401
        async with httpx.AsyncClient() as http_client:
            vl_client = _make_vl_client(vl, http_client)
            fetcher = LogWindowFetcher(vl_client)
            return await fetcher.fetch(
                logs_ql=f'service:"{_SERVICE}" "{marker}"',
                anchor_ts=anchor,
                window_before_s=60,
                window_after_s=60,
                limit=2,
            )

    result = asyncio.run(_fetch())

    assert result.degraded is False
    assert result.truncated is True, (
        f"Expected truncated=True with 5 lines and limit=2, got truncated={result.truncated}"
    )
    assert len(result.lines) == 2, (  # noqa: PLR2004
        f"Expected exactly 2 lines (the limit), got {len(result.lines)}"
    )


@pytest.mark.integration
@pytest.mark.slow
def test_lwf_cache_hit() -> None:
    """Two identical fetches return equal results with identical queried_at.

    The second call MUST return the cached result — queried_at is frozen at the
    first call's timestamp. We inject a monotonic clock to control TTL
    deterministically and verify the cache doesn't expire between the two calls.
    """
    require_rig_components("victorialogs")

    vl = _vl_url()
    marker = f"lwf-cache-{uuid.uuid4().hex}"
    anchor = datetime.now(UTC) - timedelta(seconds=5)

    plant_log_lines(
        host="rig-lwf-host",
        service=_SERVICE,
        severity="info",
        message=f"lwf cache test marker={marker}",
        count=3,
        base_time=anchor - timedelta(seconds=5),
        interval_ms=500,
        vl_url=vl,
    )
    _wait_for_vl_ingest(vl, marker, 3)

    # Frozen monotonic clock so cache never expires during this test
    frozen_clock_value = time.monotonic()

    def _frozen_clock() -> float:
        return frozen_clock_value

    async def _fetch_twice() -> tuple[Any, Any]:
        async with httpx.AsyncClient() as http_client:
            vl_client = _make_vl_client(vl, http_client)
            fetcher = LogWindowFetcher(vl_client, clock=_frozen_clock)
            first = await fetcher.fetch(
                logs_ql=f'service:"{_SERVICE}" "{marker}"',
                anchor_ts=anchor,
                window_before_s=60,
                window_after_s=60,
                limit=200,
            )
            second = await fetcher.fetch(
                logs_ql=f'service:"{_SERVICE}" "{marker}"',
                anchor_ts=anchor,
                window_before_s=60,
                window_after_s=60,
                limit=200,
            )
            return first, second

    first, second = asyncio.run(_fetch_twice())

    assert first.queried_at == second.queried_at, (
        f"Cache hit must preserve original queried_at: "
        f"first={first.queried_at!r}, second={second.queried_at!r}"
    )
    assert len(first.lines) == len(second.lines), (
        f"Cache hit must return same line count: {len(first.lines)} vs {len(second.lines)}"
    )
    assert first.degraded == second.degraded
    assert first.truncated == second.truncated


@pytest.mark.integration
@pytest.mark.slow
def test_lwf_window_scoping() -> None:
    """Narrow window returns fewer lines than wide window (window math scopes VL query)."""
    require_rig_components("victorialogs")

    vl = _vl_url()
    marker = f"lwf-scope-{uuid.uuid4().hex}"
    anchor = datetime.now(UTC) - timedelta(seconds=5)

    # Plant 5 lines tightly around anchor (within ±5 s)
    plant_log_lines(
        host="rig-lwf-host",
        service=_SERVICE,
        severity="info",
        message=f"lwf scope near marker={marker}",
        count=5,
        base_time=anchor - timedelta(seconds=4),
        interval_ms=1000,  # 5 x 1s = 5s total, fits within +-5s
        vl_url=vl,
    )
    # Plant 5 lines 30-60 s before anchor (outside +-5s but inside +-60s)
    plant_log_lines(
        host="rig-lwf-host",
        service=_SERVICE,
        severity="info",
        message=f"lwf scope far marker={marker}",
        count=5,
        base_time=anchor - timedelta(seconds=59),
        interval_ms=2000,
        vl_url=vl,
    )

    _wait_for_vl_ingest(vl, marker, 10)

    async def _fetch_narrow_and_wide() -> tuple[Any, Any]:
        async with httpx.AsyncClient() as http_client:
            vl_client = _make_vl_client(vl, http_client)
            # Use fresh fetcher instances so no cache interference
            narrow_fetcher = LogWindowFetcher(vl_client)
            narrow = await narrow_fetcher.fetch(
                logs_ql=f'service:"{_SERVICE}" "{marker}"',
                anchor_ts=anchor,
                window_before_s=5,
                window_after_s=5,
                limit=200,
            )
            wide_fetcher = LogWindowFetcher(vl_client)
            wide = await wide_fetcher.fetch(
                logs_ql=f'service:"{_SERVICE}" "{marker}"',
                anchor_ts=anchor,
                window_before_s=60,
                window_after_s=60,
                limit=200,
            )
            return narrow, wide

    narrow, wide = asyncio.run(_fetch_narrow_and_wide())

    assert narrow.degraded is False
    assert wide.degraded is False
    assert len(narrow.lines) < len(wide.lines), (
        f"Narrow window (±5s) should return fewer lines than wide (±60s): "
        f"narrow={len(narrow.lines)}, wide={len(wide.lines)}"
    )
    # Near lines should appear in wide; far lines should NOT appear in narrow
    narrow_messages = [ln.message for ln in narrow.lines]
    assert not any("scope far" in m for m in narrow_messages), (
        f"Far lines (anchor-30..60s) should not appear in ±5s window: {narrow_messages}"
    )


@pytest.mark.integration
@pytest.mark.slow
def test_lwf_degraded_on_unreachable_vl() -> None:
    """Unreachable VL URL → degraded=True, lines=[], no raise.

    Constructs a VictoriaLogsClient pointed at a bad port. fetch() must
    return degraded=True instead of raising.
    """
    # Note: this test does NOT need a real VL to be UP — it proves graceful
    # degradation against a bad URL. We only need VL to be configured.
    # We skip if victorialogs is down to avoid false-positives from missing rig.
    require_rig_components("victorialogs")

    anchor = datetime.now(UTC)

    async def _fetch_bad_url() -> Any:  # noqa: ANN401
        async with httpx.AsyncClient() as http_client:
            # Port 19999 is not bound to anything on this host
            bad_client = VictoriaLogsClient(
                vl_url="http://127.0.0.1:19999",
                http_client=http_client,
                limits=VlQueryLimits(max_lines=100, max_bytes=1024 * 1024, timeout_seconds=2.0),
            )
            fetcher = LogWindowFetcher(bad_client)
            return await fetcher.fetch(
                logs_ql='service:"lwf-degrade-test"',
                anchor_ts=anchor,
                window_before_s=60,
                window_after_s=60,
                limit=10,
            )

    result = asyncio.run(_fetch_bad_url())

    assert result.degraded is True, (
        f"Expected degraded=True when VL is unreachable, got degraded={result.degraded}"
    )
    assert result.lines == [], (
        f"Expected empty lines on degraded result, got {len(result.lines)} lines"
    )
    assert result.truncated is False


@pytest.mark.integration
@pytest.mark.slow
def test_lwf_after_side_keeps_lines_adjacent_to_anchor() -> None:
    """REGRESSION (empty-window bug): one-sided AFTER window with MORE than `limit`
    lines spread across ~30 min must return the lines IMMEDIATELY after the anchor
    (contiguous, oldest-first), not the far-future newest tail.
    """
    require_rig_components("victorialogs")

    vl = _vl_url()
    marker = f"lwf-after-adjacent-{uuid.uuid4().hex}"
    anchor = datetime.now(UTC) - timedelta(minutes=35)  # leave room for +30min window

    count = 60
    limit = 20
    # 60 lines, 30s apart → spans 30 min starting 1s after the anchor.
    plant_log_lines(
        host="rig-lwf-host",
        service=_SERVICE,
        severity="info",
        message=f"lwf after-adjacent marker={marker}",
        count=count,
        base_time=anchor + timedelta(seconds=1),
        interval_ms=30_000,  # 30s apart
        vl_url=vl,
    )
    _wait_for_vl_ingest(vl, marker, count)

    async def _fetch() -> Any:  # noqa: ANN401
        async with httpx.AsyncClient() as http_client:
            vl_client = _make_vl_client(vl, http_client)
            fetcher = LogWindowFetcher(vl_client)
            return await fetcher.fetch(
                logs_ql=f'service:"{_SERVICE}" "{marker}"',
                anchor_ts=anchor,
                window_before_s=0,
                window_after_s=1800,
                limit=limit,
            )

    result = asyncio.run(_fetch())

    assert result.degraded is False
    assert result.truncated is True, "60 lines with limit=20 must truncate"
    assert len(result.lines) == limit
    # Lines must be ascending by timestamp (oldest-first).
    timestamps = [ln.timestamp for ln in result.lines]
    assert timestamps == sorted(timestamps), f"not ascending: {timestamps}"
    # The FIRST returned line must be within ~2 min of the anchor (adjacent, not a
    # far-future island ~30 min away). This is the assertion that FAILED pre-fix.
    first_ns = datetime.fromisoformat(timestamps[0].replace("Z", "+00:00"))
    delta_s = (first_ns - anchor).total_seconds()
    assert 0 <= delta_s < 120, (  # noqa: PLR2004
        f"First after-side line should be adjacent to anchor (<120s), got {delta_s}s"
    )
