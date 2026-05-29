"""Slow integration test: cadvisor sidecar exports container_* metrics into VM (STAGE-003-001).

Prerequisites:
    bash scripts/run-integration.sh  # brings up the full test rig.

The test rig's vmagent scrapes cadvisor at 5s intervals (test-fixtures/
vmagent-scrape.yaml). cadvisor scrapes ALL containers on the docker host —
including the fixture-host + noisy-logger test workloads.

Skips automatically when the rig isn't reachable.
"""

from __future__ import annotations

import os
import time

import httpx
import pytest

from .helpers.rig_health import require_rig_components


@pytest.mark.integration
@pytest.mark.slow
def test_cadvisor_exports_container_cpu_metric() -> None:
    """VM has at least one container_cpu_usage_seconds_total series within 30s."""
    require_rig_components("victoriametrics")

    vm_url = os.environ.get("VM_URL", "http://victoriametrics:8428").rstrip("/")

    # Poll up to 30s for the cadvisor series to land (vmagent scrape_interval=5s
    # in the test rig; allow some slack for first-scrape lag and storage flush).
    deadline = time.time() + 30
    last_result: list[object] = []
    while time.time() < deadline:
        r = httpx.get(
            f"{vm_url}/api/v1/query",
            params={"query": "container_cpu_usage_seconds_total"},
            timeout=5.0,
        )
        r.raise_for_status()
        last_result = r.json().get("data", {}).get("result", []) or []
        if last_result:
            break
        time.sleep(1.0)

    assert last_result, "no container_cpu_usage_seconds_total series in VM after 30s"


@pytest.mark.integration
@pytest.mark.slow
def test_cadvisor_relabel_drops_filesystem_noise() -> None:
    """No container_fs_*-series for tmpfs/overlay/shm/loop devices in VM."""
    require_rig_components("victoriametrics")

    vm_url = os.environ.get("VM_URL", "http://victoriametrics:8428").rstrip("/")

    # Allow ~15s for at least one scrape to land before asserting the drop rule.
    time.sleep(15)

    r = httpx.get(
        f"{vm_url}/api/v1/query",
        params={"query": 'container_fs_reads_bytes_total{device=~"overlay|tmpfs|shm|/dev/loop.*"}'},
        timeout=5.0,
    )
    r.raise_for_status()
    result: list[object] = r.json().get("data", {}).get("result", []) or []
    assert result == [], f"expected zero series after relabel drop, got: {result}"


@pytest.mark.integration
@pytest.mark.slow
def test_cadvisor_keep_rule_keeps_curated_families() -> None:
    """At least one series exists for each of three curated families."""
    require_rig_components("victoriametrics")

    vm_url = os.environ.get("VM_URL", "http://victoriametrics:8428").rstrip("/")

    time.sleep(15)

    for family in (
        "container_cpu_usage_seconds_total",
        "container_memory_working_set_bytes",
        "container_last_seen",
    ):
        r = httpx.get(
            f"{vm_url}/api/v1/query",
            params={"query": family},
            timeout=5.0,
        )
        r.raise_for_status()
        result: list[object] = r.json().get("data", {}).get("result", []) or []
        assert result, f"missing series for kept family: {family}"
