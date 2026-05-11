# EPIC-002: Heartbeat receiver + cron registry + cron auto-discovery

## Status: Not Started

## Overview

Deliver the cron-monitoring half of the system: a Healthchecks.io-style heartbeat receiver, a cron registry that tracks every cron the user has, automatic discovery of crons from `/etc/cron.*` + user crontabs, vector log-scrape integration to track unmodified cron exit codes, and helpers that bootstrap heartbeat pings into existing cron jobs without modifying them silently. After this epic ships, the dashboard exposes a Crons tab in Inventory listing every known cron with last-seen state, expected cadence, lateness, and recent runs; vmalert fires when a cron is late or fails; and the user can opt-in any cron to active heartbeat with a one-click wrapper install.

Cron monitoring is hybrid by design (per spec §2 / Q5):

- **B (default)**: log-scrape — observe via journald/syslog that the cron ran and what its exit code was. Works on any cron without modification.
- **A (gold standard)**: active heartbeat — the cron is wrapped/modified to call `curl http://homelab-monitor/hb/<id>/start` and `…/ok|/fail`. Most reliable, requires touching the cron.
- **C (bootstrap helper)**: discovery + wrapper — the monitor reads the user's crontabs, auto-generates a wrapper script per job, and offers it as a one-click install via the dashboard.

Public release defaults to A-mode-by-opt-in and B-mode-by-default — discovery records crons in `observe` mode (no modifications, no scraping yet), and the user explicitly flips a cron to `heartbeat` or `both` per row. The host-overrides repo for this user's deployment will be more aggressive about applying B/C selectively.

After EPIC-002 is complete, EPIC-003+ can rely on: an authoritative cron registry, a heartbeat receiver that backs Healthchecks-style integrations from any source (not just crons — also long-running jobs, backups, and the local-watchdog itself), and the foundation for the digest's "Cron heartbeat report" section (per spec §11 digests).

## Source documents (must be read by every session before working in this epic)

- **Spec:** `docs/superpowers/specs/2026-05-04-homelab-monitor-design.md` — relevant sections:
  - §2 Q5 (cron monitoring model — hybrid B/A/C decision)
  - §2 Q30 (existing scripts policy — public-A / overrides-B+C)
  - §3.1 (heartbeat receiver responsibility)
  - §6.1 (`crons` + `heartbeats_state` table contracts)
  - §11 (`/storage/scripts/cron/backup.sh` integration target — overrides repo, not public)
- **Project memory** at `/home/jakekausler/.claude/projects/-storage-programs-homelab-monitor/memory/`:
  - `reference_homelab_inventory.md` — the user's existing crons (4 known: 1 user, 3 system) plus the `homelab-monitor-overrides` separation
  - `project_repo_tooling.md` — monorepo, CRG, workflow choices
- **CLAUDE.md** at the repo root — verify command, status values, code review graph slash commands, the "no `git add -A`" rule
- **EPIC-001.md** at `epics/EPIC-001-foundation/EPIC-001.md` — cross-stage acceptance criteria are inherited; foundation behaviors (alert ingestor, dispatcher, render-on-boot, vmalert) that this epic builds on
- **STAGE-001-021's heartbeat router stub** at `apps/monitor/homelab_monitor/kernel/api/routers/heartbeat.py` — the entrypoint this epic fleshes out (auth boundary already in place via `Scope.HEARTBEAT_WRITE`)

## Stages

| Stage         | Name                                                                  | Status      |
| ------------- | --------------------------------------------------------------------- | ----------- |
| STAGE-002-001 | Heartbeat receiver + `crons`/`heartbeats_state` schema + audit         | Not Started |
| STAGE-002-002 | Cron registry CRUD API + Inventory "Crons" tab UI                     | Not Started |
| STAGE-002-003 | Cron auto-discovery + suggestion queue integration                    | Not Started |
| STAGE-002-004 | B-mode log-scrape (vector + journald + collector parsing)             | Not Started |
| STAGE-002-005 | C-mode wrapper helpers + dashboard "Install heartbeat" action         | Not Started |
| STAGE-002-006 | vmalert rules for stale heartbeats + lateness ladder + flap detector  | Not Started |

## Current Stage: STAGE-002-001
## Current Phase: Design (not yet started)

## Cross-stage acceptance criteria

Inherits all five EPIC-001 cross-stage criteria (see `epics/EPIC-001-foundation/EPIC-001.md` §Cross-stage acceptance criteria):

1. `make verify` green (full pipeline; `make test-fast` is for local iteration only).
2. New tests for the stage's behavior; 100% kernel coverage gate maintained.
3. Demoable vertical slice (UI change at `http://localhost:9090`, API response, or deterministic integration test).
4. No `git add -A` or `git add .` — specific paths only.
5. Changelog entry in `changelog/$(date +%Y-%m-%d).changelog.md`; ADR for any architecture-changing decision.

