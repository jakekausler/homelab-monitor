# Integration Test Rig

## Overview

The integration rig is a full docker-compose stack (`deploy/compose/docker-compose.test.yml`) that brings up the real monitor backend, its sidecars (VictoriaMetrics, VictoriaLogs, vmagent, vmalert ×2, Alertmanager, Karma, kthxbye, Grafana, vector), and two controllable fixture services (fixture-host, noisy-logger). A dedicated `integration-tests` container runs pytest against the live stack.

**When to use integration tests:**

- The behaviour spans multiple services (metric scrape → alert fire → webhook → SSE).
- You need to verify a real Alertmanager webhook, a real VictoriaLogs query, or a real SSE stream.
- The "happy path" is too expensive to stub reliably in unit tests.

**When NOT to use integration tests:**

- You're testing a single function, endpoint, or DB query in isolation — use unit tests (`tests/`) with mocks or an in-memory SQLite database.
- You're iterating on a new feature mid-Build; `make test-fast` is ~5× faster and gives quicker feedback.
- You're in CI on a branch that hasn't touched the pipeline path — integration adds ~7-8 min per run.

The integration suite is gated by the `@pytest.mark.integration` marker. An AST guardrail at `apps/monitor/tests/test_integration_markers_guardrail.py` fails if any test in `tests/integration/` is missing the marker, preventing accidental inclusion in the fast unit suite.

---

## Running Locally

### One-shot (matches CI exactly)

```bash
bash scripts/run-integration.sh
```

This script:

1. Runs a Docker `cp` to pre-seed `deploy/compose/test-fixtures/am-config-seed/alertmanager.yml` so Alertmanager can start before the monitor's bootstrap command renders the real config. Without this pre-seed, AM and the monitor deadlock on each other's health checks.
2. Runs `docker compose up --build --abort-on-container-exit --exit-code-from integration-tests`.
3. Tears the whole stack down when the `integration-tests` container exits.

The exit code of the script matches the exit code of the `integration-tests` container.

### Via Make

```bash
make integration
```

This is a thin wrapper around `bash scripts/run-integration.sh`.

### Tear-down

The `--abort-on-container-exit` flag handles automatic tear-down. If you need to clean up a stuck stack manually:

```bash
docker compose -f deploy/compose/docker-compose.test.yml down -v
```

The `-v` removes the anonymous volumes (`data_monitor_test`, `shared_rig_secrets`) so the next run gets a clean database and token file.

---

## Rig Topology

All services share the `homelab-monitor-test-net` bridge network. Internal DNS names match service names.

| Service | Container name | Internal DNS | Host port (127.0.0.1 only) | Purpose |
|---|---|---|---|---|
| `victoriametrics` | homelab-vm-test | `victoriametrics:8428` | 8428 | Metrics TSDB |
| `victorialogs` | homelab-vl-test | `victorialogs:9428` | 9428 | Log TSDB |
| `vmagent` | homelab-vmagent-test | — | — | Scrapes fixture-host + monitor |
| `vector` | homelab-vector-test | — | — | Collects noisy-logger stdout → VL |
| `alertmanager` | homelab-alertmanager-test | `alertmanager:9093` | — | Alert routing + webhook |
| `karma` | homelab-karma-test | `karma` | — | AM UI (distroless, no healthcheck) |
| `kthxbye` | homelab-kthxbye-test | — | — | Auto-extends AM silences |
| `vmalert-metrics` | homelab-vmalert-metrics-test | `vmalert-metrics:8880` | — | Evaluates metrics alert rules |
| `vmalert-logs-test` | homelab-vmalert-logs-test | — | — | Evaluates log alert rules |
| `grafana` | homelab-grafana-test | `grafana` | 3000 | Dashboards (custom image with VL plugin) |
| `fixture-host` | homelab-fixture-host-test | `fixture-host:8000` | 8000 | Controllable Prometheus metric source |
| `noisy-logger` | homelab-noisy-logger-test | `noisy-logger:8001` | 8001 | Planted log lines via HTTP |
| `shared-init` | homelab-shared-init | — | — | Init container: chmods shared volume |
| `monitor` | homelab-monitor-test | `monitor:9090` | 19090 | FastAPI backend under test |
| `integration-tests` | homelab-integration-tests | — | — | Pytest runner |

**Volumes:**

- `data_monitor_test` — monitor's SQLite database (`/data/homelab.db`).
- `shared_rig_secrets` — ephemeral volume for the bootstrap API token (`/shared/rig-token`). Mounted read-only by the `integration-tests` container.

**Monitor bootstrap sequence** (all in one `command` block to avoid race conditions):

1. Pre-seed alertmanager.yml so AM can reach healthy.
2. `hm migrate` — run DB migrations.
3. `hm user create admin` — create the rig admin user (idempotent).
4. `hm api-token create` — write the rig token to `/shared/rig-token`.
5. `exec uvicorn` — hand off to the API server.

---

## Adding a New Integration Test

### File location

All integration tests live under:

```
apps/monitor/tests/integration/
```

