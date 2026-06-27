"""Integration test: /api/hb/{fingerprint}/* heartbeat receiver.

Asserts the token auth boundary (Spec A, STAGE-001-021) AND the real
heartbeat success path added by the EPIC-002 receiver: a valid rig token
posting ``/ok`` for a REGISTERED cron returns 204.

The success path requires the cron fingerprint to resolve, so the test
first registers a cron via ``POST /api/hb/{fingerprint}/register`` (the
idempotent wrapper handshake) and then posts ``/ok`` to that fingerprint.
"""

from __future__ import annotations

import hashlib
import json

import httpx
import pytest

from .helpers.rig import Rig
from .helpers.rig_health import require_rig_components

_HTTP_CREATED = 201
_HTTP_NO_CONTENT = 204
_HTTP_NOT_FOUND = 404
_HTTP_UNAUTHORIZED = 401


def _compute_fingerprint(*, host: str, source_path: str | None, schedule: str, command: str) -> str:
    """Mirror ``kernel.cron.fingerprint.compute_fingerprint`` for the URL segment.

    The server recomputes the fingerprint from the register body and rejects
    with 422 on mismatch, so the test must derive the same value the same way:
    SHA256 of the canonical JSON of the identity tuple.
    """
    payload = json.dumps(
        {
            "host": host,
            "source_path": source_path,
            "schedule": schedule,
            "command": command,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


@pytest.mark.integration
def test_heartbeat_returns_401_without_token() -> None:
    """POST /api/hb/<fingerprint>/ok without Authorization returns 401."""
    require_rig_components("monitor")

    with Rig.boot() as rig:
        resp = httpx.post(f"{rig.urls.monitor}/api/hb/0123456789abcdef/ok", json={}, timeout=5.0)
    assert resp.status_code == _HTTP_UNAUTHORIZED


@pytest.mark.integration
def test_heartbeat_returns_404_for_unknown_fingerprint() -> None:
    """POST /api/hb/<unknown>/ok with a valid token returns 404 (cron not resolved).

    Proves the token auth passes (not 401/403) but the cron-resolution gate
    rejects an unregistered fingerprint before any state write.
    """
    require_rig_components("monitor")

    with Rig.boot() as rig:
        resp = httpx.post(
            f"{rig.urls.monitor}/api/hb/0123456789abcdef/ok",
            json={},
            headers={"Authorization": f"Bearer {rig.token}"},
            timeout=5.0,
        )
    assert resp.status_code == _HTTP_NOT_FOUND


@pytest.mark.integration
def test_heartbeat_returns_204_with_rig_token() -> None:
    """POST /ok for a REGISTERED cron with the rig's HEARTBEAT_WRITE token returns 204.

    Registers a cron first (so the fingerprint resolves), then posts the
    success ping. This exercises the real heartbeat success path, not the
    pre-EPIC-002 auth-boundary stub.
    """
    require_rig_components("monitor")

    identity = {
        "host": "homelab-host",
        "source_path": "/etc/crontab",
        "schedule": "*/5 * * * *",
        "command": "/usr/local/bin/heartbeat-path-integration-test.sh",
    }
    fingerprint = _compute_fingerprint(**identity)

    with Rig.boot() as rig:
        headers = {"Authorization": f"Bearer {rig.token}"}
        # Idempotent register handshake — 201 (new) or 200 (already present).
        register = httpx.post(
            f"{rig.urls.monitor}/api/hb/{fingerprint}/register",
            json={**identity, "wrapper": True},
            headers=headers,
            timeout=5.0,
        )
        assert register.status_code in (_HTTP_CREATED, 200), register.text
        # Now the fingerprint resolves: the /ok success path returns 204.
        resp = httpx.post(
            f"{rig.urls.monitor}/api/hb/{fingerprint}/ok",
            json={"any": "payload"},
            headers=headers,
            timeout=5.0,
        )
    assert resp.status_code == _HTTP_NO_CONTENT, resp.text
