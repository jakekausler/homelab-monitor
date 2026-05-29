"""Canonical end-to-end test for STAGE-001-021.

Exercises the FULL alert pipeline:

    fixture-host /control -> Prom metric series ->
        vmagent scrape -> VictoriaMetrics ->
            vmalert-metrics evaluation (FixtureHostHighCPU rule) ->
                Alertmanager fire -> webhook ->
                    monitor /api/alerts/ingest -> alerts table + SSE broker

Then the resolution path:

    fixture-host /control (cpu=5) -> rule no longer satisfied ->
        vmalert -> AM resolved notification ->
            monitor ingest -> mark_resolved + AlertResolvedEvent

Budget: 60s for fire, 60s for resolve. Total wall clock ~ 30-90s on a warm rig.

Requires the docker-compose.test.yml stack to be up. Skips automatically if
the monitor or fixture-host endpoints are unreachable.
"""

from __future__ import annotations

import pytest

from .helpers.rig import Rig
from .helpers.rig_health import require_rig_components


@pytest.mark.integration
@pytest.mark.slow
def test_fixture_host_high_cpu_canonical_e2e() -> None:
    """Set CPU=95 -> firing alert in /api/alerts -> set CPU=5 -> resolved + SSE event."""
    require_rig_components("monitor", "victoriametrics", "alertmanager")

    with Rig.boot() as rig:
        # Reset to baseline so the test doesn't accidentally see a left-over
        # alert from a prior test run.
        rig.set_fixture_cpu(5)

        # FIRE
        rig.set_fixture_cpu(95)
        alert = rig.wait_for_alert(
            "FixtureHostHighCPU",
            source_tool="vmalert-metrics",
            severity="warning",
            timeout_s=60.0,
        )
        assert alert["status"] == "firing"
        assert alert["labels"]["alertname"] == "FixtureHostHighCPU"
        assert alert["labels"]["host"] == "fixture-host"
        alert_id = alert["id"]

        # RESOLVE
        rig.set_fixture_cpu(5)
        resolved = rig.wait_for_resolution(alert_id, timeout_s=60.0)
        assert resolved["resolved_at"] is not None
        assert resolved["status"] == "resolved"

        # SSE: the resolved event MUST land on the broker.
        # Open a fresh SSE connection (replays last 50 events) so we don't
        # have to maintain a long-lived stream during the polling above.
        sse_payload = rig.wait_for_sse_event(
            "alert.resolved",
            timeout_s=30.0,
            match_alert_id=alert_id,
        )
        assert sse_payload["alert_id"] == alert_id
        assert sse_payload.get("source_tool") in {"vmalert-metrics", None}
        # `source_tool` may be omitted from the resolved payload depending on
        # the broker schema; assert presence-or-correct rather than required.