New test files must be named `test_*.py` and placed in that directory (or a subdirectory, as long as `conftest.py` propagates markers).

### Required marker

Every test function in `tests/integration/` **must** carry `@pytest.mark.integration`. The AST guardrail (`apps/monitor/tests/test_integration_markers_guardrail.py`) will fail the unit suite if any function in that tree is missing the marker. This is intentional: it prevents integration tests from accidentally running during `make test-fast`.

```python
import pytest
from .helpers.rig import Rig

@pytest.mark.integration
def test_my_new_scenario() -> None:
    with Rig.boot() as rig:
        ...
```

### Polling pattern

The rig provides deadline-based polling helpers rather than `time.sleep` loops. Use them:

```python
@pytest.mark.integration
def test_alert_appears_after_log_plant() -> None:
    with Rig.boot() as rig:
        rig.plant_log_via_noisy_logger("CRITICAL: disk full on /dev/sda1")
        alert = rig.wait_for_alert(
            "NoisyLoggerCritical",
            source_tool="vmalert-logs-test",
            timeout_s=60.0,
        )
        assert alert["status"] == "firing"
```

Avoid sleeping fixed durations. The pipeline has variable propagation delays (vmagent scrape interval is 5 s, vmalert evaluation is 5 s). The `wait_for_alert` default of 60 s provides adequate margin on both warm and cold rigs.

---

## The Rig Helper

**Defined at:** `apps/monitor/tests/integration/helpers/rig.py`

`Rig` is the facade for all test interactions with the live stack. Always construct it via `Rig.boot()` as a context manager — this handles login, CSRF token capture, and socket cleanup.

### Public API

#### `Rig.boot() -> ContextManager[Rig]`

Class-method context manager. Reads URL env vars (`MONITOR_URL`, `FIXTURE_HOST_URL`, `NOISY_LOGGER_URL`, `AM_URL`, `VL_URL`) with sensible defaults for compose-internal DNS. Reads the bootstrap token from `/shared/rig-token` (waits up to 30 s for it to appear). Performs cookie-session login as the rig admin user and captures the CSRF token.

```python
with Rig.boot() as rig:
    # rig is logged in and ready
    ...
```

#### `rig.set_fixture_cpu(value: int) -> None`

POSTs `{"cpu_percent": value}` to `fixture-host /control`. Instantly mutates the `cpu_percent` gauge that vmagent scrapes. Value must be 0–100.

#### `rig.wait_for_alert(alertname, *, source_tool, severity, timeout_s, poll_interval_s) -> dict`

Polls `GET /api/alerts?status=firing&limit=200` until an alert matching `alertname` (and optional `source_tool` / `severity` exact-match filters) appears. Returns the matching alert dict. Raises `TimeoutError` on timeout with a diagnostic message listing the last-seen alerts.

#### `rig.wait_for_resolution(alert_id, *, timeout_s, poll_interval_s) -> dict`

Polls `GET /api/alerts/{alert_id}` until `resolved_at` is non-null. Returns the resolved alert dict.

#### `rig.wait_for_sse_event(kind, *, timeout_s, match_alert_id) -> dict`

Opens `GET /api/events` as an SSE stream and returns the first event payload with the given `kind` (e.g. `"alert.firing"`, `"alert.resolved"`). If `match_alert_id` is provided, skips events for other alerts. The SSE endpoint replays the last 50 events, so you can open a fresh connection after polling for resolution and still catch the event.

#### `rig.plant_log_via_noisy_logger(line: str) -> None`

POSTs `{"line": line}` to `noisy-logger /log`, which prints the line to stdout. Vector picks it up from the Docker log socket and ships it to VictoriaLogs. Use this to trigger log-based alert rules without writing to the host filesystem.

#### `rig.get(path, **kwargs) -> httpx.Response`

Issues a GET against the monitor with the rig's session cookie. Use for reading API endpoints that require auth.

#### `rig.post(path, **kwargs) -> httpx.Response`

Issues a POST against the monitor with the cookie session + CSRF header. For state-changing requests over the cookie auth path. Token-auth requests (e.g. heartbeat ingest) should use `rig.token` directly.

#### `rig.token: str`

The plaintext API token written by the monitor bootstrap. Has `heartbeat:write` and `alerts:ingest:write` scopes.

---

## Fixture Services

### fixture-host

**Source:** `apps/monitor/tests/fixtures/fixture-host/`

A minimal Python HTTP server that exposes a Prometheus metrics endpoint (`/metrics`) with a controllable `cpu_percent` gauge. The gauge starts at the value of `FIXTURE_CPU_PERCENT` env var (default `5`). POST to `/control` with `{"cpu_percent": N}` to change it live.

vmagent scrapes `/metrics` every 5 s. vmalert evaluates the `FixtureHostHighCPU` rule every 5 s. After `set_fixture_cpu(95)`, expect the alert to fire within ~15–30 s.

Use fixture-host when your test scenario involves metric-based alert rules.

### noisy-logger

**Source:** `apps/monitor/tests/fixtures/noisy-logger/`

