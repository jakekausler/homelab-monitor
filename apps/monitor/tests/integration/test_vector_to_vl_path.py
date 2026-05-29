"""Integration test: noisy-logger -> vector -> VictoriaLogs -> /api/logs/query.

Plant a UNIQUE log line via noisy-logger; assert it surfaces in the monitor's
/api/logs/query within 30s. Verifies the docker_logs vector source actually
tails the right container and ships to VL.
"""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from .helpers.rig import Rig
from .helpers.rig_health import require_rig_components

VECTOR_LATENCY_BUDGET_S = 30.0


@pytest.mark.integration
@pytest.mark.slow
def test_log_line_via_noisy_logger_reaches_logs_query() -> None:
    """Plant a unique line; poll /api/logs/query until it surfaces or 30s elapses."""
    require_rig_components("monitor", "victorialogs", "noisy-logger")

    marker = f"rig-test-{uuid.uuid4().hex}"
    with Rig.boot() as rig:
        rig.plant_log_via_noisy_logger(marker)

        # Query window: now-1m to now+1m to absorb clock skew between fixture
        # container and the monitor. LogsQL phrase filter on the marker.
        deadline = time.time() + VECTOR_LATENCY_BUDGET_S
        found = False
        last_resp_text = ""
        while time.time() < deadline:
            now = datetime.now(UTC)
            start = (now - timedelta(minutes=1)).isoformat()
            end = (now + timedelta(minutes=1)).isoformat()
            resp = rig.get(
                "/api/logs/query",
                params={"expr": f'"{marker}"', "start": start, "end": end, "limit": "50"},
            )
            if resp.status_code == 200:  # noqa: PLR2004
                last_resp_text = resp.text
                lines = resp.json().get("lines", [])
                if any(marker in line.get("message", "") for line in lines):
                    found = True
                    break
            time.sleep(2.0)

        assert found, (
            f"marker {marker!r} did not appear in /api/logs/query within "
            f"{VECTOR_LATENCY_BUDGET_S}s. Last response body: {last_resp_text[:500]}"
        )
