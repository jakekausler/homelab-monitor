# EPIC-003: Docker collector + per-container probes + label-based discovery + diun-style updates

## Status: Not Started

## Overview

Deliver Docker-aware monitoring. Land cadvisor for container resource metrics, a Docker socket collector for inventory + status + healthcheck signals, a Docker discoverer that emits suggestions for newly-seen containers, label-based per-container probe auto-config (with a per-service file override), image-update detection for both registry-pulled and locally-built images, and a confirm-gated "Pull & Restart" action.

EPIC-003 is the first epic to land per-container monitoring on top of EPIC-001's foundation and EPIC-002's heartbeat receiver. It exercises three subsystems live for the first time: the discoverer plugin contract (STAGE-001-006), the suggestion engine data flow (§4.6), and Docker write actions through the kernel (precursor to EPIC-009's auto-fix).

The drill-down panel ("Docker" tab under Integrations) lands as a SKELETON early in the epic, and each subsequent stage fills its slots as new data becomes available — so every stage has user-visible Refinement sign-off. A final mop-up stage fills any unfilled skeleton sections and completes the in-epic suggestions stub (cross-referenced to EPIC-011 for the global Suggestions screen).

## Source documents (must be read by every session before working in this epic)

- **Spec:** `docs/superpowers/specs/2026-05-04-homelab-monitor-design.md` — relevant sections:
  - §2 Q13 (Docker monitoring decision — labels primary, config-file override, diun-style updates, Pull & Restart, auto-update OFF)
  - §3.4 (Discovered targets — "All Docker containers")
  - §5.5 (Plugin discovery & install)
  - §7.2 (Confirm-on-destructive — Pull & Restart belongs here)
  - §7.4 (Auto-fix isolation — Pull & Restart is NOT a Claude runbook, but the audit/kill-switch/confirm patterns inform the design)
  - §7.5 (Network model — outbound for registry digest checks)
  - §9.2 (Integrations sub-page — Docker drill-down panel)
  - §16 (Cross-cutting requirements — host-network alias rules, profiles handling)
- **Project memory** at `/home/jakekausler/.claude/projects/-storage-programs-homelab-monitor/memory/`:
  - `reference_docker_inventory.md` — full container list (host-network containers, locally-built images, `/rackstation/*` mounts, disabled-profile containers)
  - `reference_homelab_inventory.md` — host topology
  - `project_repo_tooling.md` — monorepo + CRG + workflow
- **CLAUDE.md** at the repo root — verify command, status values, the `git add -A` rule, the `make uv` rule
- **EPIC-001.md** at `epics/EPIC-001-foundation/EPIC-001.md` — cross-stage acceptance criteria are inherited
- **EPIC-002.md** at `epics/EPIC-002-heartbeat-cron/EPIC-002.md` — heartbeat receiver is reused for per-container "synthetic heartbeat" probes (a container expected to call out periodically registers via `/register` like a cron)

## Stages

| Stage | Name | Status |
| --- | --- | --- |
| STAGE-003-001 | cadvisor sidecar + scrape config + first container metrics in VM | Complete |
| STAGE-003-002 | Vector container-log ingestion fix + opt-out `exclude_containers` wiring | Complete |
| STAGE-003-003 | Docker drill-down UI skeleton — Integrations sub-page, routes, empty states, logs-route placeholder | Complete |
| STAGE-003-004 | Docker socket collector — container inventory + status + restart_count + exit_code + healthcheck | Complete |
| STAGE-003-005 | Docker discoverer + suggestions data — periodic + socket-event-driven, writes to `suggestions` table | Complete |
| STAGE-003-006 | Label-based probe auto-config — `homelab-monitor.<kind>.<name>=...` labels create probes | Complete |
| STAGE-003-007 | Per-service config-file override — YAML override under `/config/plugins/docker/` supersedes labels | Complete |
| STAGE-003-008 | Image-update detection (registry digest) — `homelab_image_update_available` metric + vmalert info-severity rule | Complete |
| STAGE-003-009 | Image-update detection (locally-built images) — build-context source hash | Complete |
| STAGE-003-010 | "Pull & Restart" action — confirm-gated compose-aware action + audit + new `compose_actions` table | Complete |
| STAGE-003-011 | Per-container log viewer route — `/integrations/docker/containers/$name/logs` (VL-backed, manual refresh) | Not Started |
| STAGE-003-012 | Drill-down completion + in-epic suggestions stub (cross-ref EPIC-011) | Not Started |

## Current Stage: STAGE-003-011

## Cross-stage acceptance criteria

Inherits all EPIC-001 cross-stage criteria (see `epics/EPIC-001-foundation/EPIC-001.md` §Cross-stage acceptance criteria):

1. `make verify` green (full pipeline; `make test-fast` is for local iteration only).
2. New tests for the stage's behavior; 100% kernel coverage gate maintained.
3. Demoable vertical slice (UI change at the host backend port, API response, or deterministic integration test).
4. No `git add -A` or `git add .` — specific paths only.
5. Changelog entry in `changelog/$(date +%Y-%m-%d).changelog.md`; ADR for any architecture-changing decision.

Plus EPIC-003-specific criteria:

6. **The user's compose file is read-only by every collector and probe.** Only the explicit "Pull & Restart" action (STAGE-003-010) invokes write operations, and only with session-confirm + audit. Compose file lives at `/storage/docker/compose/docker-compose.yml` on the user's host; the path is configurable; the public release does NOT assume this path.
7. **Probe failure does NOT cause container restart.** A failing probe alerts; the user decides if action is needed. The monitor never restarts a container in response to its own probe.
8. **Host-network containers probe via host IP, not container IP.** Per `reference_docker_inventory.md`, the following containers use `network_mode: host`: Home Assistant, Plex, Pi-hole (pihole-unbound), library-organizer, matter-server, music-assistant. Every probe code path MUST handle this — resolve target = host IP when `network_mode: host`, container IP otherwise.
9. **Disabled containers (`profiles: ["disabled"]`) are listed in inventory but NOT probed.** The discoverer surfaces them as informational suggestions ("Frigate exists but is disabled — probe when enabled?"). No probe is created or scheduled for a disabled-profile container.
10. **Multiple probes per container are supported.** Label syntax: `homelab-monitor.<kind>.<name>=<value>` where `<kind>` ∈ `{http, tcp, exec, metrics}` and `<name>` is a user-chosen identifier (defaults to `default`). File-override mirrors this. Examples in STAGE-003-006.
11. **Label collisions are surfaced as suggestions, not silently dropped.** Two labels resolving to the same `(kind, name)` for the same container → suggestion + log + no probe created until resolved.
12. **Every probe runs in a `concurrency_group` keyed by the target.** Per spec §5.4 — all probes hitting the same container share a group `docker.<container_name>` so one service is never DDOSed by parallel HTTP + TCP + exec probes.
13. **Locally-built images use source-hash update-detection, not registry digest.** Containers whose compose entry has `build:` (not `image:`) get build-context source-tree hashing in STAGE-003-009.
14. **`host.docker.internal:host-gateway` aliasing is preserved when resolving probe targets.** Several user containers use this alias; the resolver must not strip it.
15. **Dev rig has a synthetic cadvisor seed CLI** — `hm dev seed-container-metrics` (or equivalent) generates fake `container_*` metric streams so frontend Refinement works without real cadvisor data. Lands in STAGE-003-001 alongside cadvisor.
16. **Integration test rig uses real cadvisor against toy services.** `deploy/compose/docker-compose.test.yml` includes a cadvisor service scraping the test rig's deterministic toy containers. Same code path as prod.
17. **Every stage gets dev rig (3a) AND prod (3b) Refinement sign-off WHERE THE STAGE HAS HOST-INTEGRATION CONCERNS** — desktop + mobile viewport approval on the drill-down UI. Frontend-only stages (e.g., STAGE-003-003, STAGE-003-012) are 3a only. Backend-only stages with no host delta also skip 3b. The drill-down skeleton lands in STAGE-003-003 so every subsequent backend stage has UI to sign off on.

18. **The Docker socket mount widens from :ro to RW at STAGE-003-010.** Stages 001-009 use :ro; STAGE-003-010 widens because Pull & Restart needs the RW path. The security boundary is at the docker socket level either way — anyone with socket access has root-equivalent on the host. This widening is documented + accepted; STAGE-003-010's commit message includes the explicit acknowledgment.

19. **Container stdout/stderr MUST be ingested into VictoriaLogs by default.** STAGE-003-002 fixes the gap from STAGE-001-016 (where `include_containers = []` blocked all containers). Default behavior after STAGE-003-002: vector tails ALL containers on the docker socket; opt-out via `VECTOR_DOCKER_EXCLUDE` env var (CSV list). This is the prerequisite for STAGE-003-011 (log viewer) and EPIC-004 (log anomaly detection).

## Sequential dependency notes

- **STAGE-003-001 (cadvisor sidecar)** is the foundation — adds cadvisor to prod compose, dev compose, and integration test compose. No code in `apps/monitor` apart from the dev seed CLI; deployment + scrape config work.
- **STAGE-003-002 (vector container-log fix)** is a prerequisite for STAGE-003-011 and for EPIC-004. Fixes the gap from STAGE-001-016 where `include_containers = []` blocked all container log ingestion. Render-on-boot substitution wires `VECTOR_DOCKER_EXCLUDE` env var.
- **STAGE-003-003 (UI skeleton)** depends on STAGE-003-001 — the skeleton renders cadvisor data immediately. Pure frontend stage; routes + empty-state components only. ALSO scaffolds the per-container log viewer ROUTE + placeholder component (filled by STAGE-003-011) AND the "Logs" column placeholder in the grid (filled by STAGE-003-011).
- **STAGE-003-004 (socket collector)** is independent of 001/002/003 but lands after them so the drill-down has somewhere to render status data. Fills the "Status" + "Restart Count" + "Healthcheck" columns of the grid.
- **STAGE-003-005 (discoverer)** depends on STAGE-003-004 (reuses the same socket connection abstraction) and writes to `suggestions` table (reusing EPIC-011's pre-existing schema; no UI for suggestions globally yet — just the in-epic stub fills in STAGE-003-012).
- **STAGE-003-006 (label-based config)** depends on STAGE-003-004 (uses inventory) and STAGE-003-005 (the discoverer is the entry point that creates probe-targets when labels are seen).
- **STAGE-003-007 (file override)** depends on STAGE-003-006 — extends the same probe-config resolver with a higher-precedence source.
- **STAGE-003-008 (registry digest)** is independent — runs as its own collector. Lands after the drill-down has settled because the "image update" badge column needs the grid in place.
- **STAGE-003-009 (build-source hash)** depends on STAGE-003-008 (shares the `homelab_image_update_available` metric family + vmalert rule + UI badge).
- **STAGE-003-010 (Pull & Restart)** depends on STAGE-003-008 + STAGE-003-009 (the action is "pull because update is available" — useless without update detection). Adds confirm-gated UI button + new `compose_actions` audit table.
- **STAGE-003-011 (per-container log viewer)** depends on STAGE-003-002 (container logs in VL) + STAGE-003-003 (route + Logs column placeholder) + STAGE-003-004 (inventory tells us what containers exist). Reuses `VictoriaLogsClient` from STAGE-002-013. EPIC-004 adds anomaly / pattern / live-tail layers on top.
- **STAGE-003-012 (mop-up)** is last — fills any unfilled drill-down skeleton sections + completes the in-epic suggestions stub. Explicit forward reference to EPIC-011 for replacement.

**Strict serial order: 001 → 002 → 003 → 004 → 005 → 006 → 007 → 008 → 009 → 010 → 011 → 012.**

## Notes

- **The compose file path is configurable.** The public release does NOT hard-code `/storage/docker/compose/docker-compose.yml`. Config key `docker.compose_file_path` defaults to `/storage/docker/compose/docker-compose.yml` for this user's deployment via the host-overrides repo; public default is unset → "Pull & Restart" disabled until configured.
- **`host.docker.internal:host-gateway` is used by several user containers** — probes that resolve container hostnames must handle this. Documented in STAGE-003-005 + the resolver tests.
- **The default probe set per discovered container is conservative.** A discovered container with no `homelab-monitor.*` labels gets an HTTP probe ONLY if exposed ports + a healthcheck combine to suggest it; otherwise it is recorded in inventory and shown in the grid, but no probe is auto-created. `exec` probes are NEVER auto-created — they require explicit user opt-in for safety (an exec probe runs a command inside the container; high blast radius).
- **Disabled containers (`profiles: ["disabled"]`) are listed but not probed.** The discoverer surfaces them as informational suggestions ("Frigate exists but is disabled — probe when enabled?"). When the user later enables the profile, the discoverer re-surfaces the suggestion.
- **The Docker socket mount widens from read-only to read-write at STAGE-003-010.** Stages 001-009 use :ro; STAGE-003-010 widens to RW so `docker compose pull && up -d <svc>` works (Pull & Restart action). The security boundary is at the docker socket level either way — anyone with socket access has root-equivalent on the host. Documented in the STAGE-003-010 design.
- **Host-native MariaDB and MySQL are out of scope here** — they are covered in EPIC-018 service deep-dives.
- **`/rackstation/*` mount-health is upstream of Plex, Frigate, podcast-feed** — but mount monitoring belongs in EPIC-001 / EPIC-008 (Synology). This epic just lists them as containers; mount probes are not added here.

## Cross-epic carry-forward → EPIC-011

The in-epic suggestions UI stub (STAGE-003-002 skeleton + STAGE-003-010 completion) is a TEMPORARY in-epic surface. When EPIC-011 (Discovery & Suggestion engine UX) is built, the global Suggestions inbox MUST:

- Render Docker discoverer suggestions alongside all other discoverer types in a single unified inbox.
- Subsume the per-epic stub — the Docker drill-down's "Pending suggestions" panel can either link to the global inbox OR be removed entirely (to be decided in EPIC-011 Design).
- Honor the existing `suggestions` table schema written by STAGE-003-004 — no schema rewrite required for EPIC-011 to take over rendering.

This carry-forward MUST be added as an explicit EPIC-011 acceptance criterion when EPIC-011 is opened.

## Cross-epic carry-forward → EPIC-009

The "Pull & Restart" action (STAGE-003-010) introduces the FIRST kernel-driven Docker write operation in the project. EPIC-009 (auto-fix subsystem) will build the broader runbook orchestrator that may invoke `docker compose` (or `docker exec`) on a broader allow-list. When EPIC-009 is built:

- The `compose_actions` audit table introduced in STAGE-003-010 SHOULD be either subsumed into `runbook_runs` OR kept separate and cross-referenced — to be decided in EPIC-009 Design.
- The confirm-on-destructive UX from STAGE-003-010 SHOULD be the same component used for runbook real-runs.
- The docker socket RW widening from STAGE-003-010 is a prerequisite for any EPIC-009 runbook that needs to invoke `docker` commands.
