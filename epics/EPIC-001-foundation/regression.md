# Regression Checklist - EPIC-001: Foundation

Items to verify after each deployment in this epic. Format: `[D]` = desktop, `[M]` = mobile, `[D][M]` = both.

## STAGE-001-001: Backend Python skeleton

- [ ] `make setup` from a clean clone produces a working `.venv` (no "Failed to spawn ruff" or similar)
- [ ] `make verify` runs ruff + pyright + pytest cleanly with 100% coverage on tracked files
- [ ] `scripts/verify` produces output identical to `make verify`
- [ ] Pre-commit hook installed; running `pre-commit run --all-files` does not modify any project files (excluding `.omc/state/*` tool state)
- [ ] `make clean` removes `.coverage`, `htmlcov/`, `.pytest_cache/`, `.ruff_cache/`, `__pycache__/` without errors and without removing source files
- [ ] `make clean && make verify` round-trip exits 0 (regenerates artifacts)
- [ ] `hm` CLI prints `homelab-monitor 0.0.0` (entry point intact)
- [ ] `python -m homelab_monitor.cli` produces identical output to `hm`
- [ ] `uv.lock` is present in the repo root and tracked
- [ ] `.python-version` matches the runtime `python --version`

## STAGE-001-002: Frontend skeleton

- [ ] `pnpm verify` (root) passes: lint + format-check + tsc + vitest with 100% coverage on tracked files + vite build
- [ ] `make verify` chains both backend and frontend pipelines green
- [ ] `pnpm --filter ui run dev` starts a Vite dev server in <500ms with no errors
- [ ] [D][M] Placeholder Tremor card renders centered on dark `#0b0d10` background at first paint (no FOUC)
- [ ] [D] Card heading "homelab-monitor" and status text "EPIC-001 STAGE-001-002" visible at 1280×720
- [ ] [M] Card fits within 375×667 viewport with no horizontal scrollbar
- [ ] [D][M] Zero console errors and zero 4xx/5xx network requests on initial load
- [ ] HMR works: editing `apps/ui/src/App.tsx` updates the page without full reload
- [ ] `pnpm --filter ui run build` produces `apps/ui/dist/` (chunk-size warning ~993KB is informational, deferred)
- [ ] `pnpm-lock.yaml` is present at the repo root and tracked in git
- [ ] No emitted `.js` or `.d.ts` files appear in `apps/ui/src/` after running `pnpm typecheck` (noEmit: true respected)

## STAGE-001-004 — SQLite + Alembic + first migration (added 2026-05-05)

Re-run after any change to: `apps/monitor/homelab_monitor/kernel/db/`,
`apps/monitor/alembic/`, `apps/monitor/homelab_monitor/cli/migrate.py`,
or related tests.

- [ ] `cd apps/monitor && HOMELAB_MONITOR_DB_URL="sqlite+aiosqlite:///$(mktemp -d)/test.db" uv run hm migrate status` reports `pending migrations` against an empty DB
- [ ] `cd apps/monitor && HOMELAB_MONITOR_DB_URL="sqlite+aiosqlite:///$(mktemp -d)/test.db" uv run hm migrate` exits 0 and applies the schema
- [ ] After applying: `hm migrate status` reports `up to date`, `current: 0001`, `head: 0001`
- [ ] `hm migrate history` lists `0001 -> <base>: initial schema (19 tables, 2 indexes)`
- [ ] After migration, the DB has exactly 19 application tables (excluding `sqlite_*` and `alembic_version`)
- [ ] After migration, pragmas via the engine: `journal_mode=wal`, `foreign_keys=1`, `busy_timeout=5000`
- [ ] With `HOMELAB_MONITOR_AUTO_MIGRATE=false` and an empty DB, `kernel.db.migrations.run_migrations()` raises `MigrationsPendingError`
- [ ] Repository facade smoke: `repo.execute(insert)`, `repo.fetch_one(select)`, `repo.fetch_all(select)`, `audit_write(repo, ...)`, and `repo.transaction()` rollback on exception all work against a real tempfile DB
- [ ] Alembic round-trip via the `hm migrate` CLI: upgrade head → downgrade-equivalent (or new migration) → upgrade head leaves DB at expected state

## STAGE-001-003: CI + Code Review Graph + Dependabot

