# Regression Checklist - EPIC-003: Docker

(Items added per stage during Refinement.)

## STAGE-003-001 — cadvisor sidecar + dev seed CLI (2026-05-21)

- **R-003-001-1 — cadvisor dev rig**: `make dev` brings cadvisor up on host port 18081; `curl http://127.0.0.1:18428/api/v1/query?query=count(container_cpu_usage_seconds_total)` returns non-zero count within 30s of bringup.
- **R-003-001-2 — cadvisor prod rig**: `make dev-prod` brings up homelab-monitor on 29090 + cadvisor container-internal; vmagent's cadvisor target shows `health=up`.
- **R-003-001-3 — seed CLI happy path**: `hm dev seed-container-metrics --containers 5 --vm-url http://127.0.0.1:18428` exits 0 and produces 5 synthetic series in VM (labeled `homelab_synthetic="true"`).
- **R-003-001-4 — seed CLI clear**: `hm dev seed-container-metrics --clear` removes synthetic series (assert count goes to 0) without affecting cadvisor real series.
- **R-003-001-5 — hostname gate**: `HM_HOST_HOSTNAME=fake-host hm dev seed-container-metrics --containers 1` exits 1 with "hostname mismatch" stderr message; `--force` overrides.
- **R-003-001-6 — prod env loading**: `make dev-prod` resolves `HOMELAB_MONITOR_PORT=29090` and `HOMELAB_MONITOR_BIND_HOST=0.0.0.0` from `deploy/compose/.env` (not `deploy/dev/dev.env`). Monitor binds `0.0.0.0:29090->9090/tcp`.

## STAGE-003-002: Vector container-log ingestion + VECTOR_DOCKER_EXCLUDE

**Sanity check (run after any change touching vector.toml.template or kernel/cron/render.py):**

