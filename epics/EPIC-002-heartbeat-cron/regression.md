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

- [ ] **GET /api/crons** — paginated list responds 200 with seeded rows; filters by host, integration_mode, enabled, state, q substring work
- [ ] **GET /api/crons/{id}** — returns cron + heartbeats_state; 404 on missing or archived
- [ ] **POST /api/crons** — creates cron + audit row; 409 on duplicate (host, command); CSRF required; `crons.create` audit verb
- [ ] **PATCH /api/crons/{id}** — updates only changed fields; audit `before`/`after` JSON contains only diff; empty-diff PATCH returns 200 with NO audit row; `crons.update` audit verb
- [ ] **DELETE /api/crons/{id}** — sets `archived_at`; subsequent GET excludes by default; `?include_archived=1` re-includes; `crons.delete` audit verb
- [ ] **Restore via PATCH {archived_at: null}** — restores archived cron; `crons.restore` audit verb
- [ ] **Receiver behavior on archived cron** — POST /hb/{id}/ok returns 404 (no audit row); cross-stage edit to STAGE-002-001's repository._SELECT_CRON_SQL
- [ ] **Schedule preview endpoints** — GET /api/crons/{id}/preview-runs?count=3 and POST /api/crons/preview-runs both return upcoming runs
- [ ] **CronsTab** — table renders all seeded crons; filter dropdowns reflect Title-Case (Observe/Heartbeat/Both); state badges render (Ok/Failed/Late/Unknown); search debounces 250ms; "+ Add cron" opens modal
- [ ] **CronDetail** — opens via row click → /inventory/crons/{id}; loads cron data; Save changes button fires PATCH and navigates back to list; Archive button opens confirm modal with "Archive cron?" heading; mode-swap radio preserves last-typed schedule + cadence values; key="schedule-input"/key="cadence-input" forces input remount
- [ ] **AddCronModal** — opens via toolbar button; submit POSTs and closes; new row appears in list
- [ ] **ConfirmDeleteModal** — heading "Archive cron?"; button text "Archive"/"Archiving…" when isDeleting; type-cron-name to enable
- [ ] **Mobile (390px)** — Archive modal scrolls with max-h-[calc(100vh-2rem)] overflow-y-auto; CronsTable collapses to mobile layout
