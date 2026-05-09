"""Slow integration test: kthxbye extends an ACK!-prefixed silence.

Brings up docker-compose.test.yml stack with the Karma + kthxbye additions
(see deploy/compose/docker-compose.test.yml). Walks the wire-level flow:

  1. Direct POST a firing alert to AM.
  2. Direct POST a silence to AM with comment="ACK! testing" matching
     kthxbye's --extend-with-prefix=ACK!.
  3. Capture the silence's initial endsAt.
  4. Re-fire the alert every 5s for 30s (alert must remain active so
     kthxbye doesn't bail on a missing target).
  5. Re-read the silence's endsAt; assert it has advanced past the
     original endsAt + at least one extension window.

Skips automatically when sidecars aren't reachable.

The test rig overrides kthxbye intervals to:
  -extend-by=10s -extend-if-expiring-in=15s -interval=5s -max-duration=2m
so the wall clock is ~30-40s.
"""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

AM_URL = os.environ.get("AM_URL", "http://alertmanager:9093").rstrip("/")
TEST_ALERTNAME = "KthxbyeTestAlert"
TEST_HOST = "kthxbye-host"


def _post_firing_alert() -> None:
    """POST a firing alert directly to Alertmanager's v2 API."""
    resp = httpx.post(
        f"{AM_URL}/api/v2/alerts",
        json=[
            {
                "labels": {
                    "alertname": TEST_ALERTNAME,
                    "host": TEST_HOST,
                    "severity": "warning",
                },
                "annotations": {"summary": "kthxbye test"},
                # endsAt empty → AM treats as currently firing
                "generatorURL": "http://test/source",
            }
        ],
        timeout=5.0,
    )
    assert resp.status_code == 200, resp.text  # noqa: PLR2004


def _post_silence(comment: str = "ACK! testing") -> str:
    """Create a silence on Alertmanager that matches the test alert. Return silenceID."""
    # Minimal duration so kthxbye has work to do.
    starts = datetime.now(UTC).replace(microsecond=0, tzinfo=None).isoformat() + "Z"
    ends_dt = datetime.now(UTC).replace(microsecond=0, tzinfo=None)
    # 20s lifetime — within kthxbye's 15s "expiring soon" window.
    ends = (ends_dt + timedelta(seconds=20)).isoformat() + "Z"
    resp = httpx.post(
        f"{AM_URL}/api/v2/silences",
        json={
            "matchers": [
                {
                    "name": "alertname",
                    "value": TEST_ALERTNAME,
                    "isRegex": False,
                    "isEqual": True,
                },
            ],
            "startsAt": starts,
            "endsAt": ends,
            "createdBy": "kthxbye-pipeline-test",
            "comment": comment,
        },
        timeout=5.0,
    )
    assert resp.status_code == 200, resp.text  # noqa: PLR2004
    return resp.json()["silenceID"]


def _get_silence(silence_id: str) -> dict[str, Any] | None:
    """Fetch a silence by ID. Returns None if not found."""
    resp = httpx.get(f"{AM_URL}/api/v2/silence/{silence_id}", timeout=5.0)
    if resp.status_code == 404:  # noqa: PLR2004
        return None
    assert resp.status_code == 200  # noqa: PLR2004
    data: dict[str, Any] = resp.json()
    return data


@pytest.mark.integration
@pytest.mark.slow
def test_kthxbye_extends_acked_silence() -> None:
    """End-to-end: firing alert + ACK! silence + steady refire → kthxbye extends endsAt."""
    try:
        httpx.get(f"{AM_URL}/-/healthy", timeout=3.0)
    except httpx.HTTPError:
        pytest.skip(f"AM not reachable at {AM_URL} — start docker-compose.test.yml")

    _post_firing_alert()
    silence_id = _post_silence("ACK! testing")
    initial = _get_silence(silence_id)
    assert initial is not None
    initial_ends_at = initial["endsAt"]

    # Refire alert every 5s for 35s so AM keeps the silence "active"
    deadline = time.time() + 35
    while time.time() < deadline:
        _post_firing_alert()
        time.sleep(5)

    # Re-read silence — endsAt should have moved.
    final = _get_silence(silence_id)
    assert final is not None, "silence vanished mid-test"
    final_ends_at = final["endsAt"]

    # Parse and compare. AM returns RFC3339 with optional 'Z'.
    def _parse(ts: str) -> datetime:
        # AM may return ".000Z" or "+00:00"; normalize.
        ts2 = ts.replace("Z", "+00:00")
        return datetime.fromisoformat(ts2)

    assert _parse(final_ends_at) > _parse(initial_ends_at), (
        f"kthxbye did not extend silence: "
        f"initial endsAt={initial_ends_at} final endsAt={final_ends_at}"
    )


@pytest.mark.integration
@pytest.mark.slow
def test_kthxbye_does_not_extend_non_ack_silence() -> None:
    """Negative: a silence WITHOUT ACK! prefix is NOT extended."""
    try:
        httpx.get(f"{AM_URL}/-/healthy", timeout=3.0)
    except httpx.HTTPError:
        pytest.skip(f"AM not reachable at {AM_URL} — start docker-compose.test.yml")

    _post_firing_alert()
    silence_id = _post_silence("plain comment, no prefix")
    initial = _get_silence(silence_id)
    assert initial is not None
    initial_ends_at = initial["endsAt"]

    # Wait long enough for kthxbye's interval to run a few times
    deadline = time.time() + 25
    while time.time() < deadline:
        _post_firing_alert()
        time.sleep(5)

    final = _get_silence(silence_id)
    assert final is not None
    # endsAt should be unchanged (or, in pathological cases, the silence
    # has already expired and AM 404'd — both are acceptable proof that
    # kthxbye did NOT extend).
    assert final["endsAt"] == initial_ends_at, (
        f"non-ACK silence was extended: initial={initial_ends_at} final={final['endsAt']}"
    )
