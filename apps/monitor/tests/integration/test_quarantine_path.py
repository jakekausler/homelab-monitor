"""Integration test: quarantine alert reaches /api/alerts via the ingest path.

Posts a synthetic Alertmanager-v2 payload for a `source_tool=scheduler`
quarantine alert, asserts it lands in /api/alerts with the correct shape.

WHY NOT TRIP A REAL COLLECTOR: forcing a real builtin collector to fail
requires injecting a config that points at a non-existent socket / file
INTO the running monitor container, which adds rig surface area for one
test. The unit test apps/monitor/tests/test_scheduler_quarantine_alert.py
already exhaustively covers the FailureBudget._emit_quarantine_alert ->
AlertDispatcher -> AlertRepository wire-up. This integration test verifies
the _ingest endpoint path is exercisable end-to-end with the rig token.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest

from .helpers.rig import Rig
from .helpers.rig_health import require_rig_components


@pytest.mark.integration
def test_synthetic_quarantine_alert_lands_in_alerts_list() -> None:
    """POST /api/alerts/ingest with a quarantine-shaped payload; expect it in GET /api/alerts."""
    require_rig_components("monitor")

    with Rig.boot() as rig:
        now = datetime.now(UTC).isoformat()
        # Synthetic AM v2 payload with the labels the scheduler uses for
        # quarantine emission (see kernel/scheduler/failure_budget.py).
        payload: dict[str, object] = {
            "version": "4",
            "groupKey": "{}:{}",
            "status": "firing",
            "receiver": "default",
            "groupLabels": {},
            "commonLabels": {},
            "commonAnnotations": {},
            "externalURL": "http://localhost:9093",
            "alerts": [
                {
                    "status": "firing",
                    "labels": {
                        "alertname": "CollectorQuarantined",
                        "severity": "warning",
                        "source_tool": "scheduler",
                        "target_kind": "collector",
                        "collector_name": "rig_synthetic_quarantine",
                        "reason": "exception",
                    },
                    "annotations": {
                        "summary": "Synthetic quarantine for rig integration test",
                    },
                    "startsAt": now,
                    "endsAt": "0001-01-01T00:00:00Z",
                    "generatorURL": "http://test/rig",
                    "fingerprint": "rig-synthetic-quarantine-fp",
                },
            ],
        }
        resp = httpx.post(
            f"{rig.urls.monitor}/api/alerts/ingest",
            json=payload,
            headers={"Authorization": f"Bearer {rig.token}"},
            timeout=10.0,
        )
        assert resp.status_code == 202, resp.text  # noqa: PLR2004
        body = resp.json()
        assert body.get("ingested") == 1

        # Fetch list and find the synthetic alert.
        list_resp = rig.get("/api/alerts?status=firing&source_tool=scheduler&limit=50")
        assert list_resp.status_code == 200  # noqa: PLR2004
        items = list_resp.json().get("items", [])
        match = next(
            (
                a
                for a in items
                if a.get("labels", {}).get("collector_name") == "rig_synthetic_quarantine"
            ),
            None,
        )
        assert match is not None, (
            "synthetic quarantine alert did not surface in /api/alerts. "
            f"Listed items: {json.dumps(items, indent=2)[:1000]}"
        )
        assert match["source_tool"] == "scheduler"
        assert match["severity"] == "warning"
