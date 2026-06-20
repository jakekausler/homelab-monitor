"""Slow integration test: vmalert-logs UDM (Unifi) pipeline end-to-end.

Mirrors test_vmalert_logs_pipeline.py. Brings up docker-compose.test.yml stack
(VL + AM + vmalert-logs-test + test webhook receiver) then plants structured UDM
syslog records via VL ingest (bypassing vector — the udm_parse pipeline is tested
separately). Asserts the unifi_logs rule group loads (all 8 rules) and the 4
confirmed-real rules fire → reach the test webhook with integration=unifi labels.

The 4 UNVALIDATED rules (UnifiAdminLoginExternalLog, UnifiPortFlapLog,
UnifiDeviceDisconnectLog, UnifiFirmwareEventLog) are NOT planted for — only their
presence/load is asserted (live-firing validation is a Refinement regression item).

Marked @pytest.mark.integration + @pytest.mark.slow — runs only on `make integration`;
deselected by `make verify` (-m 'not integration') and `make test-fast`.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from .helpers.rig_health import require_rig_components
from .helpers.vl_planter import plant_log_lines

VL_URL = os.environ.get("VL_URL", "http://victorialogs:9428").rstrip("/")
VMALERT_LOGS_URL = os.environ.get("VMALERT_LOGS_URL", "http://vmalert-logs-test:8880").rstrip("/")
AM_URL = os.environ.get("AM_URL", "http://alertmanager:9093").rstrip("/")
WEBHOOK_RECEIVED_FILE = Path("/tmp/received-alerts.jsonl")

# Timing budget (test windows: 30s):
#   - vmalert eval interval: 5s (vmalert-logs-test)
#   - LogsQL window: 30s (rule expr `_time:30s`)
#   - vmalert detects firing → notifies AM: ~5-10s after lines ingested
#   - AM group_wait: 30s
#   - AM → webhook POST: ~1-2s
# Worst-case: 30s window + 5s eval + 30s group_wait + 5s buffer = ~70s.
WAIT_FOR_ALERT_S = 90

# All 8 unifi_logs rules — asserted present/loaded at /api/v1/rules.
EXPECTED_UNIFI_LOG_RULES = {
    "UnifiAdminLoginLog",
    "UnifiAdminLoginExternalLog",
    "UnifiConfigChangeLog",
    "UnifiWanBlockSpikeLog",
    "UnifiOomPressureLog",
    "UnifiPortFlapLog",
    "UnifiDeviceDisconnectLog",
    "UnifiFirmwareEventLog",
}


def _start_test_webhook() -> subprocess.Popen[bytes]:
    """Spawn the webhook receiver subprocess (see helpers/test_webhook_server.py).

    The receiver appends every POSTed alerts payload to WEBHOOK_RECEIVED_FILE
    and returns 202. Auth is accepted permissively (any Bearer or none).
    """
    WEBHOOK_RECEIVED_FILE.unlink(missing_ok=True)
    return subprocess.Popen(
        [sys.executable, "-m", "tests.integration.helpers.test_webhook_server"],
        cwd=str(Path(__file__).resolve().parent.parent.parent),
    )


def _wait_for_alert_with_label(
    label_key: str, label_val: str, alertname: str, timeout_s: int
) -> dict[str, Any] | None:
    """Poll WEBHOOK_RECEIVED_FILE; return first matching alert payload or None."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if WEBHOOK_RECEIVED_FILE.exists():
            for line in WEBHOOK_RECEIVED_FILE.read_text().splitlines():
                if not line.strip():
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:  # pragma: no cover -- defensive
                    continue
                for alert in payload.get("alerts", []):
                    if (
                        alert.get("labels", {}).get(label_key) == label_val
                        and alert.get("labels", {}).get("alertname") == alertname
                    ):
                        return alert
        time.sleep(2)
    return None


@pytest.fixture(scope="module")
def test_webhook() -> Iterator[None]:
    """Module-scoped: start the test webhook receiver once for all tests in this file."""
    # Skip if VL/vmalert/AM aren't reachable.
    require_rig_components("victorialogs", "vmalert-logs", "alertmanager")

    proc = _start_test_webhook()
    try:
        # Wait for receiver to bind port 9090.
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                httpx.get("http://localhost:9090/openapi.json", timeout=1.0)
                break
            except httpx.HTTPError:
                time.sleep(0.2)
        else:  # pragma: no cover -- defensive
            msg = "test webhook receiver did not start within 10s"
            raise RuntimeError(msg)
        yield
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover -- defensive
            proc.kill()
            proc.wait(timeout=5)
        WEBHOOK_RECEIVED_FILE.unlink(missing_ok=True)