Plus EPIC-002-specific criteria:

6. **No existing cron is modified** without explicit user confirmation through the dashboard (per spec Q30 default). The wrapper installer in STAGE-002-005 must produce copy-paste output by default; "push to host" is opt-in per cron.
7. **Heartbeat endpoints accept API tokens only** — no anonymous heartbeat posts. Tokens carry `heartbeat:write` scope; rate-limited per-token.
8. **B-mode parsing is idempotent** — re-processing the same syslog/journald line never double-counts. Achieved via a high-water mark per cron + per-line content hash.
9. **All `crons` rows must originate from either auto-discovery (suggested) or explicit user creation** — the heartbeat receiver does NOT create cron rows on first ping (404 forces explicit registration; prevents typos in cron commands from polluting the registry).
10. **`heartbeats_state` is derived state** — every value can be recomputed from the events log (heartbeat receiver writes + B-mode collector writes). Treat it as a materialized view; never the source of truth.
11. **All cron schedules parsed in the host's local timezone**, not UTC (per spec §16 — cron schedules are user-facing and must match what the user sees in `crontab -e`). Display layer also shows local time. Internal `expected_next_at` and `last_ok_at` columns remain UTC.

## Sequential dependency notes

- **STAGE-002-001 depends on**: STAGE-001-021's heartbeat router stub (`apps/monitor/homelab_monitor/kernel/api/routers/heartbeat.py` — replaces with persistent receiver), STAGE-001-013's alert ingestor (freshness-alert path), STAGE-001-017's AM render-on-boot (heartbeat-freshness vmalert rules need rendering on container boot), STAGE-001-004's `crons`/`heartbeats_state` minimal-schema stubs (this stage's migration adds behavioral columns). It is the foundation for everything else in this epic.
- **STAGE-002-002 depends on STAGE-002-001**: the CRUD API mutates the rows that STAGE-002-001's migration created.
- **STAGE-002-003 depends on STAGE-002-002**: discovery creates suggestions; accepting a suggestion calls the CRUD API. STAGE-002-002 must ship the API first.
- **STAGE-002-004 depends on STAGE-002-001 + STAGE-001-016**: B-mode collector reads vector-shipped logs from VictoriaLogs and updates `heartbeats_state`. Requires STAGE-001-016's vector + VL infrastructure.
- **STAGE-002-005 depends on STAGE-002-001 + STAGE-002-002**: the wrapper script POSTs to the receiver (001); the dashboard action surfaces in the cron-row detail page (002).
- **STAGE-002-006 depends on STAGE-002-001**: rules query metrics derived from `heartbeats_state`. Also depends on STAGE-001-017 for vmalert metrics rules location convention.

Stages 001 → 002 → 003 are strictly sequential. Stages 004, 005, 006 can be developed in parallel after 003 ships, but the recommended order is 004 → 005 → 006 so that the demo at the end of 005 has B-mode in place too.

## Notes

- **Cron expressions stored as strings, parsed lazily.** The `croniter` library is the recommended parser (well-maintained, handles `@reboot`, `@hourly`, etc., and supports `next()`/`prev()` for `expected_next_at` math). Final library choice is brainstormed in STAGE-002-002 Design.
- **`@reboot` jobs are tracked, not heartbeated.** A `@reboot` cron fires once per boot; STAGE-002-003 records `last_seen_state` based on "did this fire within N minutes after the last self-monitor-recorded boot timestamp?" — see STAGE-002-003 Decisions.
- **systemd timers are out of scope for this epic.** Spec §3.1 mentions `systemctl list-timers --all --output=json` as a discovery source, but that's a follow-up stage in EPIC-003 or later. STAGE-002-003 covers `/etc/cron.{d,daily,hourly,weekly,monthly}/` plus root + per-user crontabs only.
- **Existing user crons (per project memory)** are NOT modified by anything in EPIC-002:
  - `@reboot /storage/scripts/startup/startup.sh` — discovered + tracked in `observe` mode
  - `17 * * * * /storage/scripts/rtlamr-watchdog.sh` — discovered + tracked in `observe` mode
  - `/etc/cron.d/certbot` — discovered + tracked in `observe` mode (renewal is critical; B-mode log-scrape will catch failures)
  - `/storage/scripts/cron/backup.sh` — discovered + tracked in `observe` mode in public release; the host-overrides repo will flip this to `heartbeat` mode and inject the wrapper. That edit is NOT made by this epic.
- **The `homelab-monitor-overrides` repo wiring is NOT required for EPIC-002 completion.** EPIC-002 ships the public-safe defaults; the overrides repo is wired into a later epic (likely EPIC-008 or EPIC-014).
- **Heartbeat receiver is reusable beyond crons.** The receiver accepts any `<id>` registered in the `crons` table. EPIC-014 (self-monitor + local-watchdog) will register `local-watchdog` itself as a "cron" with cadence `1m` and use the same receiver to detect monitor death.
