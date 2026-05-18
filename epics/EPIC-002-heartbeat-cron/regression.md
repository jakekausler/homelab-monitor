# Regression Checklist - EPIC-002: Heartbeat + cron registry

(Items added per stage during Refinement.)

## STAGE-002-001 (heartbeat receiver)

- [ ] `make verify` GREEN: 1184+ backend tests pass, kernel coverage 100%, UI tests pass
- [ ] `POST /api/hb/<unknown-id>/ok` returns 404 (NotFoundProblem RFC 7807 body) — guards against accidental auto-create regression
- [ ] `POST /api/hb/<id>/ok?foo=bar` returns 422 — guards against `extra='forbid'` enforcement regressing if `query_model()` is removed or replaced with `Depends()` again
- [ ] Vertical slice: insert cron via SQL → POST /start, /ok, /fail → DB shows correct state transitions, audit_log has 3 rows, crons.last_seen_state mirror matches heartbeats_state.current_state
- [ ] Alembic 0006 down + up cycle preserves schema parity (idempotent migration)
- [ ] Heartbeat router rate-limit 429 path stays unit-tested (parametrized over /start, /ok, /fail in `test_rate_limit_returns_429_with_retry_after_per_verb`)

## STAGE-002-002 (Cron registry CRUD + Inventory UI)

- [ ] **GET /api/crons** — paginated list responds 200 with seeded rows; filters by host, enabled, state, q substring work
- [ ] **GET /api/crons/{id}** — returns cron + heartbeats_state; 404 on missing or hidden
- [ ] **PATCH /api/crons/{id}** — updates only changed fields; audit `before`/`after` JSON contains only diff; empty-diff PATCH returns 200 with NO audit row; `crons.update` audit verb
- [ ] **DELETE /api/crons/{id}** — sets `hidden_at`; subsequent GET excludes by default; `?include_hidden=1` re-includes; `crons.hide` audit verb
- [ ] **Restore via PATCH {hidden_at: null}** — restores hidden cron; `crons.unhide` audit verb
- [ ] **Receiver behavior on hidden cron** — POST /hb/{fp}/ok returns 404 (no audit row); cross-stage edit to STAGE-002-001's repository._SELECT_CRON_SQL
- [ ] **Schedule preview endpoints** — GET /api/crons/{fp}/preview-runs?count=3 and POST /api/crons/preview-runs both return upcoming runs
- [ ] **CronsTab** — table renders all seeded crons; filter dropdowns reflect title case; state badges render (Ok/Failed/Late/Unknown); search debounces 250ms; no "+ Add cron" button
- [ ] **CronDetail** — opens via row click → /inventory/crons/{fp}; loads cron data; Save changes button fires PATCH and navigates back to list; Archive button opens confirm modal with "Archive cron?" heading
- [ ] **ConfirmDeleteModal** — heading "Archive cron?"; button text "Archive"/"Archiving…" when isDeleting; type-cron-name to enable
- [ ] **Mobile (390px)** — Archive modal scrolls with max-h-[calc(100vh-2rem)] overflow-y-auto; CronsTable collapses to mobile layout

## STAGE-002-003 (Cron schema redesign — fingerprint identity, drop integration_mode, rename archived_at→hidden_at)

