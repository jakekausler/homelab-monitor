"""Smoke test: verify the integration test rig actually has VictoriaMetrics reachable.

This test requires the docker-compose.test.yml stack (or a host VM at $VM_URL).
It is excluded from the default test run by the
`-m "not integration"` / `--ignore=tests/integration` config in pyproject.toml.

Run with:
    bash scripts/run-integration.sh                    # full compose
    docker compose -f .../docker-compose.test.yml up -d victoriametrics
    VM_URL=http://localhost:8428 pytest tests/integration/
"""

from __future__ import annotations

import os

import httpx
import pytest

from .helpers.rig_health import require_rig_components


@pytest.mark.integration
def test_vm_reachable() -> None:
    """`VM_URL/health` returns 200 OK."""
    require_rig_components("victoriametrics")

    vm_url = os.environ.get("VM_URL", "http://victoriametrics:8428").rstrip("/")
    resp = httpx.get(f"{vm_url}/health", timeout=5.0)
    assert resp.status_code == 200  # noqa: PLR2004
