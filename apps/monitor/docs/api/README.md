# HTTP API Reference

This document is for operators and integrators. It describes the homelab-monitor
HTTP API: endpoints, request/response shapes, authentication, error handling,
and Server-Sent Events (SSE) subscription.

## API conventions

**Timestamps:** All timestamps are ISO-8601 UTC (e.g., `"2026-05-05T14:30:00Z"`).

**Request body:** JSON (application/json).

**Response body:** JSON.

**Request ID:** Each request is assigned a unique `X-Request-Id` header (36-character hex UUID).
If the client sends `X-Request-Id`, the server echoes it; otherwise, the server generates one.
This ID propagates through logs and is returned in error responses for tracing.

**Status codes:** 200 (success), 400 (bad input), 401 (unauthorized), 403 (forbidden),
404 (not found), 409 (conflict), 422 (validation error), 500 (internal error),
503 (service unavailable).

## Authentication

The API uses two complementary authentication schemes — choose based on the caller:

| Caller | Mechanism | Use case |
|---|---|---|
| Browser / SPA | Signed-cookie session + double-submit CSRF token | UI traffic |
| External service (Alertmanager, scheduled cron) | `Authorization: Bearer <api-token>` | Programmatic access |

### Session cookies (browser flow)

1. `POST /api/auth/login` with JSON body `{"username": "...", "password": "..."}`. On success the server sets two cookies:
   - `homelab_monitor_session` — `HttpOnly; SameSite=Lax; Path=/; Max-Age=...; Secure` (Secure attribute controlled by `HOMELAB_MONITOR_HTTPS_ONLY_COOKIES`, default `true`).
   - `homelab_monitor_csrf` — readable by JS (HttpOnly=false), same Path/Max-Age/Secure as above.
2. The browser includes both cookies on subsequent requests automatically.
3. For state-changing requests (`POST/PUT/PATCH/DELETE`), JavaScript MUST read the `homelab_monitor_csrf` cookie and echo it in the `X-CSRF-Token` header. If absent or mismatched, the server returns 403 with `error.code = "csrf_mismatch"`.
4. Login is rate-limited to 5 failed attempts per 5 minutes per IP; 429 with `error.code = "rate_limited"` after that.
5. `POST /api/auth/logout` deletes the server-side session row and clears cookies.
6. `POST /api/auth/change-password` rotates the user's sessions; the prior session cookie becomes 401.

### API tokens (programmatic flow)

1. Operator generates a token via the CLI: `hm api-token create --scope <scope> --name <name>`. The plaintext is printed once — store it in a secrets manager.
2. Token format: `homelab_<env-prefix>_<base64url-30-bytes>`. Only the SHA-256 hash is persisted.
3. Caller sends `Authorization: Bearer <token>` on every request.
4. Tokens are CSRF-immune (no ambient credential).
5. Scopes available: `heartbeat:write`, `alerts:ingest:write`, `read:status`. Endpoint-to-scope mapping is documented per-endpoint below.

### Auth-exempt endpoints

These never require authentication:

- `GET /api/healthz`
- `GET /api/version`
- `GET /api/openapi.json`
- `GET /api/docs`
- `GET /api/redoc`
- `POST /api/auth/login`

### Bootstrap

A fresh deployment has zero users. The CLI is the only path to create the first user:

```
hm user create <username>
```

`/api/version` exposes `users_configured: false` until the first user is created. The lifespan emits a `lifespan.bootstrap_required` warning at startup when no users exist.

## Error envelope

All error responses have a uniform JSON structure:

```json
{
  "error": {
    "code": "error_code_here",
    "message": "Human-readable error message",
    "details": { "field": "value", ... }  // optional
  }
}
```

**Example 404:**
```json
{
  "error": {
    "code": "not_found",
    "message": "collector 'unknown' not found",
    "details": null
  }
}
```