- [x] **compute_fingerprint stability** — `compute_fingerprint(host, source_path, schedule, command)` returns 64-char lowercase hex SHA256; same tuple → same fp; deterministic across processes
- [x] **compute_fingerprint sensitivity** — Changing any of (host, source_path, schedule, command) produces a distinct fp; NULL source_path ≠ empty string source_path
- [x] **Migration 0008 upgrade** — `alembic upgrade head` from any prior revision applies 0008 cleanly; schema has `fingerprint PK`, `hidden_at`, `source_path`, `wrapper_installed_at`; no `id`, `integration_mode`, or `archived_at`
- [x] **Migration 0008 downgrade** — `alembic downgrade 0007` restores legacy schema; all post-redesign data lost (documented destructive behavior)
- [x] **Migration 0008 seed gating** — `HOMELAB_MONITOR_INCLUDE_DEMO_SEEDS=1` enables 4 demo seed rows on upgrade; unset = no seeds (production default)
- [x] **Migration 0008 seed audit_log bypass** — Seed inserts emit ZERO `audit_log` rows (per D5); only runtime discovery emits `crons.discover`
- [x] **Heartbeat receiver fingerprint URLs** — `POST /api/hb/{fingerprint}/ok|fail|start` returns 204 on known fingerprint; 404 on unknown fingerprint (incl. zeros)
- [x] **Heartbeat receiver hidden cron 404** — `POST /api/hb/{fp}/ok` where `crons.hidden_at IS NOT NULL` returns 404 (D2a cross-stage; `_SELECT_CRON_SQL` filters)
- [x] **API CronCreate field shape** — POST /api/crons accepts `host, name, command, schedule, cadence_seconds, expected_grace_seconds, enabled, source_path`; no `integration_mode`; server computes fingerprint as PK
- [x] **API CronUpdate whitelist** — PATCH /api/crons/{fp} accepts ONLY `name, expected_grace_seconds, enabled, hidden_at`; rejects all other fields with HTTP 422 (`extra_forbidden`)
- [x] **API CronOut field shape** — GET /api/crons and /api/crons/{fp} return `fingerprint, hidden_at, source_path, wrapper_installed_at`; do NOT return `id`, `integration_mode`, or `archived_at`
- [x] **Audit verb taxonomy** — `crons.create`, `crons.update`, `crons.hide`, `crons.unhide`; PATCH that sets `hidden_at != null` emits `crons.hide`; PATCH that sets `hidden_at = null` emits `crons.unhide`

## STAGE-002-004 (Manual-create API removal — POST /api/crons gone, AddCronModal gone, CronForm edit-only)

- [x] **POST /api/crons returns 405 Method Not Allowed** — FastAPI emits 405 with `Allow: GET` header automatically when the GET route exists but POST does not
- [x] **AddCronModal component does not exist** — no DOM tree contains an "Add cron" modal; verify by grep `AddCronModal` in apps/ui/src/ returns no matches
- [x] **CronsToolbar has no "+ Add cron" button** — toolbar contains only filter controls (host/state/search + include-hidden checkbox)
- [x] **CronForm renders edit-only** — fields visible: name, expected_grace_seconds, enabled. No scheduleMode radio, no schedule input, no cadence_seconds input. Submit always sends CronUpdate payload (PATCH only)
- [x] **Archive button has no confirmation modal** — clicking Archive on the cron detail page immediately calls soft-delete + navigates back to the list with `include_hidden=true`. No typed-name confirmation step. ConfirmDeleteModal component does not exist (grep returns 0 hits).

## STAGE-002-005 — /api/hb/{fingerprint}/register + hidden semantic change

When making changes to the heartbeat receiver or cron schema, re-verify:

1. **POST /api/hb/{fingerprint}/register happy path (201):**
   ```bash
   # Compute fingerprint from {host, source_path, schedule, command} (JSON canonical SHA256)
   # POST with heartbeat-write token → expect 201 + CronOut body
   ```

2. **Idempotent re-register (200 + no audit if wrapper=false):**
   Second POST with identical body and wrapper=false returns 200; audit_log count does NOT increase.

3. **Wrapper refresh (200 + audit row, D2 + D10 Path 3):**
   Second POST with wrapper=true refreshes `wrapper_last_seen_at` AND writes a `crons.register` audit row with before/after timestamps.

4. **422 fingerprint_mismatch detail flag:**
   POST to URL with mismatching fingerprint vs body → 422 with `error.details.fingerprint_mismatch: true`.

5. **422 invalid_schedule detail flag:**
   POST with `schedule: "not a cron"` → 422 with `error.details.invalid_schedule: true, reason: <str>`.

6. **Hidden cron heartbeat operations succeed (D5 architectural change):**
   Set a cron's `hidden_at` (via PATCH). Then POST /api/hb/{fp}/start, /ok, /fail — all must return 204 (NOT 404). Audit rows must still be written. The `_SELECT_CRON_SQL` filter must NOT include `AND hidden_at IS NULL`.

7. **Policy fields preserved on re-register (D7):**
   Operator PATCH'es `name`, `expected_grace_seconds`, `enabled`. Wrapper re-registers. Those operator-set values MUST be preserved; only `wrapper_last_seen_at` mutates.

