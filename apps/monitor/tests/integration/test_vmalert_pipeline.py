"""Slow integration test: vmalert + Alertmanager containers parse our configs.

Prerequisites:
    docker compose -f deploy/compose/docker-compose.test.yml up -d alertmanager vmalert-metrics

Skips automatically when the sidecars aren't reachable, so `make test` and
`make test-fast` (which don't run the docker compose stack) keep working.
"""

from __future__ import annotations

import os

import httpx
import pytest


@pytest.mark.integration
@pytest.mark.slow
def test_alertmanager_accepts_rendered_config() -> None:
    """AM /-/healthy returns 200 — i.e., the static test fixture parses cleanly."""
    am_url = os.environ.get("AM_URL", "http://alertmanager:9093").rstrip("/")
    try:
        resp = httpx.get(f"{am_url}/-/healthy", timeout=5.0)
    except httpx.HTTPError:
        pytest.skip(f"AM not reachable at {am_url} — start docker-compose.test.yml")
    assert resp.status_code == 200  # noqa: PLR2004


@pytest.mark.integration
@pytest.mark.slow
def test_vmalert_loads_host_rules() -> None:
    """vmalert /api/v1/rules lists our host rules — i.e., host.yaml parses cleanly."""
    vmalert_url = os.environ.get("VMALERT_URL", "http://vmalert-metrics:8880").rstrip("/")
    try:
        resp = httpx.get(f"{vmalert_url}/api/v1/rules", timeout=5.0)
    except httpx.HTTPError:
        pytest.skip(f"vmalert not reachable at {vmalert_url} — start docker-compose.test.yml")
    assert resp.status_code == 200  # noqa: PLR2004
    data = resp.json()
    rule_names = {
        rule["name"]
        for group in data.get("data", {}).get("groups", [])
        for rule in group.get("rules", [])
    }
    assert {"HostHighCPU", "HostHighMemory", "CollectorQuarantined"}.issubset(rule_names)