**Example 422 (validation error):**
```json
{
  "error": {
    "code": "validation_error",
    "message": "validation error",
    "details": {
      "errors": [
        {
          "type": "string_pattern",
          "loc": ["body", "name"],
          "msg": "String should match pattern ...",
          "input": "..."
        }
      ]
    }
  }
}
```

| Status | Code | Meaning |
|---|---|---|
| 400 | `invalid_input` | Malformed request body or query parameters. |
| 401 | `unauthenticated` | No valid session cookie or API token; or session expired. |
| 403 | `forbidden` | User does not have permission for this resource. |
| 404 | `not_found` | Resource does not exist. |
| 409 | `conflict` | Request conflicts with current state. |
| 422 | `validation_error` | Pydantic validation failed; see `details.errors` for field-level issues. |
| 500 | `internal_error` | Unexpected server error. Logs the full exception; stack trace never leaks to client. |
| 503 | `dependency_unavailable` | Database, migrations, or required service not ready. |

## Endpoints

### `GET /api/healthz`

Health status and aggregate metrics.

**Response:**
```json
{
  "ok": true,
  "version": "0.1.0-dev",
  "db": "up",
  "scheduler": "running",
  "last_tick_at": "2026-05-05T14:30:00Z",
  "failed_ticks_last_5m": 2,
  "quarantined_collectors": ["docker-daemon"],
  "degraded_collectors": []
}
```

| Field | Type | Notes |
|---|---|---|
| `ok` | bool | `true` if database and scheduler are both operational. |
| `version` | string | Semantic version of homelab-monitor. |
| `db` | string | `"up"` or `"down"` (determined by probing `SELECT 1`). |
| `scheduler` | string | `"running"` or `"stopped"`. |
| `last_tick_at` | string \| null | ISO-8601 UTC timestamp of the most recent collector tick (success or failure), or `null` if no ticks yet. |
| `failed_ticks_last_5m` | int | Count of failed ticks in the last 5 minutes. |
| `quarantined_collectors` | list[string] | Names of collectors currently under quarantine (repeated failure budget exhaustion). |
| `degraded_collectors` | list[string] | Collectors that failed to load at startup (e.g., malformed manifest). |

**Example curl:**
```bash
curl -s http://localhost:9090/api/healthz | jq .
```

---

### `GET /api/version`

Server version and metadata.

**Response:**
```json
{
  "version": "0.1.0-dev",
  "git_sha": "abc123def456...",
  "built_at": "2026-05-05T10:00:00Z"
}
```

| Field | Type | Notes |
|---|---|---|
| `version` | string | Semantic version from `homelab_monitor.__version__`. |
| `git_sha` | string | Git commit SHA. Defaults to `"dev"` if not set via `HOMELAB_MONITOR_GIT_SHA`. |
| `built_at` | string | ISO-8601 UTC timestamp of build. Defaults to current time if not set via `HOMELAB_MONITOR_BUILT_AT`. |

**Example curl:**
```bash
curl -s http://localhost:9090/api/version | jq .
```

---

### `GET /api/collectors`

List all registered collectors and their status.

**Response:**
```json
{
  "collectors": [
    {
      "name": "noop",
      "status": "healthy",
      "last_run": "2026-05-05T14:30:00Z",
      "last_error": null,
      "quarantined": false,
      "quarantined_at": null,
      "quarantine_reason": null,
      "next_run": "2026-05-05T14:31:00Z",
      "run_kind": "async",
      "interval_seconds": 60.0,
      "consecutive_failures": 0
    }
  ]
}
```

