"""Integration: VL /hits field=severity grouping returns per-severity series.

STAGE-004-019. Plant known-severity lines directly into VictoriaLogs, then poll
/api/logs/histogram and assert the per-severity stacked counts surface non-zero.
This validates the one behavior unit tests mock: that v0.30.0 /select/logsql/hits
with field=severity actually returns one series per distinct severity.

Runs via `make integration` (full docker-compose.test.yml stack), NOT `make verify`.
"""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest

from .helpers.rig import Rig
from .helpers.rig_health import require_rig_components
from .helpers.vl_planter import plant_log_lines

POLL_BUDGET_S = 30.0


@pytest.mark.integration
@pytest.mark.slow
def test_histogram_hits_returns_per_severity_series() -> None:
    """Plant error+warn+info lines; assert the histogram stacks each non-zero."""
    require_rig_components("monitor", "victorialogs", "noisy-logger")

    marker = f"rig-hist-{uuid.uuid4().hex}"
    base = datetime.now(UTC) - timedelta(seconds=20)

    with Rig.boot() as rig:
        # Plant known counts per severity, all carrying the unique marker in _msg.
        plant_log_lines(
            host="rig-host",
            service="rig-svc",
            severity="error",
            message=marker,
            count=3,
            base_time=base,
            interval_ms=200,
        )
        plant_log_lines(
            host="rig-host",
            service="rig-svc",
            severity="warning",
            message=marker,
            count=2,
            base_time=base,
            interval_ms=200,
        )
        plant_log_lines(
            host="rig-host",
            service="rig-svc",
            severity="info",
            message=marker,
            count=5,
            base_time=base,
            interval_ms=200,
        )

        deadline = time.time() + POLL_BUDGET_S
        body: dict[str, Any] | None = None
        last_text = ""
        while time.time() < deadline:
            now = datetime.now(UTC)
            start = (now - timedelta(minutes=2)).isoformat()
            end = now.isoformat()  # INCLUSIVE, never future
            resp = rig.get(
                "/api/logs/histogram",
                params={"expr": f'"{marker}"', "start": start, "end": end, "buckets": "60"},
            )
            if resp.status_code == 200:  # noqa: PLR2004
                last_text = resp.text
                candidate = resp.json()
                totals = {"error": 0, "warn": 0, "info": 0}
                for b in cast(list[dict[str, Any]], candidate.get("buckets", [])):
                    cs = cast(dict[str, Any], b.get("counts_by_severity", {}))
                    for k in totals:
                        totals[k] += cs.get(k, 0)
                if totals["error"] > 0 and totals["warn"] > 0 and totals["info"] > 0:
                    body = candidate
                    break
            time.sleep(2.0)

        assert body is not None, (
            f"histogram per-severity series for marker {marker!r} did not surface "
            f"within {POLL_BUDGET_S}s. Last response: {last_text[:500]}"
        )
        totals = {"error": 0, "warn": 0, "info": 0}
        for b in cast(list[dict[str, Any]], body["buckets"]):
            cs = cast(dict[str, Any], b["counts_by_severity"])
            for k in totals:
                totals[k] += cs.get(k, 0)
        # error=3 (severity error), warn=2 (severity warning->warn), info=5.
        assert totals["error"] == 3, f"expected 3 error lines, got {totals}"  # noqa: PLR2004
        assert totals["warn"] == 2, f"expected 2 warn lines, got {totals}"  # noqa: PLR2004
        assert totals["info"] == 5, f"expected 5 info lines, got {totals}"  # noqa: PLR2004
        assert body["bucket_duration_ms"] > 0