- [ ] `make verify-ci` runs locally and exits 0 (full Python+frontend chain + CRG build)
- [ ] Pushing a PR with deliberately-broken Python (unused import OR type mismatch) triggers a FAIL on the `backend` CI job and PASS on `frontend` + `crg-build` + CodeQL
- [ ] Reverting the breakage in the same PR moves all checks to green
- [ ] CodeQL fires on `push` to main (Analyze python + javascript)
- [ ] CodeQL fires on every PR
- [ ] Required-status-checks list in branch protection includes: `backend`, `frontend`, `crg-build`, `Analyze (python)`, `Analyze (javascript)` (per `docs/repo-setup.md`)
- [ ] `release.yml` does NOT fire on push or PR — only on `v*` tags
- [ ] Dependabot opens an update PR within 48h of a known-stale dep being added; minor+patch updates are grouped per ecosystem
- [ ] `pnpm/action-setup@v4` reads `packageManager` from `package.json` automatically (no `version:` input on the action — see PR #29)

## STAGE-001-004: SQLite + Alembic + first migration

- [ ] Fresh container boots, runs migrations, ends up at head revision
- [ ] `alembic downgrade -1` then `alembic upgrade head` round-trip works
- [ ] `audit_log` row appears whenever a state-changing API call is made

## STAGE-001-005 — Encrypted secrets store (added 2026-05-05)

Re-run after any change to: `apps/monitor/homelab_monitor/kernel/secrets/`,
`apps/monitor/homelab_monitor/cli/secrets.py`, `apps/monitor/alembic/versions/0002_secrets_columns.py`,
or related tests.

- [ ] `cd apps/monitor && KEY_B64=$(head -c 32 /dev/urandom | base64) && DB="$(mktemp -d)/test.db" && HOMELAB_MONITOR_DB_URL="sqlite+aiosqlite:///$DB" uv run hm migrate` exits 0
- [ ] `echo -n 'sentinel-v1' | HOMELAB_MONITOR_DB_URL="..." HOMELAB_MONITOR_MASTER_KEY="$KEY_B64" uv run hm secrets set tok --from-stdin` exits 0
- [ ] `hm secrets list` shows the secret name + created_at, never the plaintext value
- [ ] `hm secrets get tok` (without REVEAL=1) exits 1 with `set HOMELAB_MONITOR_REVEAL=1 to reveal` on stderr
- [ ] `HOMELAB_MONITOR_REVEAL=1 hm secrets get tok` exits 0 and prints the plaintext exactly
- [ ] `echo -n 'sentinel-v2' | hm secrets rotate tok --from-stdin` exits 0; subsequent get returns the new value
- [ ] `hm secrets delete tok` exits 0; subsequent get returns 1 with "no secret"
- [ ] `echo -n "$NEW_KEY_B64" | hm secrets rotate-master --from-stdin` exits 0 and prints "N secret(s) re-encrypted" + old/new fingerprints
- [ ] After rotate-master: get with the OLD key fails with AEAD tag verification error; get with NEW key returns the original plaintext
- [ ] Direct DB corruption: `sqlite3 "$DB" "UPDATE secrets SET ciphertext='AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA' WHERE name=?"` followed by `hm secrets get` returns AEAD error
- [ ] No-leak: a sentinel value piped through any non-`get` CLI op never appears in captured stdout or stderr
- [ ] audit_log table contains rows for set/rotate/delete/rotate-master with `before_json` / `after_json` containing ONLY metadata (name, row_count), never plaintext values

## STAGE-001-006 — Collector protocol + base classes (added 2026-05-05)

Re-run after any change to: `apps/monitor/homelab_monitor/kernel/plugins/`
or related tests.

- [ ] All 20 public symbols import from `homelab_monitor.kernel.plugins` (RunKind, TrustLevel, CollectorConfig, CollectorEvent, CollectorResult, CollectorContext, Collector, BaseCollector, NoopCollector, MetricsWriter, LogsWriter, InMemoryMetricsWriter, InMemoryLogsWriter, MetricEntry, LogEntry, SshClientFactory, SshConnection, HomeAssistantClient, plus the 4 event payload classes if exported)
- [ ] `isinstance(NoopCollector(), Collector)` returns True (runtime_checkable Protocol)
- [ ] `isinstance(InMemoryMetricsWriter(), MetricsWriter)` returns True
- [ ] `isinstance(InMemoryLogsWriter(), LogsWriter)` returns True
- [ ] All 4 CollectorEvent kinds round-trip via `TypeAdapter[CollectorEvent].validate_python(...).dump_json(...)`
- [ ] Invalid discriminator (`{"kind": "unknown"}`) raises `pydantic.ValidationError`
- [ ] CollectorContext is `@dataclass(slots=True)`: extra attribute assignment raises AttributeError
- [ ] CollectorContext.ha defaults to None
- [ ] BaseCollector class defaults: run_kind=RunKind.ASYNC, trust_level=TrustLevel.BUILTIN, concurrency_group="default"
- [ ] NoopCollector().run(ctx) returns CollectorResult(ok=True, metrics_emitted=0, errors=[], events=[])
- [ ] CollectorEvent pickle round-trip preserves kind discriminator (STAGE-001-009 subprocess boundary requirement)

## STAGE-001-007 — Scheduler

1. **Run scheduler e2e suite as part of slow/integration tier** — `apps/monitor/tests/test_scheduler_e2e.py`. Particularly `test_long_running_tick_precision` (30s window) catches deadline drift that short unit tests cannot reveal.
2. **Re-run PROCESS worker crash test after any ProcessPoolExecutor config change** — the isolation guarantee (worker death → failure metric, scheduler continues) is load-bearing for scheduler stability.
3. **Re-run high-cardinality offset spread test if collector naming conventions change** — if all collector names hash to the same offset bucket, thundering herd protection silently breaks.
4. **Address fork() in multi-threaded context before enabling PROCESS RunKind in production** — STAGE-001-010 (FastAPI lifespan) must construct ProcessPoolExecutor with `mp_context=multiprocessing.get_context("forkserver")` (or "spawn"), or document that PROCESS RunKind is unsupported in production. Add a test that verifies the executor's start method once decided.

   **Status update (2026-05-06):** CLOSED. STAGE-001-010 Build added `mp_context="forkserver"` to ProcessPoolExecutor instantiation in scheduler.py:172. STAGE-001-010 Refinement Scenario 4 confirmed under real lifespan conditions: `app.state.scheduler._process_pool._mp_context.get_start_method() == "forkserver"`. Unit-tested in `test_scheduler_request_immediate_run.py::test_process_pool_executor_has_forkserver_context`.

## STAGE-001-008 — Concurrency groups + failure budget + quarantine

1. **Quarantine DB columns set atomically with audit row** — `consecutive_failures`, `quarantined_at`, and `quarantine_reason` must all be written in the same transaction as the `audit_log` INSERT. Verify by reading both tables after exactly N=threshold failures: SQL UPDATE columns + audit_log row should be visible together or not at all.
2. **Quarantine gates ticks after threshold** — After quarantine fires, `homelab_collector_run_skipped_total{reason="quarantined"}` must increment on each scheduled tick; `success_total` and `failure_total` must NOT increment for the quarantined collector.
3. **load_state() rehydrates quarantine across scheduler restart** — A new `FailureBudget` bound to the same DB file must return `is_quarantined=True` for previously-quarantined collectors without any additional failures occurring. Run scheduler instance #2 → 0 ticks of the quarantined collector.
4. **clear_quarantine audit trail** — `audit_log` must contain `collector.quarantine_cleared` event with correct `who` field (operator-supplied `by` parameter). The cleared event must appear chronologically AFTER the corresponding `quarantine_entered` event.
5. **concurrency_group serializes named-group members** — Maximum concurrent execution depth for collectors sharing a non-default group name must not exceed 1, even with sleep-heavy `run()` implementations. Verify via shared in-process counter.
6. **group_busy skip metric** — When a group lock is held past `interval/2`, the waiting collector must emit `homelab_collector_run_skipped_total{reason="group_busy"}` rather than blocking or timing out with an error.
7. **quarantine_after per-collector override** — Threshold of N < default(5) must quarantine after exactly N failures; DB `consecutive_failures` column must equal N. Tests at `tests/test_scheduler_quarantine_e2e.py::test_per_collector_quarantine_after_override` cover this.

## STAGE-001-009 — Subprocess plugin runner + JSON line protocol

1. **Subprocess plugin end-to-end via SubprocessCollector class factory** — `runbooks/_examples/hello-subprocess-plugin/plugin.yaml` should always parse, spawn, and emit metrics correctly when run via `make_subprocess_collector` + `run_subprocess`. Test: `tests/test_subprocess_runner_e2e.py::test_hello_world_plugin_via_collector_class_factory`. Re-run after any change to manifest schema, runner protocol parsing, or class factory.
2. **All 5 JSON protocol line types** — metric/event/log/heartbeat/result must all parse correctly in a single subprocess run. Test: `test_all_five_protocol_line_types_parsed_correctly`. Re-run after any change to subprocess_runner's `_drain_stdout` parser or any new line type addition.
3. **Timeout escalation: SIGTERM → 2s grace → SIGKILL** — Subprocess plugin that ignores SIGTERM must be SIGKILLed within `timeout + SIGTERM_GRACE_SECONDS` wall-clock. Test: `test_timeout_sigterm_then_sigkill_wall_clock`. Re-run after any change to timeout enforcement, signal handling, or `start_new_session`/`os.killpg` invocation.
4. **Untrusted plugin secrets filtering** — Subprocess plugin's `secrets: [...]` manifest declaration must be the ONLY secrets visible to the subprocess (via filtered `SyncSecretsResolver`). Test: `test_untrusted_plugin_secrets_filtered_to_manifest_declarations`. Re-run after any change to `SyncSecretsResolver.filtered`, stdin payload construction, or trust-tier dispatch.
5. **Loader.persist_to_db idempotency** — Calling `await loader.persist_to_db(repo)` twice on the same registered set must produce no duplicates and no errors. Closes STAGE-001-008's loader-INSERT gap. Test: `test_loader_persist_to_db_inserts_and_is_idempotent`. Re-run after any change to `loader.persist_to_db` SQL or schema migrations on the `collectors` table.

## STAGE-001-009: Subprocess plugin runner + JSON line protocol

- [ ] A bash hello-world plugin produces metrics visible via the API
- [ ] Plugin timeout kills the subprocess and records a failure
- [ ] Malformed JSON on stdout is logged, doesn't crash the host
- [ ] Non-zero exit code marks the run as failed and emits the failure metric

## STAGE-001-010 Deferred — BaseHTTPMiddleware blocks streaming SSE under httpx ASGITransport

**Date filed:** 2026-05-06
**Affected tests:**
- `tests/test_api_events_sse.py::test_sse_http_endpoint_smoke`
- `tests/test_lifespan_e2e.py::test_lifespan_e2e_sse_receives_tick`

**Status:** xfail with documented `reason=` parameter; not failing the suite

**Symptom:** SSE HTTP endpoint tests time out after 5 seconds. Subscribers connect but never receive published events.

**Root cause:** FastAPI's `BaseHTTPMiddleware` (used as base class by `RequestIdMiddleware`, `AccessLogMiddleware`, `DevAuthMiddleware`) wraps the ASGI app in a way that BUFFERS streaming responses until the response generator completes. SSE relies on the response generator yielding events incrementally over a long-lived connection; buffering means clients see nothing until the connection closes. This is a known limitation: https://github.com/encode/starlette/issues/919, https://www.starlette.io/middleware/#limitations.

**Why not blocking:** The SSE broker logic (subscribe/publish/replay/overflow/concurrent/non-throwing/event_seq) is fully covered by 7 passing unit tests in `test_api_events_sse.py` that exercise the broker directly without going through the HTTP layer. The end-to-end HTTP delivery path is the only thing untested.

**Resolution path:** Migrate the 3 middleware classes from `BaseHTTPMiddleware` subclasses to pure ASGI callables (`async def __call__(self, scope, receive, send)`). This is a non-trivial refactor; the dispatch/response handling has to be rewritten without the `call_next` abstraction. Estimated 1-2 hours of work + verification.

**Targeted resolution:** STAGE-001-014 (UI shell + login + Overview live-tile) is the FIRST stage where the React frontend's SSE consumer needs HTTP delivery to actually work. That stage will discover the issue if not already resolved; resolving in STAGE-001-014's Design or Build is the natural fit. Alternatively, STAGE-001-013 (alert ingestor + dispatcher; first SSE-consuming alert channel) could resolve it earlier.

**Workaround in current code:** The xfail decorator on each test carries a long, explicit `reason=` string so the deferral is visible in test output and won't silently pass if accidentally fixed.

## STAGE-001-010: FastAPI app shell + healthz + structured logging

- [ ] `/api/healthz` returns 200 with `{ok: true}` from a cold start
- [ ] Log lines are valid JSON (jq parses them)
- [ ] An intentional 500 produces a uniform error envelope

## STAGE-001-011: Local auth

- [ ] [D][M] Login page renders, accepts credentials, redirects to overview
- [ ] Wrong password attempts are rate-limited
- [ ] Logout clears the session cookie
- [ ] CSRF token is required for POST/PUT/DELETE; missing token returns 403
- [ ] Session expiry is enforced (test with short TTL)

## STAGE-001-011 Regression Watch — Streaming endpoints MUST have explicit auth dependency

**Date filed:** 2026-05-06

**Pattern:** Any FastAPI route that returns a streaming response (`StreamingResponse`, `EventSourceResponse`, async generators) MUST declare an explicit `Depends(require_*)` in its signature.

**Why:** Without an auth dependency, unauthenticated requests cause the response generator to begin streaming. Tests using `httpx.AsyncClient` against such endpoints WAIT INDEFINITELY for response data → infinite hang in test suite.

**Real incident:** STAGE-001-011 Build phase. `/api/events` was missing `Depends(require_session())`. A test calling the endpoint without auth caused pytest to hang indefinitely. 17 minutes of CI wall-clock wasted before diagnosis. Fix: add the dep — covered in STAGE-011's commit.

**Detection:** code-review-graph rule (future): every router function returning `StreamingResponse | EventSourceResponse | AsyncIterator` requires at least one `Depends(require_*)` parameter.

**Resolution path:** STAGE-001-014 (UI shell — first frontend SSE consumer) or any future stage adding streaming endpoints must verify this. Add `pytest-timeout` to deps in a future stage to bound any future hang to 30s.

**Status:** OPEN (preventive — applies to all future streaming endpoints)

## STAGE-001-012: First built-in `host` collector

- [ ] `GET /api/collectors` returns `host` with `last_run_at` populated and updating across two calls
- [ ] `GET /api/metrics/snapshot` returns `homelab_host_cpu_percent` with `cpu="all"` label and at least one per-core entry
- [ ] `homelab_host_disk_bytes` emitted for `/` and (when mounted) `/rackstation`
- [ ] `homelab_collector_run_success_total{name="host"}` increments on each tick
- [ ] `homelab_collector_run_duration_seconds{name="host"}` records sane values (under timeout)

## STAGE-001-012 Regression Watch — Top-N families must remain bounded

**Date filed:** 2026-05-06

**Pattern:** Counters and gauges that track top-N entities (processes by CPU, processes by memory) must use epoch semantics to prevent unbounded cardinality growth.

**Implementation:** `MemoryRetainingMetricsWriter.replace_family(name, entries)` atomically clears prior entries for a family and writes the new top-N. This mirrors `topk()` behavior on a real VM.

**Why:** If top-N families are emitted with standard `write_gauge()` (append-only), new processes appearing and disappearing will create unique (name, labels) tuples indefinitely. After weeks of uptime, the snapshot endpoint returns thousands of stale process entries with zero-value metrics.

**Detection:** Iterate the collector 100 times; assert `len(writer._latest)` for top-N families stays ≤ `2 * top_n_processes` (one epoch of new, one epoch being replaced). Test: `test_memory_retaining_writer.py::test_replace_family_epoch_semantics`.

**Status:** Monitored via regression checklist below.

## STAGE-001-012: First built-in `host` collector

- [ ] `GET /api/collectors` returns `host` with `last_run_at` populated and updating across two calls
- [ ] `GET /api/metrics/snapshot` returns `homelab_host_cpu_percent` with `cpu="all"` label and at least one per-core entry
- [ ] `homelab_host_disk_bytes` emitted for `/` and (when mounted) `/rackstation`
- [ ] `homelab_collector_run_success_total{name="host"}` increments on each tick
- [ ] `homelab_collector_run_duration_seconds{name="host"}` records sane values (under timeout)

## STAGE-001-012 Regression Items Added

- [ ] `GET /api/metrics/snapshot` (authenticated) returns 200 with `{ts, entries: [...]}` shape; entries include the 11 host metric families after at least one collector tick.
- [ ] `GET /api/metrics/snapshot` without session cookie returns 401.
- [ ] `GET /api/metrics/snapshot` with tampered/expired session cookie returns 401.
- [ ] `MemoryRetainingMetricsWriter.replace_family()` is atomic — top-N families (`homelab_host_top_processes_cpu_percent`, `homelab_host_top_processes_memory_bytes`) bounded to ≤ 10 entries each in the snapshot regardless of how many ticks have run.
- [ ] HostCollector handles per-section psutil failures (NoSuchProcess, FileNotFoundError on extra_mountpoints) without aborting the whole tick.
- [ ] HostCollector with a base `InMemoryMetricsWriter` (not retaining) silently skips top-N families.
- [ ] `utc_now_iso()` produces `+00:00`-suffixed ISO timestamps (NOT `Z`-suffix); snapshot endpoint preserves this.
- [ ] Coverage gate `fail_under = 100` in `apps/monitor/pyproject.toml` is met by `make test`.

## STAGE-001-013: Alert ingestor + first `inproc-dashboard` channel

- [ ] POSTing an Alertmanager-shaped payload to `/api/alerts/ingest` produces a row in `alerts`
- [ ] Same fingerprint posted twice is deduped (one row, counter incremented)
- [ ] Channel receives the alert and the dashboard SSE stream emits it
- [ ] `POST /api/alerts/ingest` with cookie auth + CSRF → 202 with `{received, ingested}` body shape; row appears in `alerts` table with correct fingerprint, severity, source_tool.
- [ ] `POST /api/alerts/ingest` with `Bearer homelab_<env>_<token>` token (scope `alerts:ingest:write`) → 202.
- [ ] Same fingerprint posted twice while firing → 1 row, `last_seen_at` advanced, `opened_at` stable.
- [ ] Resolution payload after firing → `resolved_at` set; `alert.resolved` SSE event published.
- [ ] Refire after resolve → NEW row inserted (D4 design decision); 2 rows total with same fingerprint.
- [ ] `GET /api/alerts` requires session cookie (token auth rejected with 401); filters `status`, `severity`, `source_tool`, `fingerprint` work.
- [ ] `GET /api/alerts/{id}` returns alert + outcome history; 404 for unknown id.
- [ ] `POST /api/alerts/{id}/ack` (with CSRF) creates outcome row + sets `ack_at`/`ack_by`. Without CSRF → 403.
- [ ] `POST /api/alerts/{id}/dismiss` creates outcome row; alert status unchanged.
- [ ] Scheduler quarantine: 3 consecutive collector failures → quarantine entered → `alert.firing` SSE event + alerts row with `source_tool='scheduler'`. `Scheduler.clear_quarantine(name)` → `alert.resolved` event + `resolved_at` set.
- [ ] Quarantine after clear → NEW row inserted (D5 reuses D4 dedup story).
- [ ] FailureBudget without alert_repo/dispatcher (defensive None defaults) does NOT raise; skips alert dispatch silently.
- [ ] AlertDispatcher with a failing channel does NOT raise; logs WARNING, increments `_delivery_failures` counter; OTHER channels still receive the event.
- [ ] Anonymous request to any `/api/alerts/*` endpoint → 401.
- [ ] Privacy: INFO log lines NEVER contain full alert payload (label dicts).
- [ ] Migration 0005 is additive (no row backfill required); SQLite batch_alter_table handles FK-having tables.

## STAGE-001-014: UI shell + login + Overview live-tile

- [ ] [D][M] Login → Overview screen → live tile updates every 10s
- [ ] [D][M] Logout from menu, login again, state restored
- [ ] [D][M] Empty/error states render correctly when monitor is restarted

## STAGE-001-015: VictoriaMetrics + vmagent

- [ ] `host` collector data queryable in VM via `vmui` and `/api/v1/query`
- [ ] vmagent reload via `/-/reload` picks up a config change without restart
- [ ] VM snapshot endpoint produces a usable backup

## STAGE-001-015A: Backup + disk budget + minimal test rig extension

- [ ] `POST /api/admin/backup` produces a SQLite snapshot file and a non-empty VM snapshot directory
- [ ] After 8 daily backups, the oldest is automatically deleted (retention default = 7)
- [ ] `homelab_self_disk_used_pct` metric reflects the actual size on disk
- [ ] Synthetic >95% disk usage trips auto-shrink (VM retention drops one tier; audit row written)
- [ ] `scripts/run-integration.sh` exits 0 against the test rig

## STAGE-001-016: VictoriaLogs + vector

- [x] GET /api/logs/query with cookie+CSRF returns 200 + LogsQueryResponse shape parsed from VL native NDJSON.
- [x] GET /api/logs/query without auth returns 401.
- [x] GET /api/logs/query with VL HTTP 500 surfaces as 502 upstream_unavailable.
- [x] GET /api/logs/query with expr length > 4096 returns 400 invalid_expr.
- [x] GET /api/logs/streams returns the in-process state populated by LogStreamBudgetCollector.
- [x] VictoriaLogsWriter.ingest() puts on bounded queue without blocking; QueueFull drops + increments dropped_count.
- [x] VictoriaLogsWriter flusher batches up to 100 events / 1s timeout, POSTs NDJSON to /insert/jsonline.
- [x] VictoriaLogsWriter on HTTP error increments error_count + logs warning, doesn't crash worker.
- [x] VictoriaLogsWriter.aclose() drains remaining queue then returns.
- [x] MultiplexLogsWriter fans out ingest() to all wrapped writers in registration order.
- [x] LogStreamBudgetCollector at 60s queries VL stats, emits homelab_log_stream_bytes_today + lines_per_sec gauges.
- [x] LogStreamBudgetCollector on VL HTTP failure returns CollectorResult with errors populated.
- [x] docker compose -f deploy/compose/docker-compose.yml config exits 0 (compose validity with new VL + vector).
- [x] deploy/vector/vector.toml is well-formed TOML.
- [x] data_vl volume mounted RW into victorialogs and RO into monitor (for SelfDiskCollector "vl" slot accounting).
- [x] HOMELAB_MONITOR_VL_RETENTION_DAYS env var documented in .env.example with default 30.
- [x] load_log_stream_budget_config() returns DiskBudgetConfig defaults when YAML absent; YAML overrides values.

## STAGE-001-017: Alertmanager + vmalert (metrics) + first rule

- [ ] Synthetic high-CPU triggers vmalert; webhook hits `/api/alerts/ingest`
- [ ] Alert reaches the in-process dashboard channel
- [ ] When the metric recovers, "resolved" notification is emitted

### STAGE-001-017 (Alertmanager + vmalert metrics)

- [ ] Run `docker compose -f deploy/compose/docker-compose.test.yml up -d alertmanager vmalert-metrics victoriametrics`. Verify alertmanager `/-/healthy` returns OK and vmalert `/api/v1/rules` shows all 6 rules (`HostHighCPU`, `HostHighMemory`, `CollectorQuarantined`, `SelfDiskWarn`, `SelfDiskError`, `SelfDiskCritical`).
- [ ] Boot a fresh monitor instance with empty DB. Verify `alertmanager-ingest` token row is auto-minted, secret `alertmanager-ingest-token` is created, audit log has `who="system:alertmanager-bootstrap"`. Boot a SECOND time. Verify only ONE token row remains and no new mint audit row was added.
- [ ] After STAGE-001-018 emits `homelab_collector_quarantine_count`: verify the `CollectorQuarantined` vmalert rule fires when a collector quarantines, routes through Alertmanager → `/api/alerts/ingest`, and produces an `alert.firing` SSE event.
- [ ] Verify `HOMELAB_MONITOR_ALERTMANAGER_URL=disabled` env var causes render-on-boot to skip the AM `/-/reload` call.
- [ ] When AM template path is set, verify rendered config contains the auto-minted token plaintext that matches `secrets.get("alertmanager-ingest-token")`.

## STAGE-001-018: vmalert (logs) + first log-derived rule

- [ ] LogsQL rule fires when a planted "Out of memory" pattern appears in vector input
- [ ] Webhook is delivered with `source_tool="vmalert-logs"` (not `vmalert-metrics`) on the alert row
- [ ] After planted lines stop, `SshFailedLoginBurst` alert auto-resolves within 10 minutes
- [ ] `KernelOOM` alert fires at severity `critical` (not `warning`)

## STAGE-001-019: Karma + kthxbye

- [ ] [D][M] Karma iframe renders inside the Alerts screen
- [ ] [D][M] Ack button creates a silence visible in Alertmanager API
- [ ] kthxbye keeps the silence alive while the alert is firing
- [ ] When the alert resolves, kthxbye lets the silence expire

## STAGE-001-020: Grafana + dashboards-as-code provisioning

- [ ] [D][M] Grafana renders the provisioned default dashboard with live data
- [ ] [D][M] Editing a dashboard JSON in `deploy/grafana/dashboards/` and restarting Grafana picks up the change

## STAGE-001-014: UI shell + login + Overview live-tile

- Login: empty submit shows "Username is required" / "Password is required"; wrong creds shows 401 message; rate-limited shows 429 with countdown when Retry-After present.
- Login form has working show/hide password toggle (Eye / EyeOff icons).
- "Welcome — please run hm user create" message shows when `/api/version` returns `users_configured: false`.
- After successful login, browser is redirected to `/overview` and `/api/auth/me` returns the user.
- Overview page: HostCpuTile shows "Connecting…" briefly, then a big number CPU% + sparkline. Sparkline starts as a 60-point flat baseline at the initial value; new SSE ticks produce visible deltas. Sparkline does not flatline between ticks.
- `/api/metrics/snapshot` is called ONCE on mount, then once per SSE tick (~10s) — not multiple times per second. `refetchOnWindowFocus` is OFF.
- "Updated" line displays HH:MM:SS in the user's local TZ; date prefix appears only when the timestamp's calendar date differs from today.
- AppShell sidebar: 13 items, Overview enabled, others show "Coming soon" tooltip, all disabled.
- Top bar: collapse-sidebar (desktop) / open-mobile-sidebar (mobile), search placeholder (disabled), Notifications (disabled, "Coming soon" tooltip, no yellow dot), user menu (theme toggle, sign out).
- Theme toggle persists across page reloads via localStorage.
- Mobile (< 768px viewport): sidebar is a full-screen overlay with backdrop; opens via hamburger; closes via close button, backdrop click, or any nav item click. Body scroll-locked while open.
- Desktop (≥ 768px viewport): sidebar is persistent; hamburger toggles `collapsed` width.
- Logging in from a 2nd device does NOT revoke the 1st device's session. Both sessions remain valid until TTL or change-password.
- Change-password still revokes all sessions for the user (post-incident posture preserved).
- The 2 SSE pytest tests (`test_sse_http_endpoint_smoke`, `test_lifespan_e2e_sse_receives_tick`) pass against the in-process uvicorn fixture.
- `make verify` exits 0 deterministically (no flakes from probabilistic tamper tests, no async timing flakes).

## STAGE-001-015: VictoriaMetrics + vmagent

- [ ] `GET /metrics` endpoint serves Prometheus exposition format without auth. Verify by running: `curl -sS http://localhost:9090/metrics | head -20` — should show Prometheus text starting with `# HELP` lines.
- [ ] `GET /api/metrics/range` proxies to VictoriaMetrics. Verify with VM running: authenticated GET `/api/metrics/range?expr=homelab_host_cpu_percent&start=...&end=...&step=10s` returns matrix data.
- [ ] `GET /api/metrics/snapshot` continues to return latest in-memory values (regression after writer multiplex landing).
- [ ] HostCpuTile range backfill happy path. With VM running, opening Overview should populate the sparkline with 60 historical points within 1-2s. SSE-driven appends continue afterward.
- [ ] HostCpuTile range backfill graceful failure. With VM unreachable (502/timeout), the synthetic baseline retains; tile remains functional via snapshot + SSE.
- [ ] `docker compose -f deploy/compose/docker-compose.yml config` exits 0 (compose file validity).
- [ ] `docker build --check -f apps/monitor/Dockerfile .` exits 0 (Dockerfile syntax validity).
- [ ] Backfill race regression — HostCpuTile must apply range backfill even when SSE ticks first. Verify by running `cd apps/ui && pnpm exec vitest run src/components/tiles/HostCpuTile.test.tsx` — the test "applies range backfill even after SSE has ticked" must pass.

## STAGE-001-015A: Backup + disk budget + minimal test rig extension

- [ ] `POST /api/admin/backup` with cookie+CSRF returns 200 + BackupResponse. SQLite snapshot file created at returned sqlite_path. Audit row written.
- [ ] `POST /api/admin/backup` with API token bearing scope admin:backup:write returns 200.
- [ ] `POST /api/admin/backup` without auth returns 401; without CSRF returns 403; with token lacking scope returns 403.
- [ ] `hm backup run` with unreachable VM exits 1, errors list mentions VM, but SQLite snapshot IS created at the expected path.
- [ ] `hm backup list` with empty backup root prints `{"sqlite": [], "vm": []}` exit 0.
- [ ] `hm backup retention --keep N` applies retention, removes files beyond Nth-most-recent, returns count via JSON.
- [ ] SelfDiskCollector emits homelab_self_disk_used_bytes/budget_bytes/used_pct gauges. At >95% used_pct, emits homelab_self_disk_shrink_total{tier} counter + writes critical-severity audit row.
- [ ] `docker compose -f deploy/compose/docker-compose.test.yml config -q` exits 0 (test rig compose validity).
- [ ] `docker build --check -f apps/monitor/Dockerfile.test .` exits 0.
- [ ] `bash -n scripts/run-integration.sh` exits 0 (script syntax valid).
- [ ] `deploy/vmalert/metrics/self_disk.yaml` parses as YAML, contains 3 alert rules (SelfDiskWarn, SelfDiskError, SelfDiskCritical).
- [ ] `load_disk_budget_config()` returns DiskBudgetConfig defaults when YAML file missing; HOMELAB_MONITOR_DISK_BUDGET_GB env overrides total_gb.

## STAGE-001-021: Full integration test rig + canonical e2e test

- [ ] `docker compose -f deploy/compose/docker-compose.test.yml up --abort-on-container-exit --exit-code-from integration-tests` exits 0
- [ ] Canonical e2e test exercises: collector → VM → vmalert → AM → ingestor → channel
- [ ] CI runs the integration suite on every PR