| Field | Type | Notes |
|---|---|---|
| `name` | string | Collector name (unique). |
| `status` | string | `"healthy"` (running normally), `"quarantined"` (failure budget exhausted), or `"degraded"` (failed to load). |
| `last_run` | string \| null | ISO-8601 UTC timestamp of the most recent tick, or `null`. |
| `last_error` | string \| null | Error message from the most recent failure, or `null`. |
| `quarantined` | bool | `true` if under quarantine. |
| `quarantined_at` | string \| null | ISO-8601 UTC timestamp when quarantine began, or `null`. |
| `quarantine_reason` | string \| null | Reason for quarantine (e.g., `"consecutive_failures"`), or `null`. |
| `next_run` | string \| null | Estimated ISO-8601 UTC timestamp of the next scheduled tick, or `null` if never run. |
| `run_kind` | string | Collector execution model: `"async"`, `"thread"`, `"process"`, or `"subprocess"`. |
| `interval_seconds` | float | Tick interval in seconds. |
| `consecutive_failures` | int | Number of consecutive tick failures. Resets on success. |

**Auth:** Session cookie + `X-CSRF-Token` (no scope check); token-based access not yet supported on this endpoint.

**Example curl (after login; assumes you saved the cookies via `-c` and read CSRF from the jar):**
```bash
curl -s -b cookies.txt http://localhost:9090/api/collectors | jq .
```

---

### `POST /api/collectors/{name}/retry`

Request an immediate out-of-band run for a collector, bypassing the normal schedule.
Also clears any active quarantine.

**Path parameters:**
- `name` (string): Collector name. Must match `^[a-z][a-z0-9_-]{2,63}$`.

**Response:**
```json
{
  "name": "docker-daemon",
  "tick_id": "a1b2c3d4e5f6...",
  "requested_at": "2026-05-05T14:30:00Z"
}
```

| Field | Type | Notes |
|---|---|---|
| `name` | string | Collector name (echoed from request). |
| `tick_id` | string | Unique tick identifier (36-character hex UUID). The next SSE event for this collector will carry this ID. |
| `requested_at` | string | ISO-8601 UTC timestamp when the request was received. |

**Status codes:**
- 200: Success.
- 404: Collector does not exist.
- 401: Missing/invalid auth header (when auth enabled).
- 500: Unexpected error (e.g., no failure budget configured for this collector).

**Auth:** Session cookie + `X-CSRF-Token` (state-changing endpoint).

**Example curl (post-login):**
```bash
CSRF=$(grep homelab_monitor_csrf cookies.txt | awk '{print $7}')
curl -s -X POST -b cookies.txt -H "X-CSRF-Token: $CSRF" \
  http://localhost:9090/api/collectors/noop/retry | jq .
```

---

### `GET /api/events`

Subscribe to a stream of collector tick events via Server-Sent Events (SSE).

**Response:** Streaming HTTP response with `Content-Type: text/event-stream`.

**SSE event format:**
```
event: collector.tick
data: {"kind":"collector.tick","collector":"noop","tick_id":"a1b2c3...","outcome":"success","duration_seconds":0.005,"ts":"2026-05-05T14:30:00Z","reason":null,"trigger_kind":"scheduled","request_id":null}
id: 1

```

Each event is a `SchedulerTickEvent` with these fields:

| Field | Type | Notes |
|---|---|---|
| `kind` | string | Always `"collector.tick"`. |
| `collector` | string | Name of the collector that just ran. |
| `tick_id` | string | Unique tick identifier (36-character hex UUID). |
| `outcome` | string | `"success"`, `"failure"`, `"skipped"`, or `"shutdown"`. |
| `reason` | string \| null | Failure reason if `outcome="failure"`: `"group_busy"`, `"quarantined"`, `"timeout"`, `"exception"`, `"result_error"`. Otherwise `null`. |
| `duration_seconds` | float \| null | Wall-clock time elapsed during the tick, or `null` if skipped. |
| `trigger_kind` | string | How the tick was initiated: `"scheduled"` (normal interval), `"retry"` (manual via retry endpoint), or `"manual"` (reserved for future). |
| `request_id` | string \| null | Request ID of the API call that triggered a `"retry"` tick, or `null` for scheduled ticks. |
| `ts` | string | ISO-8601 UTC timestamp when the tick completed. |

**Replay on connect:** The broker maintains a 50-event ring buffer. New subscribers receive
the last 50 events as a replay before receiving live events.