@pytest.mark.integration
@pytest.mark.slow
def test_vmalert_logs_loads_unifi_rules() -> None:
    """vmalert-logs /api/v1/rules lists all 8 unifi_logs rules, each health:'ok'."""
    require_rig_components("vmalert-logs")

    resp = httpx.get(f"{VMALERT_LOGS_URL}/api/v1/rules", timeout=5.0)
    assert resp.status_code == 200  # noqa: PLR2004

    groups = resp.json().get("data", {}).get("groups", [])
    unifi_rules = [
        rule
        for group in groups
        if group.get("name") == "unifi_logs"
        for rule in group.get("rules", [])
    ]
    rule_names = {rule["name"] for rule in unifi_rules}
    assert EXPECTED_UNIFI_LOG_RULES.issubset(rule_names), (
        f"missing unifi_logs rules; got {rule_names}"
    )
    # Every loaded unifi rule must be healthy (no LogsQL parse/eval error).
    unhealthy = [
        rule["name"]
        for rule in unifi_rules
        if rule.get("health") not in (None, "ok") or rule.get("lastError")
    ]
    assert not unhealthy, f"unifi_logs rules with non-ok health: {unhealthy}"


@pytest.mark.integration
@pytest.mark.slow
def test_unifi_admin_login_log_fires(test_webhook: None) -> None:
    """Plant a CEF-544 audit login record; UnifiAdminLoginLog fires within 90s."""
    plant_log_lines(
        host="udm-pro",
        service="udm-audit",
        severity="info",
        message="User admin Network Accessed",
        count=1,
        extra_fields={
            "cef_signature_id": "544",
            "udm_admin": "admin",
            "src": "192.168.2.38",
        },
        vl_url=VL_URL,
    )
    alert = _wait_for_alert_with_label("host", "udm-pro", "UnifiAdminLoginLog", WAIT_FOR_ALERT_S)
    assert alert is not None, f"UnifiAdminLoginLog never reached webhook within {WAIT_FOR_ALERT_S}s"
    assert alert["labels"]["source_tool"] == "vmalert-logs"
    assert alert["labels"]["integration"] == "unifi"
    assert alert["labels"]["category"] == "network"
    assert alert["labels"]["severity"] == "info"


@pytest.mark.integration
@pytest.mark.slow
def test_unifi_config_change_log_fires(test_webhook: None) -> None:
    """Plant a CEF-546 config-change record; UnifiConfigChangeLog fires within 90s."""
    plant_log_lines(
        host="udm-pro",
        service="udm-audit",
        severity="info",
        message="User admin Config Modified",
        count=1,
        extra_fields={
            "cef_signature_id": "546",
            "udm_admin": "admin",
            "udm_settings_section": "firewall",
        },
        vl_url=VL_URL,
    )
    alert = _wait_for_alert_with_label("host", "udm-pro", "UnifiConfigChangeLog", WAIT_FOR_ALERT_S)
    assert alert is not None, "UnifiConfigChangeLog never reached webhook"
    assert alert["labels"]["source_tool"] == "vmalert-logs"
    assert alert["labels"]["integration"] == "unifi"
    assert alert["labels"]["severity"] == "info"


@pytest.mark.integration
@pytest.mark.slow
def test_unifi_wan_block_spike_log_fires(test_webhook: None) -> None:
    """Plant 3 udm-firewall WAN-block records from one src (twin threshold >2)."""
    plant_log_lines(
        host="udm-pro",
        service="udm-firewall",
        severity="warning",
        message="WAN_LOCAL-D-1234 DROP",
        count=3,
        extra_fields={
            "fw_chain": "WAN_LOCAL-D-1234",
            "src": "203.0.113.7",
        },
        vl_url=VL_URL,
    )
    alert = _wait_for_alert_with_label(
        "src", "203.0.113.7", "UnifiWanBlockSpikeLog", WAIT_FOR_ALERT_S
    )
    assert alert is not None, "UnifiWanBlockSpikeLog never reached webhook"
    assert alert["labels"]["source_tool"] == "vmalert-logs"
    assert alert["labels"]["integration"] == "unifi"
    assert alert["labels"]["severity"] == "warning"


@pytest.mark.integration
@pytest.mark.slow
def test_unifi_oom_pressure_log_fires(test_webhook: None) -> None:
    """Plant an earlyoom udm-system kill record ('sending SIGTERM' in _msg)."""
    plant_log_lines(
        host="udm-pro",
        service="udm-system",
        severity="warning",
        message="earlyoom: sending SIGTERM to process 4242",
        count=1,
        extra_fields={"process": "earlyoom"},
        vl_url=VL_URL,
    )
    alert = _wait_for_alert_with_label("host", "udm-pro", "UnifiOomPressureLog", WAIT_FOR_ALERT_S)
    assert alert is not None, "UnifiOomPressureLog never reached webhook"
    assert alert["labels"]["source_tool"] == "vmalert-logs"
    assert alert["labels"]["integration"] == "unifi"
    assert alert["labels"]["severity"] == "warning"
