"""Slow integration test: vmalert-logs end-to-end.

Brings up docker-compose.test.yml stack (VL + AM + vmalert-logs-test +
test webhook receiver running inside the integration-tests container)
then plants OOM/SSH log lines via VL ingest. Asserts vmalert-logs fires
within evaluation+wait windows, alert reaches the test webhook receiver,
and `source_tool=vmalert-logs` label is preserved.

Marked @pytest.mark.slow — runs in `make verify` (full CI), excluded
from `make test-fast`.

Test webhook: A separate uvicorn-served FastAPI process started inside
the integration-tests container on port 9090, with /api/alerts/ingest
appending payloads to a JSONL file at /tmp/received-alerts.jsonl that
the test polls.
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
def test_vmalert_logs_loads_system_rules() -> None:
    """vmalert-logs /api/v1/rules lists KernelOOM + SshFailedLoginBurst (i.e., rules parsed)."""
    require_rig_components("vmalert-logs")

    resp = httpx.get(f"{VMALERT_LOGS_URL}/api/v1/rules", timeout=5.0)
    assert resp.status_code == 200  # noqa: PLR2004
    rule_names = {
        rule["name"]
        for group in resp.json().get("data", {}).get("groups", [])
        for rule in group.get("rules", [])
    }
    assert {"KernelOOM", "SshFailedLoginBurst"}.issubset(rule_names), (
        f"missing rules; got {rule_names}"
    )


@pytest.mark.integration
@pytest.mark.slow
def test_kernel_oom_log_alert_fires(test_webhook: None) -> None:
    """Plant 1 OOM log line; vmalert-logs-test fires within 90s; webhook lands."""
    plant_log_lines(
        host="testhost-oom",
        service="kernel",
        severity="error",
        message="Out of memory: kill_process invoked: process 12345 (sshd)",
        count=1,
        vl_url=VL_URL,
    )
    alert = _wait_for_alert_with_label("host", "testhost-oom", "KernelOOM", WAIT_FOR_ALERT_S)
    assert alert is not None, f"KernelOOM never reached webhook within {WAIT_FOR_ALERT_S}s"
    assert alert["labels"]["source_tool"] == "vmalert-logs"
    assert alert["labels"]["severity"] == "error"
    assert alert["labels"]["target_kind"] == "host"


@pytest.mark.integration
@pytest.mark.slow
def test_ssh_failed_login_burst_fires(test_webhook: None) -> None:
    """Plant 3 'Failed password' lines (>2 threshold); SshFailedLoginBurst fires."""
    plant_log_lines(
        host="testhost-ssh",
        service="sshd",
        severity="warning",
        message="Failed password for root from 192.0.2.1 port 22 ssh2",
        count=3,
        vl_url=VL_URL,
    )
    alert = _wait_for_alert_with_label(
        "host", "testhost-ssh", "SshFailedLoginBurst", WAIT_FOR_ALERT_S
    )
    assert alert is not None
    assert alert["labels"]["source_tool"] == "vmalert-logs"
    assert alert["labels"]["severity"] == "warning"


@pytest.mark.integration
@pytest.mark.slow
def test_kernel_oom_alert_resolves_when_lines_age_out(test_webhook: None) -> None:
    """After 30s window expires + no new lines, vmalert-logs sends a resolved.

    Plants ONE line with `host=testhost-resolve`, waits for firing notification,
    waits 60s+ (window expiry + AM group_interval), asserts a resolved item lands.
    """
    plant_log_lines(
        host="testhost-resolve",
        service="kernel",
        severity="error",
        message="Out of memory: kill_process invoked: process 99999 (somebin)",
        count=1,
        vl_url=VL_URL,
    )
    fired = _wait_for_alert_with_label("host", "testhost-resolve", "KernelOOM", WAIT_FOR_ALERT_S)
    assert fired is not None, "firing alert never landed"
    # AM resolved_timeout is 5m default; with -evaluationInterval=5s and the 30s
    # window, vmalert stops sending the active series ~30-35s after planted line.
    # AM resolved_timeout gates how soon AM sends a "resolved" — default 5m. The
    # test rig's AM fixture overrides resolve_timeout to 60s for this assertion.
    deadline = time.time() + 180
    resolved = None
    while time.time() < deadline:
        if WEBHOOK_RECEIVED_FILE.exists():
            for line in WEBHOOK_RECEIVED_FILE.read_text().splitlines():
                if not line.strip():
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:  # pragma: no cover -- defensive
                    continue
                for a in payload.get("alerts", []):
                    if (
                        a.get("labels", {}).get("host") == "testhost-resolve"
                        and a.get("status") == "resolved"
                    ):
                        resolved = a
                        break
            if resolved:
                break
        time.sleep(5)
    assert resolved is not None, "resolved notification never landed within 180s"
    assert resolved["labels"]["source_tool"] == "vmalert-logs"