**Slow subscriber handling:** If a subscriber falls behind and the 64-event queue overflows,
the broker disconnects the subscriber by sending a special event:

```
event: error
data: {"reason":"slow_subscriber"}

```

The subscriber should treat this as a signal to reconnect and replay.

**Auth:** Session cookie required (GET endpoint; no CSRF needed).

**Example curl (listen for 10 seconds, post-login):**
```bash
curl -s -b cookies.txt http://localhost:9090/api/events \
  --max-time 10 | head -20
```

---

## Environment variables

The API and scheduler are configured via environment variables:

| Variable | Default | Notes |
|---|---|---|
| `HOMELAB_MONITOR_LOG_FORMAT` | `"json"` | Log format: `"json"` (structured) or `"pretty"` (console-friendly). |
| `HOMELAB_MONITOR_LOG_LEVEL` | `"INFO"` | Minimum log level: `"DEBUG"`, `"INFO"`, `"WARNING"`, `"ERROR"`, `"CRITICAL"`. |
| `HOMELAB_MONITOR_PLUGINS_DIR` | `runbooks/_examples` | Path to subprocess plugin directory (searched recursively). |
| `HOMELAB_MONITOR_DB_URL` | `sqlite+aiosqlite:///./data/homelab-monitor.db` | SQLAlchemy database URL. |
| `HOMELAB_MONITOR_AUTO_MIGRATE` | `"true"` | Auto-apply pending migrations at startup: `"true"` or `"false"`. |
| `HOMELAB_MONITOR_MASTER_KEY` | unset | Master key for secret encryption (16+ bytes base64-encoded, or a file path via `*_FILE`). |
| `HOMELAB_MONITOR_MASTER_KEY_FILE` | unset | Path to a file containing the base64-encoded master key. |
| `HOMELAB_MONITOR_HTTPS_ONLY_COOKIES` | `"true"` | When true, sets `Secure` on the session and CSRF cookies. Set to `"false"` only in HTTPS-disabled local dev. |
| `HOMELAB_MONITOR_SESSION_TTL_DAYS` | `7` | Idle session TTL in days. Sessions older than this are rejected by the middleware. |
| `HOMELAB_MONITOR_BCRYPT_COST` | `12` | Bcrypt cost factor for password hashing. Tests use `4` for speed; production should remain at `12`. |
| `HOMELAB_MONITOR_TOKEN_PREFIX` | `prod` | Inserted between `homelab_` and the random token body when minting API tokens via `hm api-token create`. Set to `dev` in dev environments to distinguish accidentally-pasted tokens. |
| `HOMELAB_TICK_ID` | (propagated) | Set by the scheduler in subprocess env; unique identifier for this tick. |
| `HOMELAB_TRIGGER_KIND` | (propagated) | Set by the scheduler in subprocess env when a `TriggerContext` is present: `"scheduled"`, `"retry"`, or `"manual"`. |
| `HOMELAB_REQUEST_ID` | (propagated) | Set by the scheduler in subprocess env when a `TriggerContext` has a `request_id`. |

## OpenAPI / Swagger

The OpenAPI schema is exported to `packages/shared-types/openapi.json` via the
`scripts/export-openapi.sh` pre-commit hook. It is available at:

```
GET /api/openapi.json
```

Swagger UI is available at:

```
GET /api/docs
```

ReDoc is available at:

```
GET /api/redoc
```

To regenerate the schema after API changes:

```bash
make openapi-export
```

Or, to trigger via pre-commit:

```bash
git add apps/monitor/homelab_monitor/kernel/api/*.py
git commit  # pre-commit hook regenerates packages/shared-types/openapi.json
```

---

## Running the API server

**Development mode with auto-reload:**

```bash
make backend-dev
```

This starts the server on `http://localhost:9090` with `--reload` enabled.

**Example request:**

```bash
curl -s http://localhost:9090/api/healthz | jq .
```
