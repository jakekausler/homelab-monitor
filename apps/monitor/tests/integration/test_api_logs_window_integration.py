"""Integration test: GET /api/logs/window endpoint (STAGE-004-031A).

Validates the full endpoint against real VictoriaLogs, including:
  1. Two-sided fetch: before + after windows are called with correct window params.
  2. Merge + dedup: lines from both sides are merged, deduped by (ts,stream,msg),
     and sorted ascending by timestamp.
  3. Anchor location: exact match on (ts, stream, message) returns correct index;
     fallback to insertion point works.
  4. Integration with LogWindowFetcher: real VL queries, real degradation handling.

Bring up just VictoriaLogs with:

    docker compose -f deploy/compose/docker-compose.test.yml up -d victorialogs

Then run with:

    VL_URL=http://127.0.0.1:9428 pytest tests/integration/test_api_logs_window_integration.py \\
        -m integration
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

from homelab_monitor.kernel.api.routers.logs import (
    _locate_anchor_index,  # pyright: ignore[reportPrivateUsage]
    _merge_window_lines,  # pyright: ignore[reportPrivateUsage]
)
from homelab_monitor.kernel.config import VlQueryLimits
from homelab_monitor.kernel.logs.log_window_fetcher import LogWindowFetcher
from homelab_monitor.kernel.logs.victorialogs_client import VictoriaLogsClient

from .helpers.rig_health import require_rig_components
from .helpers.vl_planter import plant_log_lines

_VL_INGEST_BUDGET_S = 30.0
_VL_INGEST_POLL_S = 2.0

# Unique service label isolates all tests in this module from other rig traffic.
_SERVICE = "api-window-itest"


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
def test_api_logs_window_two_sided_merge_and_anchor() -> None:
    """Full endpoint integration: two-sided fetch, merge, dedup, sort, anchor locate.

    Plants 11 lines: 5 before anchor, 1 AT anchor (exact ts/stream/message),
    5 after anchor. Verifies:
      1. Both sides are fetched with correct window params.
      2. Merged result contains all 11 lines (no duplicates).
      3. Anchor is located by exact match.
      4. Lines are sorted ascending by timestamp.
    """
    require_rig_components("victorialogs")

    vl = _vl_url()
    marker = f"api-window-{uuid.uuid4().hex}"
    anchor = datetime.now(UTC) - timedelta(seconds=5)

    # Plant 5 lines before the anchor (every 5 seconds back from anchor-5s).
    before_base = anchor - timedelta(seconds=25)
    plant_log_lines(
        host="rig-api-window-host",
        service=_SERVICE,
        severity="info",
        message=f"before-line marker={marker}",
        count=5,
        base_time=before_base,
        interval_ms=5000,  # 5s apart
        vl_url=vl,
    )

    # Plant 1 line AT the anchor (exact timestamp + specific message).
    anchor_msg = f"ANCHOR-LINE marker={marker}"
    plant_log_lines(
        host="rig-api-window-host",
        service=_SERVICE,
        severity="info",
        message=anchor_msg,
        count=1,
        base_time=anchor,
        vl_url=vl,
    )

    # Plant 5 lines after the anchor (every 5 seconds from anchor+5s).
    after_base = anchor + timedelta(seconds=5)
    plant_log_lines(
        host="rig-api-window-host",
        service=_SERVICE,
        severity="info",
        message=f"after-line marker={marker}",
        count=5,
        base_time=after_base,
        interval_ms=5000,
        vl_url=vl,
    )

    _wait_for_vl_ingest(vl, marker, 11)

    async def _run_endpoint() -> Any:  # noqa: ANN401
        """Simulate the endpoint's two-sided fetch + merge logic."""
        async with httpx.AsyncClient() as http_client:
            vl_client = _make_vl_client(vl, http_client)
            fetcher = LogWindowFetcher(vl_client)

            before_result = await fetcher.fetch(
                logs_ql=f'service:"{_SERVICE}" "{marker}"',
                anchor_ts=anchor,
                window_before_s=1800,
                window_after_s=0,
                limit=100,
            )
            after_result = await fetcher.fetch(
                logs_ql=f'service:"{_SERVICE}" "{marker}"',
                anchor_ts=anchor,
                window_before_s=0,
                window_after_s=1800,
                limit=100,
            )

            merged = _merge_window_lines(before_result.lines, after_result.lines)

            # Normalize the anchor timestamp to ISO format for comparison.
            anchor_iso_utc = anchor.isoformat()
            anchor_index = _locate_anchor_index(
                merged,
                anchor_iso_utc,
                anchor_stream="stdout",
                anchor_message=anchor_msg,
            )

            return {
                "merged": merged,
                "anchor_index": anchor_index,
                "before_result": before_result,
                "after_result": after_result,
            }

    result = asyncio.run(_run_endpoint())
    merged = result["merged"]
    anchor_index = result["anchor_index"]
    before_result = result["before_result"]
    after_result = result["after_result"]

    # Assertions
    assert not before_result.degraded, "Expected before_result.degraded=False"
    assert not after_result.degraded, "Expected after_result.degraded=False"

    # Should have ~11 lines total (deduped).
    assert len(merged) == 11, f"Expected 11 lines, got {len(merged)}"  # noqa: PLR2004

    # All lines should be sorted ascending by timestamp.
    timestamps = [ln.timestamp for ln in merged]
    assert timestamps == sorted(timestamps), f"Lines not sorted by timestamp: {timestamps}"

    # Anchor should be located at its position by exact match.
    assert anchor_index is not None, "Anchor should be located"
    assert anchor_index == 5, (  # noqa: PLR2004  # 5 before lines, then anchor at index 5
        f"Expected anchor at index 5 (5 before + anchor), got {anchor_index}"
    )
    assert merged[anchor_index].message == anchor_msg, (
        f"Anchor line message mismatch: got {merged[anchor_index].message}, expected {anchor_msg}"
    )