1. With prod stack running, query VictoriaLogs for distinct service values:
   ```
   docker compose -f deploy/compose/docker-compose.yml --env-file deploy/compose/.env exec -T victorialogs wget -qO- 'http://127.0.0.1:9428/select/logsql/field_values?query=*&field=service&limit=100'
   ```
   Expect: container names appear (e.g. `homelab-monitor`, plus others depending on user's docker ecosystem). NOT just journald sources.

2. Confirm the rendered vector config has `exclude_containers = ${VECTOR_DOCKER_EXCLUDE_VALUE}` (substituted) and does NOT have an `include_containers` line:
   ```
   docker compose exec vector cat /etc/vector/vector.toml | grep -A 2 docker_logs
   ```

3. Opt-out test: set `VECTOR_DOCKER_EXCLUDE=<container-name>` in `.env`, restart the `monitor` container to re-render the config; vector auto-reloads it within ~30s (it runs with `--watch-config` as of STAGE-007-016A — explicit `docker compose restart vector` is now only a fallback). Confirm new logs from that container stop arriving within ~60s.

**Known gotcha (updated by STAGE-007-016A):** vector now auto-reloads via `--watch-config`, so it no longer needs an explicit restart after the monitor re-renders. (Historically — before 016A — `docker compose up -d monitor vector` would NOT restart vector if its image/env was unchanged, so a manual `docker compose restart vector` was required; that explicit restart is now only a fallback if the watch ever fails to fire.)

## STAGE-003-003: Docker drill-down UI skeleton

**Sanity check (run after any change touching `apps/ui/src/routes/integrations/`, `apps/ui/src/router.tsx`, or `apps/ui/src/components/SidebarNav.tsx`):**

1. With dev rig running (`make dev`), open the UI in a browser. Sidebar should show "Integrations" as a non-clickable section label with "Docker" as an indented clickable item below.

2. Navigate to `/integrations/docker`. Verify:
   - Heading "Docker integration" visible
   - Table with 10 column headers (Name, Status, Image, CPU, RAM, Image Update, Healthcheck, Probes, Logs, Actions) always visible, even when no containers exist
   - Empty state "No containers discovered yet." appears as a single centered row inside the table body (NOT replacing the table)
   - "Pending suggestions" and "Recent actions" panels each show their empty state
   - No browser console errors

3. Navigate to `/integrations/docker/containers/some-name/logs`. Verify:
   - Renders `ContainerLogsPlaceholder` (NOT the parent Docker page)
   - Shows "Log viewer for `some-name` not yet implemented."
   - Back-link to `/integrations/docker` works

4. Resize browser below `md` breakpoint (768px). Verify:
   - Desktop table hidden (`hidden md:block`)
   - Mobile card-based empty state visible (`md:hidden`)
   - Panels still render

**Known gotchas:**
- The log-viewer placeholder route is a flat sibling under `protectedLayoutRoute`, NOT nested under `dockerIntegrationRoute`. If you nest it, you must add `<Outlet />` to `DockerIntegrationPage`, which would also show the parent grid alongside the log viewer.
- The empty-state row uses `colSpan={10}` — if columns are added/removed, update this.
- Mobile cards do NOT show field labels (intentional per Design phase T-VIEWPORT-SWAP).

## STAGE-003-004: Docker socket collector + container inventory API

**Sanity check (run after any change touching `kernel/docker/`, `kernel/metrics/docker_socket_collector.py`, `kernel/api/routers/docker.py`, or `deploy/compose/docker-compose.yml`):**

1. With prod stack running, login + query the docker endpoint:
   ```
   curl -c /tmp/cookies.txt -X POST 'http://127.0.0.1:29090/api/auth/login' \
     -H 'Content-Type: application/json' \
     -d '{"username":"admin","password":"<admin-password>"}'
   curl -b /tmp/cookies.txt 'http://127.0.0.1:29090/api/integrations/docker/containers' | python3 -m json.tool | head -50
   ```
   Expect: `{"containers": [...]}` with at least one container per running host docker container.

2. Confirm collector self-metric in VM:
   ```
   docker compose exec -T victoriametrics wget -qO- 'http://127.0.0.1:8428/api/v1/query?query=homelab_collector_run_success_total%7Bname%3D%22docker_socket%22%7D'
   ```
   Expect: value > 0.

3. Confirm the monitor container has supplemental docker group:
   ```
   docker compose exec -T monitor id
   ```
   Expect: `groups=995,<DOCKER_GID>` where DOCKER_GID matches `HM_HOST_DOCKER_GID` in `.env`.

**Known host-portability gotcha:**
- The docker socket on the HOST is owned `root:<docker GID>`. The monitor container needs supplemental access via `group_add: ["${HM_HOST_DOCKER_GID:-999}"]`. Each homelab host MUST set `HM_HOST_DOCKER_GID` in `deploy/compose/.env` to that host's actual docker group GID. Find with: `getent group docker | cut -d: -f3`.
- This is why a fresh `docker compose up` may fail with EACCES on a different host: GID 999 is a common default but not universal.

## STAGE-003-005 — Docker discoverer + suggestions data + compose visibility

- [ ] `docker run --rm --name regression-test hello-world` produces a suggestion within 5s; container exit transitions suggestion to `state='container_gone'`.
- [ ] Container grid shows new "Compose" column as first column with directory basename of compose_file_path; dash for non-compose containers; full path on title tooltip.
- [ ] Container grid "Restarts (24h)" column shows reset-aware 24-hour restart delta from VictoriaMetrics, NOT cumulative; cumulative count shown on title tooltip.
- [ ] `docker compose up -d --force-recreate <service>` does NOT create a new row in the targets grid — the existing row's container_id is updated and previous_container_id + recreated_at are populated.
- [ ] `status='missing'` containers are hidden from grid by default; "Show missing containers (N)" toggle reveals them.
- [ ] Mobile (ContainerGridCard) renders Compose, Status badge, Restarts (24h), and Healthcheck badge — parity with desktop.
- [ ] Pending Suggestions panel renders only `homelab-monitor.*` labels as badges (not Compose/OCI vendor labels).
- [ ] PendingSuggestionsPanel SuggestionCard shows compose_file_path below image_ref when present, with truncate + title tooltip.

## STAGE-003-007 — Per-service config-file override

- **R-007-1: file_override probe upsert** — Place a valid YAML file at `<HOMELAB_MONITOR_DOCKER_OVERRIDES_DIR>/<container>.yaml` defining 2 probes. Within 30s, verify `probe_targets` has 2 rows for that container with `config_source='file_override'`. Verify the per-container drill-down shows both probes with Source = `file_override`.
- **R-007-2: red "Config error" row badge** — Place a malformed YAML (e.g., `kind: ssh`) at `<dir>/<container>.yaml`. Within 30s, verify `/api/integrations/docker/probes/summary` returns `config_errors` (non-null list) on that container's entry. Verify the Docker grid shows the red "Config error" badge in the probes column on the affected row.
- **R-007-3: orphan malformed file → suggestion** — Place a malformed YAML where the container does NOT exist. Within 30s, verify a `docker_file_override_malformed` suggestion appears in the `suggestions` table (deduplication_key=`malformed::<container>`).
- **R-007-4: file deletion releases ownership** — Delete an existing override file. Within 30s, verify `docker_override_ownership` no longer contains that container; verify `probe_targets` rows for that container with `config_source='file_override'` are soft-deleted (`hidden_at IS NOT NULL`); verify label-derived probes (if any) re-appear on the next discoverer tick.
- **R-007-5: exec probe dual gate** — Place an override with `kind: exec` but WITHOUT container-level `exec_authorized: true`. Verify the exec probe is dropped and a `docker_file_override_malformed` suggestion is emitted with reason mentioning `exec_not_authorized`. Repeat with `exec_authorized: true` and env `HOMELAB_MONITOR_DOCKER_PROBES_EXEC_ENABLED=false`; verify the probe is still dropped (env gate also required).
- **R-007-6: per-tick log emission** — Tail backend log; verify `override_loader.refresh_complete owned=N errors=M suggestions_emitted=K` appears at least once per 30s window (Lesson 14).
- **R-007-7: container_name canonicalization** — Place an override file using the literal container name (no `/` prefix, no `<12-hex>_` recreate prefix). Verify it matches the running container even immediately after a `docker compose --force-recreate`.
- **R-007-8: openapi cascade** — When backend `/probes/summary` response schema changes in the future, the UI types in `apps/ui/src/api/schema.ts` MUST be regenerated via `make openapi-export` + `pnpm --filter ui run generate-types`. UI code that consumes `entry.config_errors` / `entry.source_breakdown` will silently break if types aren't regenerated.

## STAGE-003-006: Label-based probe auto-config

Re-verify after any change to:
- `apps/monitor/homelab_monitor/kernel/metrics/probe_supervisor.py`
- `apps/monitor/homelab_monitor/kernel/docker/probe_resolver.py`
- `apps/monitor/homelab_monitor/kernel/docker/probe_executor.py`
- `apps/monitor/homelab_monitor/kernel/docker/label_parser.py`
- `apps/monitor/homelab_monitor/plugins/discoverers/docker_discoverer.py` (probe-upsert hook)
- `apps/monitor/homelab_monitor/kernel/api/routers/docker.py` (probe endpoints)
- `apps/ui/src/routes/integrations/ProbeListPanel.tsx`, `ContainerProbesPage.tsx`, `ProbesBadge.tsx`
- `apps/ui/src/api/docker.ts` (useListProbes, useToggleProbe)
- `apps/ui/src/lib/relativeTime.ts`, `apps/ui/src/lib/useNowTick.ts`

### Backend regression items

1. `make verify` passes with 100% kernel coverage.
2. Add a `homelab-monitor.http.health: http://container:<port>/<path>` label to any compose-managed container; force-recreate; within ~60s a probe_target row appears with `last_status='ok'` for the canonical container_name.
3. Verify NO duplicate probe_target row appears under a `<12-hex-chars>_<name>` prefix (Docker rename pattern).
4. Add a malformed label like `homelab-monitor.http.bad: "not-a-url"`; force-recreate; verify a `docker_label_malformed` suggestion row appears in the suggestions table.
5. Add two colliding labels (e.g. `homelab-monitor.http=http://a/` AND `homelab-monitor.http.default=http://b/`); force-recreate; verify a `docker_label_collision` suggestion row appears AND no probe_target row is created.
6. POST /api/integrations/docker/probes/{id}/disable; verify the row's `enabled` becomes 0 AND an `audit_log` row with `what='docker.probe.disable'` is written.
7. Inspect `deploy/dev/logs/backend.log` for periodic `probe_supervisor.reconcile_complete` entries; supervisor must NOT be silent.

### UI regression items

8. Navigate to `/integrations/docker`. For a container with active probes, the Probes column/badge shows "N active" (and "N active, M failing" if any fail). For containers with no probes, badge shows "—".
9. Click the badge → URL becomes `/integrations/docker/containers/{name}/probes`. Heading shows "Probes for {name}". Back link returns to grid.
10. For a container with a successful probe, ProbeListPanel renders one row with kind/name/target/status=ok/last_error=empty/Disable toggle. Mobile viewport renders the same data as cards.
11. The "Last run" cell counts up second-by-second (e.g. "5s ago" → "6s ago" → "7s ago") between server polls, then resets to a small value every ~30s when the supervisor's next probe execution writes a new last_run_at.
12. Click Disable → toggle visually flips, probe DB row's `enabled` flag flips, supervisor stops executing the probe (probe stays in DB but no new last_run_at updates).
13. Click Enable → toggle flips back, supervisor resumes executing the probe.
14. The dual-unit relative-time format renders correctly across all 9 existing call sites (CronDetail, CronRunsList, RecentRunsPanel): `Xs ago` / `Xm Ys ago` / `Xh Ym ago` / `Xd Yh ago` / `Xd ago`.

## STAGE-003-008 — Image-update detection (registry digest)

- **R-008-1: campaign_redis update detection** — Run the dev rig with `HOMELAB_MONITOR_IMAGE_UPDATE_INTERVAL_SECONDS=60`. Within 70s, verify `sqlite3 /tmp/hm-dev/homelab.db "SELECT update_available, check_error_reason FROM image_update_state WHERE container_name='campaign_redis';"` shows `1|` (update available, no error). Drill-down at `/integrations/docker/containers/campaign_redis/image-update` shows distinct local + registry digests with the blue "yes" indicator.
- **R-008-2: Docker Hub API host routing** — Verify `RegistryDigestClient.fetch_latest_digest` routes `docker.io` to `https://registry-1.docker.io/v2/...` (NOT `https://docker.io/v2/...` which redirects to www.docker.com and 403s). Regression test: any docker.io image should produce a populated `last_registry_digest` (not a `network_error` row).
- **R-008-3: campaign_prometheus pinned-tag detection** — `prom/prometheus:v2.47.0` (pinned semver) should report `update_available=0` even though the calendar date of the local image is many months old — digests for pinned semver tags don't change. Validates the digest-comparison-not-tag-comparison invariant.
- **R-008-4: blue pill badge in container grid** — Container grid at `/integrations/docker` shows blue "Update available" pill on rows for `campaign_redis`, `gm-redis`, `gm-postgres`, `foundry` (or whatever containers currently have moved digests). Cards on mobile show the same badge. Click-through navigates to `/integrations/docker/containers/$name/image-update`.
- **R-008-5: SHA truncation via formatDigest helper** — `apps/ui/src/lib/digest.ts` `formatDigest()` truncates `sha256:<64hex>` to `sha256:<first 12 hex>…`. Applied in `ContainerImageUpdatePage` (last_local_digest, last_registry_digest), `ContainerGridRow` (image), `ContainerGridCard` (image). Non-digest strings (like `nginx:1.27`) pass through unchanged.
- **R-008-6: local-build images skipped cleanly** — Containers with `image: <none>` or `sha256:...` bare digest pins (no tag) should be skipped (no image_update_state row + log line `image_update_collector.skip_unparseable`). zigbee2mqtt is a real example on this host.
- **R-008-7: per-tick log liveness** — Backend log shows `image_update_collector.*` events on every tick (every 60s in dev / 6h in prod). The collector emits self-metric `homelab_collector_run_image_update_checker{phase="tick", result="ok"}`.
- **R-008-8: image-events second background task** — On `docker pull <image>` events from the docker socket, the standalone `_image_events_loop` task in lifespan calls `scheduler.request_immediate_run("image_update_checker", ...)`. Verify a manual `docker pull alpine:latest` triggers an immediate tick of the collector.
- **R-008-9: rate-limit hard-cap + banner** — If `homelab_registry_rate_limit_remaining{registry="docker.io"}` drops below 10, the collector skips that registry's checks for the tick + emits `homelab_image_update_check_skipped{reason="rate_limit"}` + `/image-updates/summary` returns `rate_limit_skipped_count > 0` + UI shows yellow `<RateLimitBanner />` above the container grid. (Hard to trigger in normal testing; could verify by setting `HOMELAB_MONITOR_IMAGE_UPDATE_HARD_CAP_REMAINING` if exposed as env.)
- **R-008-10: OpenAPI cascade** — When backend `/image-updates/summary` or `/image-update` response shapes change in future stages, MUST regenerate `packages/shared-types/openapi.json` + `apps/ui/src/api/schema.ts`. UI hooks (`useImageUpdatesSummary`, `useImageUpdate`) consume the generated types directly.

## STAGE-003-009 — Image-update detection (locally-built images)

- **R-009-1: Baseline persistence across ticks** — Touch a file under any locally-built container's build context (e.g., `echo "# touch" >> /storage/programs/grocy-homeassistant/main.py`). Within one collector tick (60s dev / 30 min prod), the drill-down must show `update_available=true` AND `baseline_source_hash != last_source_hash`. Confirm `available` STAYS `true` across 3+ consecutive ticks (regression guard for the original `available=false` after one tick bug).
- **R-009-2: Image rebuild resets baseline** — Rebuild any locally-built container's image with `--no-cache`. Next tick must report `update_available=false` AND `baseline_source_hash == last_source_hash` AND `baseline_image_id == <new image_id>`. Validates the image-id-tracked baseline reset.
- **R-009-3: build-sources.yaml path remap** — Operator's actual compose at `/storage/docker/compose/docker-compose.yml` declares build contexts at absolute paths like `/storage/programs/bills`. With `/config/docker/build-sources.yaml` declaring `host_prefix: /storage/programs` → `container_prefix: /host-build-contexts/programs` AND `${HOMELAB_MONITOR_BUILD_CONTEXTS_DIR}:/host-build-contexts:ro` mount, the collector must produce a real sha256 hash for `bills` (not `context_missing`). Validates D-BUILD-SOURCES-YAML-CONFIG + D-PATH-REMAP-EXPLICIT end-to-end.
- **R-009-4: build-sources.yaml hot-reload** — Edit `/config/docker/build-sources.yaml` to add a new compose file. Within 30s, the `homelab_build_sources_config_loaded` metric reflects the new state; the next collector tick uses the new config without restart.
- **R-009-5: env-var fallback when no YAML** — Without `build-sources.yaml` present, the collector falls back to single-compose mode via `HOMELAB_MONITOR_COMPOSE_DIR`. Public release default unchanged.
- **R-009-6: oversized context sentinel** — A build context exceeding `MAX_TOTAL_BYTES` or with any file exceeding `MAX_FILE_BYTES` produces `last_source_hash="OVERSIZED:context_too_large"` + `update_available=true` + counter `homelab_build_source_hash_skipped_total{reason="context_too_large"}` increments. Sentinel is deterministically-different from any sha256, drives the badge to "needs operator attention".
- **R-009-7: limit overrides** — Bumping `HOMELAB_MONITOR_BUILD_HASH_MAX_FILE_BYTES` and/or `_MAX_TOTAL_BYTES` in `deploy/compose/.env` resolves the oversized signal on next tick. This user's deployment uses 64 MB per-file + 4 GB total to accommodate `kingdom-rules/src/assets/tiles/square.png` (46 MB) and `library-organizer` (2.1 GB Go src + assets).
- **R-009-8: permission_denied surfaces correctly** — Build context containing a file unreadable by the monitor container's UID (995 in prod) produces `last_source_hash="OVERSIZED:permission_denied"` + `check_error_reason="permission_denied"`. Operator can resolve via `.dockerignore` or chmod.
- **R-009-9: Blue pill + drill-down render** — Grid (desktop + mobile) shows blue "Source changed — rebuild needed" pill for `source="local_build"` + `available=true`. Click-through drill-down at `/integrations/docker/containers/$name/image-update` shows compose service, build context path (remapped), Current source hash, Baseline source hash (when `available=true` AND baseline≠current).
- **R-009-10: Startup-tick warm-up** — Fresh `docker compose up -d monitor` populates the `/api/integrations/docker/image-updates/summary` endpoint with both registry AND local-build entries within ~30s, NOT after the 6h/30min normal interval. Validates lifespan-startup `await scheduler.await_immediate_run(...)` for both `image_update_checker` and `local_build_update_checker`.
- **R-009-11: docker_build_hashes table schema** — Migration 0027 creates `docker_build_hashes` with columns: container_name PK, compose_service, build_context_path, last_source_hash, last_checked_at, check_failed_at, check_error_reason, update_available, baseline_source_hash, baseline_image_id. CHECK constraint on `check_error_reason` allows IN (`compose_unreadable`, `context_missing`, `context_too_large`, `permission_denied`, `unknown`).
- **R-009-12: `_signal_awaitable_done` covers all _tick exit paths** — `Scheduler.await_immediate_run` must NOT hang on quarantine/group-lock-timeout/cancelled/timeout/exception early returns in `_tick`. Regression test: `make verify` completes the full pytest suite without hanging (was hanging 5+ min at ~88% before the helper was added).
- **R-009-13: OpenAPI cascade** — Future changes to `ImageUpdateDetail` or `ImageUpdateSummaryEntry` Pydantic models MUST regenerate `packages/shared-types/openapi.json` + `apps/ui/src/api/schema.ts`. The summary endpoint unions registry + local-build entries; the per-container endpoint prefers local-build over registry when both exist.

## STAGE-003-011 — Per-container log viewer

- [ ] `GET /api/integrations/docker/containers/{name}/logs` returns 200 with `log_status="available"` for a container with active logs (try `homelab-monitor` or `homelab-vector`).
- [ ] Same endpoint returns 404 with `log_status="container_unknown"` for a non-existent container name (e.g., `/api/integrations/docker/containers/does-not-exist/logs`).
- [ ] Same endpoint returns 503 with `log_status="vl_unavailable"` when VictoriaLogs is unreachable (simulate by stopping the VL container briefly).
- [ ] Returns `log_status="no_lines"` (200) when container exists in `targets` table but VL has no lines in the queried window.
- [ ] Query params clamped: `?limit=10000` → silently clamped to 500; `?since=8d` → silently clamped to 7d.
- [ ] Query params validated: `?since=abc` or `?since=5x` → 422.
- [ ] Frontend route `/integrations/docker/containers/$name/logs` renders the viewer (sticky header + log body + since picker + refresh button).
- [ ] Since picker presets (5m, 15m, 1h, 6h, 24h, 7d) each trigger a fresh query.
- [ ] Truncation banner ("Showing first 500 lines. Narrow the time window to see all entries.") appears when `truncated=true`.
- [ ] All four `log_status` UI states render appropriately (available with lines, no_lines empty state, container_unknown 404 page, vl_unavailable amber banner).
- [ ] "View logs →" link in the desktop container grid (ContainerGridRow) navigates to the correct route.
- [ ] "View logs →" link in the mobile container card (ContainerGridCard) navigates to the correct route.
- [ ] Mobile viewport renders the viewer correctly (header stacks, log body scrolls).
- [ ] Pagination & custom datetime range are explicitly deferred to EPIC-004 STAGE-004-005 — confirm code comments `// TODO: EPIC-004 STAGE-004-005 — cursor-based pagination + custom datetime range picker` exist in both `DockerContainerLogsViewer.tsx` and `CronRunLogViewer.tsx`.

## STAGE-003-010 — Pull & Restart action (added 2026-05-26)

- [ ] **Remote-image pull**: With a remote-image container showing "Update available", click Pull & Restart → modal shows current/latest digest → type "pull" → submit. Verify: button vanishes immediately → "Pulling…" appears → polling shows transition to "Restarting…" → terminal success → toast "Pull & Restart succeeded for {container}" → badge clears within ~2s → container recreated with new image.
- [ ] **Locally-built rebuild**: With a locally-built container whose build context source-hash changed (badge shows "Source changed — rebuild needed"), click Rebuild & Restart → modal title says "Rebuild & Restart" + body mentions building → type "pull" (still the universal phrase) → submit. Verify: "Building…" appears → "Restarting…" → terminal success → toast "Rebuild & Restart succeeded" → badge clears within ~2s → baseline_source_hash reset in docker_build_hashes.
- [ ] **Concurrency guard**: While an action is in flight, click Pull & Restart on the SAME container again → modal opens → submit → expect HTTP 409 toast "Pull & Restart already in progress for {container}". No second compose_actions row created.
- [ ] **Confirm phrase rejection**: Type anything except "pull" → submit button stays disabled OR API returns 400 with toast "Confirm phrase must be 'pull'".
- [ ] **Auth gate**: API tokens without DOCKER_WRITE scope cannot trigger Pull & Restart (403). Tokens WITH scope can.
- [ ] **Audit row written**: Every attempt (success/failed/timeout) writes a row to compose_actions table AND audit_log table. Verify via DB inspect.
- [ ] **Recent Actions panel** on drill-down shows all recent attempts for the container with state badge, duration, stdout/stderr expand toggle, who/client_ip.
- [ ] **Timeout enforcement**: Force a slow command (e.g., set HOMELAB_MONITOR_COMPOSE_ACTION_TIMEOUT_SECONDS=5 + pull a multi-GB image) → action terminates with state="timeout" + SIGTERM/SIGKILL semantics. Toast "Pull & Restart timed out".
- [ ] **Host setup idempotent**: `sudo bash scripts/host-setup.sh` is safe to re-run; reports "PRESENT" for existing group/file states.
- [ ] **Override generation**: `make generate-build-mounts` re-reads build-sources.yaml and rewrites docker-compose.override.yml deterministically. Operator MUST pass `-f deploy/compose/docker-compose.override.yml` to docker compose commands (auto-load is disabled when explicit `-f` is used for the main file).
- [ ] **Mobile viewport**: Modal + Recent Actions panel render correctly at 375x667; all interactions tappable.

## STAGE-003-013 (Container drill-down unification + integration-page slim-down)

- [ ] Visit `/integrations/docker` — containers grouped by compose file (header per group with Boxes icon + count), "Ungrouped" group for standalone containers, rows sorted alphabetically within groups.
- [ ] Each row shows: name, image subtitle, status/image-update/probes/healthcheck badges (with leading lucide icons), restart chip (only when > 0), CPU%, RAM MiB, chevron. Entire row is a single Link to the container's overview tab.
- [ ] ImageUpdateBadge renders as a colored Badge (not a Link): green "Up to date", amber "Update available" / "Rebuild needed", red "Update Check Failed". Renders nothing on loading / no-data / error states.
- [ ] ProbesBadge renders as a colored Badge (not a Link): red "Config errors", red "{N} failing", green "{N} active". Renders nothing on loading / 0 active / no-data.
- [ ] On mobile (< sm), ContainerListRow stacks vertically: name → image → badges → metrics. On desktop (≥ sm), name+image on left, badges+metrics on right.
- [ ] Click any row → navigates to `/integrations/docker/containers/<name>/overview` (default tab).
- [ ] Container drill-down page header is NOT sticky — scrolls naturally with content.
- [ ] Tab strip: Overview → Probes → Logs → Recent Actions (in that locked order). Horizontally scrollable on mobile.
- [ ] Overview tab: image update block (with Pull & Restart / Rebuild & Restart button when update_available), container metadata block, restart/exit history block, CPU/RAM with 15-min sparklines (via useMetricsRange).
- [ ] Probes / Logs / Recent Actions tabs render their respective content.
- [ ] Pull & Restart flow: button opens PullRestartModal → submits → polls action state.

## STAGE-003-012 — Probes section + per-container probe management

### Backend
- [ ] `POST /api/integrations/docker/probe-targets` creates a new probe_target row (config_source="manual"). 200 OK with the created row. 422 on invalid kind/name/intervals. 404 if container_name doesn't exist in targets table. 401 without session. Requires X-CSRF-Token.
- [ ] `PATCH /api/integrations/docker/probe-targets/{probe_id}` updates target_value / interval_seconds / timeout_seconds (kind + name + container_name are immutable). 200 with updated row. 404 if probe_id doesn't exist. 422 on invalid intervals. 401 without session.
- [ ] `DELETE /api/integrations/docker/probe-targets/{probe_id}` physically deletes the row (NOT a soft hide). 204 No Content. 404 if probe_id doesn't exist. 401 without session.
- [ ] All three endpoints idempotent at REST level where applicable: POST duplicate (container,kind,name) → upserts existing row.
- [ ] `GET /api/integrations/docker/suggestions/{id}/default-probes` returns the probes Accept would create (probes=[] + reason=docker_unavailable | container_gone | no_ports_no_healthcheck | available). 200 always. 404 only if suggestion_id doesn't exist.
- [ ] Audit log entries created on POST/PATCH/DELETE (search `docker.probe_target.*` in audit_log table).
- [ ] Backend kernel coverage 100%.

### Frontend — Probes section
- [ ] "Probes" section header at `/integrations/docker` (was "Pending Suggestions" before STAGE-003-012 Refinement scope expansion).
- [ ] One ContainerProbesCard per container in the grid above (1:1). No "show missing" filter.
- [ ] Each card has header (name + image + status badge), Active probes list, Suggested probes list (if pending suggestion exists), "Add new probe" button.
- [ ] Active probes list shows kind + name + target + interval/timeout + Edit + Delete buttons.
- [ ] Click Delete → row disappears IMMEDIATELY (optimistic update), toast confirms.
- [ ] Click Edit → opens EditProbeModal with kind/name read-only, target/intervals editable.
- [ ] Submit Edit → row updates IMMEDIATELY, toast confirms.
- [ ] Suggested probes list (only if pending suggestion) shows kind + name + target + Add + Ignore buttons.
- [ ] Click Add on suggested → row moves IMMEDIATELY from Suggested to Active, toast confirms.
- [ ] Click Ignore on suggested → row disappears IMMEDIATELY from Suggested (no network call, client-side only). Refreshing page → ignored items come back (persistent ignore deferred to EPIC-011).
- [ ] "Add new probe" button opens AddProbeModal (single-row form, kind=http default).
- [ ] Submit Add → row appears IMMEDIATELY in Active, toast confirms.
- [ ] Probes shown on the card match probes shown on the per-container drill-down at `/integrations/docker/containers/$name/probes` (both surfaces use the same `useListProbes` hook + cache key).
- [ ] AddProbeModal + EditProbeModal full-screen sheet on mobile (< sm); centered modal on desktop.
- [ ] Optimistic updates rollback correctly on server error (test by simulating 500 from POST).

### Cross-epic carry-forward to EPIC-011
- [ ] PendingSuggestionsPanel removal vs link decision (deferred to EPIC-011 Design): the in-epic Probes panel is now in place. EPIC-011 will decide whether to keep it AND a global inbox, or remove it.
- [ ] Endpoint path migration (`/api/integrations/docker/suggestions/*` → possibly `/api/suggestions/*`): the existing `/suggestions/{id}/accept|customize|ignore` endpoints remain on the backend but are no longer called by UI. EPIC-011 will decide whether to deprecate or generalize.
- [ ] Per-default-probe ignore persistence: currently client-side React state only. EPIC-011 will decide whether to add a `dismissed_suggestion_probes` table for cross-session persistence.

### Operational gotcha (CLAUDE.md candidate)
- [ ] After ANY change to `apps/ui/src/*`, run `pnpm --filter ui run build` BEFORE `docker compose build` to refresh the `apps/ui/dist/` folder. The Dockerfile copies the pre-built dist directly (line 72: `COPY apps/ui/dist /app/ui`). Skipping the UI build ships a stale bundle.
