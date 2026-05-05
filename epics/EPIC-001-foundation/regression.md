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

## STAGE-001-005: Encrypted secrets store

- [ ] `hm secrets set FOO bar` then `hm secrets get FOO` round-trips
- [ ] Master-key rotation via `hm secrets rotate-master` re-encrypts all rows; old key fails to decrypt
- [ ] Secret value never appears in any log file (search the journal post-test)

## STAGE-001-006: Collector protocol + base classes

- [ ] A noop collector implements the protocol with `pyright --strict` clean
- [ ] Tests cover all `RunKind` values (ASYNC / THREAD / PROCESS)

## STAGE-001-007: In-process plugin loader + scheduler

- [ ] A scheduled tick fires within ±100ms of its target time
- [ ] Multiple collectors with different intervals all fire correctly over a 60s test window
- [ ] Per-collector timeout is enforced (a collector that sleeps past timeout is killed)

## STAGE-001-008: Concurrency groups + failure budget + quarantine

- [ ] Two collectors in the same group never run concurrently (timing test)
- [ ] After N consecutive failures, collector enters quarantine and emits an alert
- [ ] Manual "retry now" via API exits quarantine

## STAGE-001-009: Subprocess plugin runner + JSON line protocol

- [ ] A bash hello-world plugin produces metrics visible via the API
- [ ] Plugin timeout kills the subprocess and records a failure
- [ ] Malformed JSON on stdout is logged, doesn't crash the host
- [ ] Non-zero exit code marks the run as failed and emits the failure metric

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

## STAGE-001-012: First built-in `host` collector

- [ ] `GET /api/collectors` returns `host` with `last_run_at` populated and updating across two calls
- [ ] `GET /api/metrics/snapshot` returns `homelab_host_cpu_percent` with `cpu="all"` label and at least one per-core entry
- [ ] `homelab_host_disk_bytes` emitted for `/` and (when mounted) `/rackstation`
- [ ] `homelab_collector_run_success_total{name="host"}` increments on each tick
- [ ] `homelab_collector_run_duration_seconds{name="host"}` records sane values (under timeout)

## STAGE-001-013: Alert ingestor + first `inproc-dashboard` channel

- [ ] POSTing an Alertmanager-shaped payload to `/api/alerts/ingest` produces a row in `alerts`
- [ ] Same fingerprint posted twice is deduped (one row, counter incremented)
- [ ] Channel receives the alert and the dashboard SSE stream emits it

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

- [ ] vector tails docker logs and a planted log line is queryable in VL
- [ ] Per-stream byte cap kicks in on a flooded stream

## STAGE-001-017: Alertmanager + vmalert (metrics) + first rule

- [ ] Synthetic high-CPU triggers vmalert; webhook hits `/api/alerts/ingest`
- [ ] Alert reaches the in-process dashboard channel
- [ ] When the metric recovers, "resolved" notification is emitted

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

## STAGE-001-021: Full integration test rig + canonical e2e test

- [ ] `docker compose -f deploy/compose/docker-compose.test.yml up --abort-on-container-exit --exit-code-from integration-tests` exits 0
- [ ] Canonical e2e test exercises: collector → VM → vmalert → AM → ingestor → channel
- [ ] CI runs the integration suite on every PR
