"""Integration test: /api/hb/{path} requires a valid token (STAGE-001-021).

Asserts the auth boundary added by Spec A. The full heartbeat ingest
behavior (persisting beats, age-out alerts) lands in EPIC-002.
"""

from __future__ import annotations

import httpx
import pytest

from .helpers.rig import Rig


@pytest.mark.integration
def test_heartbeat_returns_401_without_token() -> None:
    """POST /api/hb/test/ok without Authorization returns 401."""
    try:
        with Rig.boot() as rig:
            resp = httpx.post(f"{rig.urls.monitor}/api/hb/test/ok", json={}, timeout=5.0)
    except (httpx.HTTPError, RuntimeError, TimeoutError) as exc:
        pytest.skip(f"rig not reachable: {exc}")
        return  # for type narrowing
    assert resp.status_code == 401  # noqa: PLR2004


@pytest.mark.integration
def test_heartbeat_returns_204_with_rig_token() -> None:
    """POST /api/hb/test/ok with the rig's HEARTBEAT_WRITE token returns 204."""
    try:
        with Rig.boot() as rig:
            resp = httpx.post(
                f"{rig.urls.monitor}/api/hb/test/ok",
                json={"any": "payload"},
                headers={"Authorization": f"Bearer {rig.token}"},
                timeout=5.0,
            )
    except (httpx.HTTPError, RuntimeError, TimeoutError) as exc:
        pytest.skip(f"rig not reachable: {exc}")
        return
    assert resp.status_code == 204  # noqa: PLR2004
