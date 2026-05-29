"""Integration test: /api/hb/{path} requires a valid token (STAGE-001-021).

Asserts the auth boundary added by Spec A. The full heartbeat ingest
behavior (persisting beats, age-out alerts) lands in EPIC-002.
"""

from __future__ import annotations

import httpx
import pytest

from .helpers.rig import Rig
from .helpers.rig_health import require_rig_components


@pytest.mark.integration
def test_heartbeat_returns_401_without_token() -> None:
    """POST /api/hb/test/ok without Authorization returns 401."""
    require_rig_components("monitor")

    with Rig.boot() as rig:
        resp = httpx.post(f"{rig.urls.monitor}/api/hb/test/ok", json={}, timeout=5.0)
    assert resp.status_code == 401  # noqa: PLR2004


@pytest.mark.integration
def test_heartbeat_returns_204_with_rig_token() -> None:
    """POST /api/hb/test/ok with the rig's HEARTBEAT_WRITE token returns 204."""
    require_rig_components("monitor")

    with Rig.boot() as rig:
        resp = httpx.post(
            f"{rig.urls.monitor}/api/hb/test/ok",
            json={"any": "payload"},
            headers={"Authorization": f"Bearer {rig.token}"},
            timeout=5.0,
        )
    assert resp.status_code == 204  # noqa: PLR2004
