# EPIC-002: Heartbeat receiver + cron registry + cron auto-discovery

## Status: In Progress

**(Re-opened 2026-05-19. The original 12 stages STAGE-002-001 … 010 are Complete. Five appended stages STAGE-002-011 … 015 add cron run history & run-log viewing — see `docs/superpowers/specs/2026-05-19-cron-run-logs-design.md`. EPIC-002 will be 17 stages when complete.)**

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

**Note:** EPIC-002 was reshaped on 2026-05-11 by the cron-derived-state-redesign brainstorm (`docs/superpowers/specs/2026-05-11-cron-derived-state-redesign.md`). After STAGE-002-002 ships as-is, four new stages (003–006) introduce the fingerprint identity model + remove manual-create + add the `/register` endpoint + redesign the UI. The original 003–006 are renumbered 007–010 and reworked to the derived-state model.

| Stage         | Name                                                                  | Status      |
| ------------- | --------------------------------------------------------------------- | ----------- |
| STAGE-002-001 | Heartbeat receiver + `crons`/`heartbeats_state` schema + audit         | Complete |
| STAGE-002-002 | Cron registry CRUD API + Inventory "Crons" tab UI                     | Complete |
| STAGE-002-003 | Schema redesign — fingerprint identity, drop integration_mode, rename archived_at→hidden_at, add source_path + wrapper_installed_at | Complete |
| STAGE-002-004 | API removal — drop POST /api/crons + AddCronModal + CronCreate + create-mode of CronForm | Complete |
| STAGE-002-005 | `/api/hb/{fingerprint}/register` endpoint + heartbeat receiver behavior change | Complete |
| STAGE-002-006 | UI redesign — 4-panel detail page, hidden replaces archive, remote-banner, wrapper-installed display | Complete |
| STAGE-002-007 | Cron auto-discovery — REWORKED to emit fingerprints, populate registry directly, populate disk-source fields (was 003) | Complete |
| STAGE-002-007A | Auto-soft-delete crons no longer found by discovery scan; auto-restore when found again | Complete |
| STAGE-002-008 | B-mode log-scrape — REWORKED to match logs by fingerprint heuristic, runs always (was 004) | Complete |
| STAGE-002-009 | Wrapper helpers — REWORKED to embed fingerprint at install time, call /register first, "Install heartbeat" UI button local-host only (was 005) | Complete |
| STAGE-002-009A | Wrapper removal helpers — uninstall wrapper, restore original crontab line; Install/Remove UI toggle | Complete |
| STAGE-002-010 | vmalert rules — REWORKED to include wrapper-health alert via separate monitoring-health channel (was 006) | Complete |
| STAGE-002-011 | `cron_runs` table + per-run history schema + heartbeat `run_id` threading | Not Started |
| STAGE-002-012 | Wrapper rewrite — generic shared script, run-UUID capture, per-line tagging, Vector transform | Not Started |
| STAGE-002-013 | VictoriaLogsClient + CronRunReconciler + B-mode log-scrape run rows | Not Started |
| STAGE-002-014 | Run-history API + narrow run-log endpoint + anomaly heuristics v1 | Not Started |
| STAGE-002-015 | Run-history UI — teaser panel + run-history list route + run-log viewer route | Not Started |

## Current Stage: STAGE-002-011
## Current Phase: Design

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
9. **All `crons` rows must originate from auto-discovery or the heartbeat `/register` endpoint** (derived-state model after STAGE-002-005). The `POST /api/crons` manual-create endpoint is removed in STAGE-002-004; the heartbeat receiver continues to 404 on unknown fingerprints for `/start//ok//fail` (creation happens only via `/register` with metadata, or via discovery). **Until STAGE-002-005 ships, the original "404 on unknown" rule from STAGE-002-001 remains in effect.** _(Updated 2026-05-11 to reflect derived-state redesign; superseded original criterion that allowed manual user creation.)_
10. **`heartbeats_state` is derived state** — every value can be recomputed from the events log (heartbeat receiver writes + B-mode collector writes). Treat it as a materialized view; never the source of truth.
11. **All cron schedules parsed in the host's local timezone**, not UTC (per spec §16 — cron schedules are user-facing and must match what the user sees in `crontab -e`). Display layer also shows local time. Internal `expected_next_at` and `last_ok_at` columns remain UTC.

