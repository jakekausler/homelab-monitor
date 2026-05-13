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