A minimal Python HTTP server. POST to `/log` with `{"line": "..."}` and it prints that line to stdout. Vector reads container logs from the Docker socket and ships them to VictoriaLogs. vmalert-logs-test evaluates log-based rules every 5 s.

Use noisy-logger when your test scenario involves log-based alert rules.

### vl_planter.py

For scenarios that need log lines injected directly into VictoriaLogs without going through the Docker log pipeline (e.g., testing historical queries or bypassing the vector → VL latency), use `vl_planter.py` if it exists under `apps/monitor/tests/integration/helpers/`. It POSTs directly to `victorialogs:9428/insert`. Check the helpers directory for current availability.

---

## CI Integration

The integration job is defined in `.github/workflows/ci.yml`. It runs after the unit/type/lint job passes.

Key CI behaviours:

- **BuildKit cache:** The workflow uses `docker buildx` with a GitHub Actions cache backend, so the Python layer of `Dockerfile.test` is cached between runs. Cold builds (new deps) add ~2–3 min; warm cache runs add ~30 s to image assembly.
- **Pre-seed step:** `scripts/run-integration.sh` performs the alertmanager config pre-seed via a throwaway Alpine container before `compose up`. This step is idempotent.
- **Exit code:** The job fails if the `integration-tests` container exits non-zero. The `--abort-on-container-exit` flag ensures all other containers stop when `integration-tests` finishes.
- **Expected runtime:** ~7–8 minutes on the GitHub-hosted runner. Most of this is the healthcheck `start_period` for the monitor (30 s) and Grafana (60 s).

---

## Debugging Failures

### Read container logs

After a failure, `run-integration.sh` leaves the stack torn down. Re-run with `--no-recreate` disabled — or capture logs before exit — by running the stack manually:

```bash
docker compose -f deploy/compose/docker-compose.test.yml up --build 2>&1 | tee /tmp/rig.log
```

To extract per-service logs from a running stack:

```bash
docker compose -f deploy/compose/docker-compose.test.yml logs monitor
docker compose -f deploy/compose/docker-compose.test.yml logs alertmanager
docker compose -f deploy/compose/docker-compose.test.yml logs integration-tests
```

### Common failure modes

**Alertmanager permission error on startup**

Alertmanager refuses to start because `/etc/alertmanager/alertmanager.yml` is missing or has wrong ownership. This happens when the pre-seed step didn't run (e.g., running `compose up` directly without `scripts/run-integration.sh`). Fix: always use the script, or run the pre-seed docker command manually first.

**Missing UI bundle / 404 on frontend assets**

The monitor serves the React bundle from `apps/ui/dist/`. The production `Dockerfile` runs the UI build; if the build layer was cached with a stale bundle, static assets may be wrong. Run `docker compose build --no-cache monitor` to force a fresh build.

**Master key validation error on monitor startup**

The monitor validates that `HOMELAB_MONITOR_MASTER_KEY` decodes to exactly 32 bytes. The compose file has a default test key that satisfies this. If you override the env var locally, ensure it's a valid base64-encoded 32-byte value: `python3 -c "import os, base64; print(base64.b64encode(os.urandom(32)).decode())"`.

**`wait_for_alert` times out**

Check vmalert-metrics logs for rule evaluation errors. Check alertmanager logs for webhook delivery failures. Check monitor logs for ingest endpoint errors. The most common causes are: the monitor's AM config wasn't re-rendered (bootstrap CM failed silently), or the rule file path is wrong in the compose volume mount.

**Rig token file not found**

The monitor's bootstrap sequence failed before writing `/shared/rig-token`. Inspect `docker compose logs monitor` for `[bootstrap]` lines. Common causes: migration failure, user creation error, or token CLI output format changed.

---

## Adding a New Fixture Service

When you need a new controllable service (e.g. a fake SNMP agent, a mock webhook receiver):

### 1. Write the fixture

Create a minimal HTTP server under `apps/monitor/tests/fixtures/<your-service>/`:

```
apps/monitor/tests/fixtures/your-service/
  Dockerfile
  server.py
```

Dockerfile template:

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY server.py .
EXPOSE 8002
HEALTHCHECK --interval=5s --timeout=3s --retries=10 --start-period=5s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8002/healthz').read()"
CMD ["python", "server.py"]
```

Include a `/healthz` endpoint that returns 200 so compose health checks work.

### 2. Add to docker-compose.test.yml

```yaml
your-service:
  build:
    context: ../../apps/monitor/tests/fixtures/your-service
    dockerfile: Dockerfile
  container_name: homelab-your-service-test
  networks:
    - homelab-monitor-test-net
  ports:
    - "127.0.0.1:8002:8002"
  healthcheck:
    test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8002/healthz').read()"]
    interval: 5s
    timeout: 3s
    retries: 10
    start_period: 5s
```

Add it to the `depends_on` block of the `integration-tests` service with `condition: service_healthy`.

### 3. Expose via Rig (optional)

If tests need to control the service frequently, add a URL field to `RigUrls` and a helper method to `Rig`. Follow the pattern of `set_fixture_cpu` or `plant_log_via_noisy_logger`.