## Sequential dependency notes

- **STAGE-002-001 depends on**: STAGE-001-021's heartbeat router stub (replaces with persistent receiver), STAGE-001-013's alert ingestor, STAGE-001-017's AM render-on-boot, STAGE-001-004's stubbed minimal-schema migrations. Foundation for everything else in this epic.
- **STAGE-002-002 depends on STAGE-002-001**: the CRUD API mutates the rows that STAGE-002-001's migration created. (Ships with manual-create + UUID PK + integration_mode — these are removed/replaced in STAGE-002-003 + 002-004.)

**Redesign block (new stages, must ship in strict order):**
- **STAGE-002-003 depends on STAGE-002-002**: schema redesign drops the rows STAGE-002-002 was just shipped against. Destructive migration acceptable (dev seed data only).
- **STAGE-002-004 depends on STAGE-002-003**: API removal happens after the fingerprint PK is in place (so removing `POST /api/crons` doesn't break anything else mid-flight).
- **STAGE-002-005 depends on STAGE-002-003 + STAGE-002-004**: the `/register` endpoint uses fingerprint identity (003) and is the replacement creation path for the now-removed manual create (004).
- **STAGE-002-006 depends on STAGE-002-003 + STAGE-002-004 + STAGE-002-005**: UI redesign reflects the final shape — fingerprint identity, no add modal, `/register` is the heartbeat-side creation path.

**Reworked downstream stages:**
- **STAGE-002-007 depends on STAGE-002-006**: discovery writes fingerprint-keyed rows directly; UI must already match the new schema before users see auto-populated rows.
- **STAGE-002-008 depends on STAGE-002-001 + STAGE-001-016 + STAGE-002-007**: B-mode collector matches logs to fingerprints; requires fingerprint identity + at-least-one cron in registry from discovery.
- **STAGE-002-009 depends on STAGE-002-005 + STAGE-002-006 + STAGE-002-007**: wrapper helpers POST `/register` (005), the install UI lives in the 4-panel detail page (006), and they target crons discovered locally (007).
- **STAGE-002-010 depends on STAGE-002-001 + STAGE-002-008 + STAGE-002-009**: rules query metrics from heartbeats_state (001) + the `logscrape_count_since_last_heartbeat` counter from log-scrape (008) + the `wrapper_installed_at` column from wrapper installs (009).

**Strict serial order: 001 → 002 → 003 → 004 → 005 → 006 → 007 → 008 → 009 → 010.** The redesign block (003–006) cannot interleave with anything else because each stage depends on the previous one's schema/API/UI state.

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

## Cross-epic carry-forward → EPIC-004

The cron run history & run-log work (STAGE-002-011 … 015, design spec `docs/superpowers/specs/2026-05-19-cron-run-logs-design.md`) defers three capabilities to EPIC-004. When EPIC-004 (Logs Pipeline) is designed, the following MUST be added as explicit EPIC-004 acceptance criteria:

- **STAGE-004-002 (Drain clustering + error-keyword work)** must apply to **cron run logs**, not only generic service logs — anomaly detection v2/v3 (error-keyword scan, Drain content clustering) is backfilled onto the `cron_runs` history produced by EPIC-002.
- **STAGE-004-005 (logs explorer)** must explicitly include **live-tail of in-flight cron runs** as in-scope.
- The generic `/api/logs` LogsQL proxy (EPIC-004) must be built on top of the `VictoriaLogsClient` module introduced in STAGE-002-013.
