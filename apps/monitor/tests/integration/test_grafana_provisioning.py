"""Integration tests for Grafana provisioning (STAGE-001-020).

Tests run against a real Grafana sidecar from docker-compose.test.yml.
The test rig uses GF_SERVER_SERVE_FROM_SUB_PATH=false and hits Grafana
directly at http://127.0.0.1:3000, NOT through the monitor's reverse
proxy. End-to-end sub-path forwarding (the production behavior) is
validated in STAGE-001-021's `make dev-prod` rig.
"""

from __future__ import annotations

import os
import time
from typing import Any

import httpx
import pytest

GRAFANA_URL = os.environ.get("GRAFANA_URL", "http://grafana:3000").rstrip("/")
PLUGIN_INSTALL_TIMEOUT_S = 90.0


def _wait_for_health(deadline: float) -> bool:
    """Poll /api/health until 200 or deadline. Return True on success."""
    while time.time() < deadline:
        try:
            r = httpx.get(f"{GRAFANA_URL}/api/health", timeout=3.0)
            if r.status_code == 200:  # noqa: PLR2004
                return True
        except httpx.HTTPError:
            pass
        time.sleep(2.0)
    return False


@pytest.mark.integration
@pytest.mark.slow
def test_grafana_health_endpoint_returns_200() -> None:
    """Grafana's /api/health responds 200 after first-boot plugin install."""
    deadline = time.time() + PLUGIN_INSTALL_TIMEOUT_S
    if not _wait_for_health(deadline):
        pytest.skip(f"Grafana not reachable at {GRAFANA_URL} — start docker-compose.test.yml")

    resp = httpx.get(f"{GRAFANA_URL}/api/health", timeout=5.0)
    assert resp.status_code == 200  # noqa: PLR2004


@pytest.mark.integration
@pytest.mark.slow
def test_grafana_datasources_provisioned() -> None:
    """Both VictoriaMetrics and VictoriaLogs datasources are loaded by provisioning.

    Uses anonymous Viewer access (compose env GF_AUTH_ANONYMOUS_ENABLED=true).
    """
    deadline = time.time() + PLUGIN_INSTALL_TIMEOUT_S
    if not _wait_for_health(deadline):
        pytest.skip(f"Grafana not reachable at {GRAFANA_URL}")

    resp = httpx.get(f"{GRAFANA_URL}/api/datasources", timeout=5.0)
    assert resp.status_code == 200  # noqa: PLR2004
    datasources: list[dict[str, Any]] = resp.json()
    names = {d["name"] for d in datasources}
    assert "VictoriaMetrics" in names
    assert "VictoriaLogs" in names
    # Sanity: VM must be the default
    vm = next(d for d in datasources if d["name"] == "VictoriaMetrics")
    assert vm["isDefault"] is True
    assert vm["type"] == "prometheus"
    vl = next(d for d in datasources if d["name"] == "VictoriaLogs")
    assert vl["type"] == "victoriametrics-logs-datasource"


@pytest.mark.integration
@pytest.mark.slow
def test_grafana_host_overview_dashboard_provisioned() -> None:
    """The host-overview dashboard JSON is loaded with the expected UID and panels."""
    deadline = time.time() + PLUGIN_INSTALL_TIMEOUT_S
    if not _wait_for_health(deadline):
        pytest.skip(f"Grafana not reachable at {GRAFANA_URL}")

    resp = httpx.get(f"{GRAFANA_URL}/api/dashboards/uid/host-overview", timeout=5.0)
    assert resp.status_code == 200, resp.text  # noqa: PLR2004
    body = resp.json()
    dash = body["dashboard"]
    assert dash["uid"] == "host-overview"
    panels = dash.get("panels", [])
    # Expect at least 4 panels (CPU, memory, disk, network)
    assert len(panels) >= 4, f"expected >= 4 panels, got {len(panels)}"  # noqa: PLR2004
    # Tags include both 'homelab-monitor' and 'host'
    tags = set(dash.get("tags", []))
    assert "homelab-monitor" in tags
    assert "host" in tags
