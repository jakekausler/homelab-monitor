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

3. Opt-out test: set `VECTOR_DOCKER_EXCLUDE=<container-name>` in `.env`, restart `monitor` and `vector` containers (vector restart MUST be explicit), confirm new logs from that container stop arriving within 30s.

**Known gotcha:** Vector container must be explicitly restarted after monitor rebuild — `docker compose up -d monitor vector` will NOT restart vector if its image/env hasn't changed.

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