8. **OpenAPI schema for /register:**
   `GET /api/openapi.json` must show `CronOut` schema reference for both 200 and 201 responses of POST /api/hb/{fingerprint}/register, and RegisterCronBody for the request body.

9. **Migration 0009 round-trip:**
   The `wrapper_installed_at` → `wrapper_last_seen_at` rename must round-trip cleanly via alembic upgrade + downgrade.

## STAGE-002-006 — UI redesign (4-panel detail page + Archive→Hide + Remote banner + sonner toasts)

When making changes to the cron inventory UI, re-verify:

1. **Cron detail page header:** cron name + StateBadge + "Remote" badge (when source_path null) + "Hidden" badge (when hidden_at set). Host · Command subtitle.
2. **4-panel grid layout:** at `lg:` (1024px+) renders 2x2; below 1024px stacks 1-column in order: Heartbeat state → Disk source → Monitoring policy → Actions.
3. **Heartbeat state panel:** current_state, current_streak, last_ok_at, last_fail_at, expected_next_at, last_duration_seconds, last_exit_code, "Wrapper last seen <ts>" OR "No wrapper installed".
4. **Disk source panel:** host, source_path, schedule, command. Blue info banner at top of panel when source_path is null: "Remote cron on `<host>`. The monitor doesn't have direct file access to this host. Wrapper-based heartbeats are the only signal."
5. **Monitoring policy panel:** CronForm editable (name, expected_grace_seconds, enabled). Save → `toast.success("Cron updated")` (sonner top-right).
6. **Actions panel:** Hide/Unhide button + disabled "Install heartbeat wrapper" button with tooltip ("Local install ships in STAGE-002-009. Remote install requires cross-host work in EPIC-015 / EPIC-017.").
7. **Hide button:** click → `toast.success("Cron hidden")` → STAYS on detail page (no navigation). Refetch shows "Hidden" badge in header. In-flight text: "Hiding…".
8. **Unhide button (on hidden cron):** click → `toast.success("Cron restored")`. In-flight text: "Unhiding…".
9. **No confirmation modal** — silent hide pattern (per STAGE-002-004 lock).
10. **No wrapper handshake history panel** (removed during Refinement of this stage — wrapper refreshes don't represent meaningful state changes).
11. **List view CronsTable:** Name + Host (with "Remote" badge for null source_path) + Schedule + State + Last OK + Wrapper column (✓ when wrapper_last_seen_at set) + Hidden column (badge when hidden_at set).
12. **List view CronsToolbar:** search, host select, state select, "Wrapper installed" dropdown (Any wrapper / Wrapper installed / No wrapper), "Show hidden" checkbox. NO "+ Add cron" button.
13. **Empty state copy:** "No crons yet. Crons will appear here once they are discovered or have registered a heartbeat."
14. **sonner Toaster** mounted at app root (top-right, richColors).
15. **sonner Toaster mount:** verify `<Toaster position="top-right" richColors />` is mounted in `apps/ui/src/routes/__root.tsx` inside the TooltipProvider, before the main `<Outlet />`. Toasts appear top-right and use richColors styling.
16. **`wrapper_installed` URL search param round-trips through bookmarks:** opening `/inventory/crons?wrapper_installed=true` filters the list to crons with `wrapper_last_seen_at !== null` AND the toolbar dropdown reflects "Wrapper installed". Setting/clearing the toolbar updates the URL. Bookmarking + reopening the URL restores the filter state.

## STAGE-002-007 (Cron auto-discovery) regression items

- [ ] Verify discoverer plugin runs on every container restart without quarantine block (Bug A regression check).
- [ ] Verify `upsert_discovered` overwrites `command` + `name` when stored values differ from current scan (Bug B regression check).
- [ ] Verify secret-scrub for `mysqldump -pPASS` still scrubs to `-p<redacted>` AND `cd / && run-parts --report /etc/cron.hourly` is NOT corrupted (regex regression).
- [ ] Verify container serves UI at `/` (200 + HTML).
- [ ] Verify `POST /api/crons/discover-now` returns 202 with scan-result body when authenticated.
- [ ] Verify `hm collector unquarantine` CLI works inside container.
- [ ] Verify ACLs on `/var/spool/cron/crontabs/*` persist after rerunning `scripts/host-setup.sh`.
- [ ] Verify `HOMELAB_MONITOR_BIND_HOST=0.0.0.0` exposes monitor on LAN; default `127.0.0.1` keeps localhost-only.
- [ ] Verify dev rig (19090) and prod compose stack (29090) can coexist without port conflicts.

## STAGE-002-007A (Auto-soft-delete crons) regression items

- [ ] Soft-delete reconciliation: after a discovery scan that no longer finds a cron's fingerprint in a cleanly-scanned source file, that cron is soft-deleted (`soft_deleted_at` set, `crons.soft_delete` audit row). When it reappears on a later clean scan, it auto-restores.
- [ ] An UNREADABLE source file (PermissionError) must NEVER cause its crons to be soft-deleted — verify by making a user crontab unreadable and confirming its crons are untouched.
- [ ] `GET /api/crons` hides soft-deleted rows by default; `?include_soft_deleted=true` includes them; `GET /api/crons/{fingerprint}` always returns soft-deleted rows (field at nested `cron.soft_deleted_at`).
- [ ] `POST /api/hb/{fp}/register` on a soft-deleted cron auto-restores it (two audit rows: `crons.restore` before `crons.register`).
- [ ] Heartbeat endpoints (`/start`,`/ok`,`/fail`) still work on a soft-deleted cron.
- [ ] UI: "Show soft-deleted" toolbar toggle, soft-deleted badge + dimmed row, CronDetail header badge + Disk-source field.
- [ ] Prod deploy: `make dev-prod` rebuilds the monitor image from the local Dockerfile; SPA deep-links (refresh on `/overview` etc.) return the app, not a 404.

## STAGE-002-008 (B-mode log-scrape) regression items

- [ ] B-mode log-scrape: a real cron firing on the host results in that cron's `observed_runs_total` incrementing and a `cron.observed_run` audit row, within ~1 min, with `current_state` staying `unknown` (NOT `ok`).
- [ ] Idempotency: replaying the same journald cron event (same `__CURSOR`) does not double-count.
- [ ] A wrapper-tagged log line carrying `exit=0`/`exit=N` drives `record_ok`/`record_fail` (state change); a bare CMD line is a neutral observed run.
- [ ] Ambiguous match (one event matching 2+ cron fingerprints) is skipped + counted, not fanned out.
- [ ] A secret-bearing cron command (stored scrubbed) still matches its raw log line via `canonical_log_key`.
- [ ] Deploy: `docker compose up -d` (full or partial, fresh or existing volumes) brings the stack up with Vector authenticating to `/api/internal/cron-events` and rendering `vector.toml` — no manual token step; `config-init` is idempotent and non-destructive.
- [ ] Vector journald source reads the host journal (host `/etc/machine-id` bind-mounted); cron events POST as a JSON array and return 202.

## STAGE-002-009 (Wrapper helpers — install + host-side executor + crontab-snapshot) regression items

- [D][M] The cron detail page "Install heartbeat wrapper" button: enabled for local crons, disabled with EPIC-017 tooltip for remote crons.
- [D][M] The InstallHeartbeatModal: opens with a dry-run preview (crontab diff + wrapper script), the modal is width-constrained to the viewport (mobile + desktop) with only the code blocks scrolling horizontally, the confirm checkbox gates the Install button.
- [ ] Backend: after a host re-runs `host-setup.sh`, the cron-apply executor units + crontab-snapshot units are installed and the old `crontab-acl` units are retired; user crontab files stay `0600` with no ACL (no vixie-cron INSECURE MODE).
- [ ] Backend: a wrapper install on a local user-crontab cron writes the wrapper script (`/usr/local/bin/cron-with-heartbeat.sh` 0755), the token (`/etc/homelab-monitor/heartbeat.token` 0644), rewrites the crontab line via the host-side executor, sets `wrapper_last_seen_at`, and the host cron daemon runs the wrapped line producing a `/start`+`/ok` heartbeat.

## STAGE-002-009A (Wrapper removal helpers — uninstall wrapper, restore original crontab line) regression items

- [D][M] The cron detail page Install/Remove toggle: shows "Install heartbeat wrapper" when `wrapper_installed` is false, "Remove heartbeat wrapper" when `wrapper_installed` is true (local crons only). The toggle signal is keyed on the stored `wrapper_installed` boolean column, not `wrapper_last_seen_at`.
- [D][M] The RemoveHeartbeatModal: opens with a dry-run un-wrap preview (crontab diff showing the restoration), the modal is width-constrained to the viewport (mobile + desktop) with only the code blocks scrolling horizontally, the confirm checkbox gates the Remove button.
- [ ] Wrapper uninstall: `POST /api/crons/{fingerprint}/uninstall-wrapper` with `confirm=false` returns the un-wrap diff preview without modifying the crontab. With `confirm=true`, reverts the crontab line to its original form (byte-exact).
- [ ] Not-wrapped cron uninstall gate: `POST /api/crons/{fingerprint}/uninstall-wrapper` on a cron that is not wrapped returns 409 NotWrappedError (the gate keys on the discovered crontab line's `is_wrapped()`, not on `wrapper_last_seen_at`).
- [ ] Wrapper removal byte-exact round-trip: install a cron's wrapper → compute its fingerprint A → uninstall the wrapper → discover the cron again → fingerprint is still A (no mutation across install→uninstall cycle).
- [ ] Wrapper script cleanup: the shared `/usr/local/bin/cron-with-heartbeat.sh` is NOT deleted on a per-cron uninstall (never deleted per D1; harmless when unreferenced).
- [ ] Token file cleanup: `/etc/homelab-monitor/heartbeat.token` is NOT touched on a per-cron uninstall (never deleted per D2; one shared token for all wrapped crons).
- [ ] `wrapper_last_seen_at` clear: after a successful uninstall, `wrapper_last_seen_at` is set to NULL via a single atomic transaction (audit row `crons.wrapper_uninstalled` + column clear).
- [ ] Host-side executor snapshot refresh: after a wrap or unwrap operation, the executor (hm-cron-apply.sh) refreshes the crontab snapshot inline with a `refresh_user_snapshot` call; executor journal shows "snapshot refreshed: <user>" with zero mktemp errors; the ProtectSystem=strict service unit includes `/var/lib/homelab-monitor/crontab-snapshot` in ReadWritePaths.
- [ ] Rollback on uninstall failure: if the uninstall operation fails partway through (e.g., crontab write fails), the original wrapped crontab line is restored via the executor's snapshot+rollback machinery; no half-reverted state is left behind.
- [ ] CLI uninstall: `hm cron uninstall-wrapper <fingerprint>` mirrors the UI button with the same dry-run-then-confirm flow; works on the local host only.
- [ ] Standalone remote CLI uninstall: the `install_wrapper_remote.py` script gains an `--uninstall` mode that scans local crontabs for wrapped lines, lets the user pick one, reverses the wrap (same atomic snapshot+rollback pattern), and restores the original crontab line.

## STAGE-002-010 (vmalert rules — wrapper-health alert + monitoring-health channel) regression items

- [ ] The monitor's `/metrics` endpoint emits the 6 homelab_heartbeat_* metric families per non-hidden, non-soft-deleted cron; a cron that becomes hidden or soft-deleted has its series dropped on the next collector tick (via replace_family atomic swap).
- [ ] vmalert evaluates `deploy/vmalert/metrics/heartbeats.yaml` rules: HeartbeatStale_Warning/Error/Critical, HeartbeatFailed, HeartbeatFlapping, WrapperPossiblyStale; recording rules homelab:heartbeat_overdue_count, homelab:heartbeat_fail_count, homelab:heartbeat_runtime_p95_seconds, homelab:wrapper_stale_count.
- [ ] WrapperPossiblyStale carries `routing_channel=monitoring-health` and routes to the monitoring-health-channel Alertmanager receiver, distinct from cron-health.
- [ ] The full alert loop (monitor `/metrics` → vmagent → VictoriaMetrics → vmalert → Alertmanager → POST `/api/alerts/ingest` → GET `/api/alerts`) delivers alerts with correct routing; Alertmanager webhook URL uses the compose service name `monitor`, not hostname.
- [ ] The cron-detail API response carries a wrapper_health enum (ok/stale/unknown); the CronDetail wrapper-health badge renders it only when wrapper_installed is true; the heartbeat-state panel labels expected_next_at as "Overdue after".
- [ ] vmalert-metrics requires `-remoteWrite.url` (base URL, no `/api/v1/write` suffix) for recording rules; both vmalert healthchecks use `/-/healthy` endpoint.
