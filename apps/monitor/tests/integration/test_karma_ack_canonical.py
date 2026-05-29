"""Integration test: silence created via AM v2 API silences a synthetic alert.

This is the integration-rig analog of the kernel-side silence behavior.
Karma's UI ultimately POSTs to AM /api/v2/silences; this test exercises
the AM silence API directly (Karma is not a programmatic API, it's a UI).
The kernel does not currently store silence state on the alert row, so the
assertion is on the AM side: the silence exists, matches the alert, and
AM no longer dispatches re-fires while the silence is active.

This validates the rig's AM + silence path is intact end-to-end.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from .helpers.rig import Rig
from .helpers.rig_health import require_rig_components

ALERTNAME = "RigSilenceTestAlert"
HOST_LABEL = "rig-silence-host"


def _post_firing_alert(am_url: str) -> None:
    resp = httpx.post(
        f"{am_url}/api/v2/alerts",
        json=[
            {
                "labels": {
                    "alertname": ALERTNAME,
                    "host": HOST_LABEL,
                    "severity": "warning",
                    "source_tool": "rig-test",
                },
                "annotations": {"summary": "rig silence test"},
                "generatorURL": "http://test/rig",
            }
        ],
        timeout=5.0,
    )
    assert resp.status_code == 200, resp.text  # noqa: PLR2004


def _create_silence(am_url: str, comment: str = "ACK! rig test") -> str:
    starts = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    ends = (
        (datetime.now(UTC) + timedelta(minutes=2))
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
    resp = httpx.post(
        f"{am_url}/api/v2/silences",
        json={
            "matchers": [
                {"name": "alertname", "value": ALERTNAME, "isRegex": False, "isEqual": True},
            ],
            "startsAt": starts,
            "endsAt": ends,
            "createdBy": "rig-canonical-ack-test",
            "comment": comment,
        },
        timeout=5.0,
    )
    assert resp.status_code == 200, resp.text  # noqa: PLR2004
    return str(resp.json()["silenceID"])


def _alert_is_silenced(am_url: str) -> bool:
    """Query AM /api/v2/alerts; return True iff the test alert has 'suppressed' state.

    AM's /api/v2/alerts response includes a `status.state` field that becomes
    `suppressed` when a matching silence is active.
    """
    resp = httpx.get(f"{am_url}/api/v2/alerts", timeout=5.0)
    if resp.status_code != 200:  # noqa: PLR2004
        return False
    for alert in resp.json():
        if alert.get("labels", {}).get("alertname") != ALERTNAME:
            continue
        state = alert.get("status", {}).get("state", "")
        if state == "suppressed":
            return True
    return False


@pytest.mark.integration
@pytest.mark.slow
def test_silence_suppresses_alert_in_am() -> None:
    """Fire alert into AM; create silence; assert AM transitions to suppressed."""
    require_rig_components("monitor", "alertmanager")

    with Rig.boot() as rig:
        am_url = rig.urls.alertmanager
        _post_firing_alert(am_url)

        # AM needs a tick to record the alert.
        time.sleep(2.0)

        _create_silence(am_url)

        # Wait up to 30s for AM to mark the alert as suppressed.
        deadline = time.time() + 30.0
        suppressed = False
        while time.time() < deadline:
            if _alert_is_silenced(am_url):
                suppressed = True
                break
            time.sleep(2.0)
        assert suppressed, "AM never marked the test alert as 'suppressed' after silence creation"
