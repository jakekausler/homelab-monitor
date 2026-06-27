"""Slow integration test: vmalert-logs Synology DSM pipeline end-to-end.

Mirrors test_vmalert_pihole_logs_pipeline.py / test_vmalert_unifi_logs_pipeline.py.
Brings up docker-compose.test.yml stack (VL + AM + vmalert-logs-test + test
webhook receiver) then plants DSM log lines via VL ingest (bypassing the vector
syslog pipeline, which is tested separately). Asserts the synology_logs rule
group loads (all 4 rules, health 'ok') and each rule fires on matching planted
line(s) -> reaches the test webhook with integration=synology labels.

LogsQL phrase-match is TOKEN-based (not substring): `"fail"` does NOT match the
token `failed`. SynologyLogErrorRate therefore matches the exact tokens
`failed` / `failure` / `critical`; the planted error-rate lines contain those
tokens verbatim so the rule provably fires (threshold >2 in the test mirror).

Marked @pytest.mark.integration + @pytest.mark.slow — runs only on `make
integration`; deselected by `make verify` (-m 'not integration') and
`make test-fast`.
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
#   - vmalert detects firing -> notifies AM: ~5-10s after lines ingested
#   - AM group_wait: 30s
#   - AM -> webhook POST: ~1-2s
# Worst-case: 30s window + 5s eval + 30s group_wait + 5s buffer = ~70s.
WAIT_FOR_ALERT_S = 90

# All 4 synology_logs rules — asserted present/loaded at /api/v1/rules.
EXPECTED_SYNOLOGY_LOG_RULES = {
    "SynologyFailedLoginBurst",
    "SynologySmartEventLog",
    "SynologyPackageEventLog",
    "SynologyLogErrorRate",
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
def test_vmalert_logs_loads_synology_rules() -> None:
    """vmalert-logs /api/v1/rules lists all 4 synology_logs rules, each health:'ok'."""
    require_rig_components("vmalert-logs")

    resp = httpx.get(f"{VMALERT_LOGS_URL}/api/v1/rules", timeout=5.0)
    assert resp.status_code == 200  # noqa: PLR2004

    groups = resp.json().get("data", {}).get("groups", [])
    synology_rules = [
        rule
        for group in groups
        if group.get("name") == "synology_logs"
        for rule in group.get("rules", [])
    ]
    rule_names = {rule["name"] for rule in synology_rules}
    assert EXPECTED_SYNOLOGY_LOG_RULES.issubset(rule_names), (
        f"missing synology_logs rules; got {rule_names}"
    )
    # Every loaded synology rule must be healthy (no LogsQL parse/eval error).
    unhealthy = [
        rule["name"]
        for rule in synology_rules
        if rule.get("health") not in (None, "ok") or rule.get("lastError")
    ]
    assert not unhealthy, f"synology_logs rules with non-ok health: {unhealthy}"


@pytest.mark.integration
@pytest.mark.slow
def test_synology_failed_login_burst_fires(test_webhook: None) -> None:
    """Plant 3 DSM Connection failed-sign-in lines; SynologyFailedLoginBurst fires."""
    plant_log_lines(
        host="NAS",
        service="synology-auth",
        severity="warning",
        message=(
            "Connection: User [REDACTED] from [REDACTED] failed to sign in to [DSM] via [REDACTED]."
        ),
        count=3,
        extra_fields={"source_type": "synology"},
        vl_url=VL_URL,
    )
    alert = _wait_for_alert_with_label("host", "NAS", "SynologyFailedLoginBurst", WAIT_FOR_ALERT_S)
    assert alert is not None, (
        f"SynologyFailedLoginBurst never reached webhook within {WAIT_FOR_ALERT_S}s"
    )
    assert alert["labels"]["source_tool"] == "vmalert-logs"
    assert alert["labels"]["integration"] == "synology"
    assert alert["labels"]["severity"] == "warning"


@pytest.mark.integration
@pytest.mark.slow
def test_synology_smart_event_log_fires(test_webhook: None) -> None:
    """Plant a DSM SMART line; SynologySmartEventLog fires within 90s."""
    plant_log_lines(
        host="NAS",
        service="synology-smart",
        severity="warning",
        message="[Storage Pool 1] SMART warning detected on disk 3.",
        count=1,
        extra_fields={"source_type": "synology"},
        vl_url=VL_URL,
    )
    alert = _wait_for_alert_with_label("host", "NAS", "SynologySmartEventLog", WAIT_FOR_ALERT_S)
    assert alert is not None, "SynologySmartEventLog never reached webhook"
    assert alert["labels"]["source_tool"] == "vmalert-logs"
    assert alert["labels"]["integration"] == "synology"
    assert alert["labels"]["severity"] == "warning"


@pytest.mark.integration
@pytest.mark.slow
def test_synology_package_event_log_fires(test_webhook: None) -> None:
    """Plant a DSM package-update line; SynologyPackageEventLog fires (severity info)."""
    plant_log_lines(
        host="NAS",
        service="synology-package",
        severity="info",
        message="Package [Docker] has been updated to version 24.0.2.",
        count=1,
        extra_fields={"source_type": "synology"},
        vl_url=VL_URL,
    )
    alert = _wait_for_alert_with_label("host", "NAS", "SynologyPackageEventLog", WAIT_FOR_ALERT_S)
    assert alert is not None, "SynologyPackageEventLog never reached webhook"
    assert alert["labels"]["source_tool"] == "vmalert-logs"
    assert alert["labels"]["integration"] == "synology"
    assert alert["labels"]["severity"] == "info"


@pytest.mark.integration
@pytest.mark.slow
def test_synology_log_error_rate_fires(test_webhook: None) -> None:
    """Plant 3 DSM lines across synology-* with exact tokens failed/failure/critical.

    LogsQL phrase match is token-based, so each planted _msg must contain one of
    the EXACT tokens the rule matches. 3 lines across 3 buckets -> err_count=3 > 2
    (test-mirror threshold) -> SynologyLogErrorRate fires.
    """
    plant_log_lines(
        host="NAS",
        service="synology-auth",
        severity="warning",
        message=(
            "Connection: User [REDACTED] from [REDACTED] failed to sign in to [DSM] via [REDACTED]."
        ),
        count=1,
        extra_fields={"source_type": "synology"},
        vl_url=VL_URL,
    )
    plant_log_lines(
        host="NAS",
        service="synology-smart",
        severity="warning",
        message="[Storage Pool 1] disk read failure detected on disk 2.",
        count=1,
        extra_fields={"source_type": "synology"},
        vl_url=VL_URL,
    )
    plant_log_lines(
        host="NAS",
        service="synology-package",
        severity="warning",
        message="Package [Docker] reported a critical error during install.",
        count=1,
        extra_fields={"source_type": "synology"},
        vl_url=VL_URL,
    )
    alert = _wait_for_alert_with_label("host", "NAS", "SynologyLogErrorRate", WAIT_FOR_ALERT_S)
    assert alert is not None, "SynologyLogErrorRate never reached webhook"
    assert alert["labels"]["source_tool"] == "vmalert-logs"
    assert alert["labels"]["integration"] == "synology"
    assert alert["labels"]["severity"] == "info"
