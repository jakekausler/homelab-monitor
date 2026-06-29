# EPIC-009: Auto-fix subsystem

## Status: In Progress (4/13 stages — STAGE-009-001..013; decomposed 2026-06-29)

## Overview

Build the auto-fix subsystem under the strict safety model defined in spec §7.4 and project memory `project_autofix_safety_model.md`. Auto-fix invokes `claude --dangerously-skip-permissions -p <runbook-folder>` against allow-listed alert types only, runs as the dedicated low-privilege OS user `homelab-fixer` inside a dedicated **`fixer-runner` container**, records every run in full detail, supports dry-run gates for risky runbooks, enforces rate-limit and cooldown, and exposes a single dashboard kill switch.

This is the **highest-blast-radius epic**. Every stage requires extra scrutiny in code review. The code-reviewer (Opus) must explicitly verify, every stage, that no auto-fix path bypasses any of the seven non-negotiables.

## Source documents (MUST be read before any stage)

- Spec §7.4 (auto-fix isolation — non-negotiable rules), §3.1 (runbook orchestrator), §4.5 (auto-fix data flow), §6.1 (`runbooks`, `runbook_runs` tables), §10.1 (fixer-runner OPTIONAL container), §10.2 (volume mounts + transcript rotation).
- Project memory `project_autofix_safety_model.md` — the seven non-negotiables. ANY change to auto-fix must satisfy ALL seven; if a change weakens any of them, flag explicitly and STOP.

## The seven non-negotiables (recap)

1. **Trigger:** allow-list per alert type only. Default = manual button. Auto-trigger only for explicitly opted-in alert types.
2. **Scope:** dedicated runbook folder per issue class, each with its own `CLAUDE.md`. Never invoke Claude on broad/unrelated paths.
3. **Identity:** runs as `homelab-fixer`, never as `jakekausler` or root.
4. **Audit:** every run records alert_id, runbook_path, prompt, full transcript/stdout/stderr, exit_code, started/ended timestamps, runbook_hash, fixer_user, host. Audit is immutable (cannot be deleted via API).
5. **Dry-run:** runbooks tagged `risky` must produce a plan first; plan requires explicit user approval before a real run.
6. **Rate limit + cooldown:** max N runs/hour globally, per-runbook cooldown to prevent tight loops.
7. **Kill switch:** single dashboard control disables all auto-fix immediately. Killable mid-run.

## LOCKED architecture decisions (2026-06-29 planning session — do NOT re-litigate)

These four decisions were made with the user during EPIC-009 planning and supersede the original bulk-brainstorm stage table (which was written at project start and is stale). Stage Design phases inherit them.

### Decision 1 — Execution model = `fixer-runner` CONTAINER (NOT native exec)

A dedicated container runs as `homelab-fixer` with the `claude` CLI installed; the orchestrator (in the monitor process) `docker exec`s into it to launch a runbook. The spec (§7.4, §17) deferred "container vs native exec" to the start of EPIC-009; **committed to the container.** Requirements:

- The fixer is told — via a **STATIC, container-level `CLAUDE.md`** baked into / mounted in the `fixer-runner` — that it is running **inside a Docker container** (not the host), with an explicit allow/deny list of what it can and cannot do.
- The fixer runs **FULLY NON-INTERACTIVE**: it cannot accept user input. The orchestrator invokes `claude` in headless `-p` mode with stdin `/dev/null`, no prompts. The static container `CLAUDE.md` ALSO instructs "you are non-interactive; never wait for or request input."
- Killable mid-run via `docker kill` / signal (non-negotiable #7).
- The monitor (orchestrator) needs Docker-socket access to `docker exec` into `fixer-runner` — it already has the socket from EPIC-003 (Docker discovery); verify + document.

### Decision 2 — Isolation = per-runbook SCOPED CAPABILITIES granted directly to the fixer container (NOT a powerless broker)

The fixer container is granted exactly the access a runbook class needs — declared PER RUNBOOK. Claude executes within its granted scope (e.g. runs `docker restart pihole-unbound` itself if granted the docker capability). Structure:

- **A runbook is a FOLDER = one or more markdown detail files (the `CLAUDE.md`-style intent + allowed/forbidden actions, human + Claude facing) PLUS a structured config/manifest file (YAML/TOML).** The config declares: match patterns (regex vs alertname/labels), `risk_tag` (default `risky`), `dry_run_required`, `rate_limit_per_hour`, `cooldown_seconds`, AND the **scoped-capabilities block** (docker container + allowed actions; an optional SSH `target_id` reference to an existing EPIC-017 target; an egress allow-list). The markdown is prose; the config is machine-read.
- **Registry authority = FILE-AUTHORITATIVE.** The runbook folder's config file is the source of truth; the DB row is a registration/enablement record + a content hash (for audit non-negotiable #4).
- **Capability reality (verified against EPIC-007/008/017):** EPIC-017 SSH targets are **read-only** (server-side forced-command — one pinned command per target; a runbook cannot run arbitrary commands over an existing SSH target). EPIC-007 (Unifi) and EPIC-008 (Synology) are **strictly observe-only** (no device-action / restart / reboot surface). So the realistic near-term capability is the **docker socket** (local container actions, e.g. the pihole example). EPIC-009 ships **docker-capability runbooks**; an SSH `target_id` may be referenced for **read** access only. EPIC-009 does **NOT** build new write-capable SSH targets.

### Decision 3 — Claude→user IMPROVEMENT-FEEDBACK channel

When the fixer hits a limitation and exhausts its options, OR finishes but had to work around something inefficiently, it emits **structured feedback** ("this would've gone better with capability X / config change Y") that the orchestrator captures, persists, and the UI surfaces for the user to review. Implementation: a structured output contract (sentinel feedback file in the transcript dir / a structured stdout marker the orchestrator parses), persistence in a **sibling `runbook_run_feedback` table** (keeps `runbook_runs` clean; allows multiple feedback items per run), and a review surface in the Auto-fix UI. This is a NEW first-class concept with no downstream consumer yet (purely additive).

### Decision 4 — Conservative public-release defaults

Ship with auto-trigger **entirely OFF, ZERO enabled runbooks**. The `pihole-restart-loop` runbook ships as an **example only** in `runbooks/_examples/` (NOT registered/enabled). A fresh install has zero active auto-fix. Every runbook defaults to `risky=true` (dry-run required) until the user explicitly downgrades it in the runbook's own config/CLAUDE.md after hands-on validation. The user must explicitly **register + enable** a runbook AND explicitly **opt it into auto-trigger** — three separate gates.

## Stages (decomposed 2026-06-29 — supersedes the original bulk-brainstorm table)

| Stage | Theme | Type |
|---|---|---|
| STAGE-009-001 | Runbook schema & config-file contract: additive migration (runbooks + runbook_runs columns) + the runbook-folder config pydantic model (match patterns, risk_tag, dry_run, rate-limit/cooldown, scoped-capabilities block); file-authoritative registry + content hash | BACKEND |
| STAGE-009-002 | `homelab-fixer` provisioning + transcript ACLs: the fixer identity inside the container, `/data/runbook-transcripts/` ACLs, orchestrator docker-socket access verified; host-overrides setup script + public docs | HOST-INTEGRATION |
| STAGE-009-003 | `fixer-runner` container: Dockerfile (claude CLI, runs as homelab-fixer) + the STATIC container-level CLAUDE.md (in-container, non-interactive, hard allow/deny) + compose wiring as an OPTIONAL container (profiles, off by default) + the non-interactive invocation contract | BACKEND/DEPLOY |
| STAGE-009-004 | Runbook registry: folder loader (markdown + config) + DB registration/enablement (register ≠ enable ≠ auto-trigger, three gates) + content hash + allow-list management API | BACKEND |
| STAGE-009-005 | Orchestrator core: alert→match→checks(kill switch, allow-list/enabled, rate-limit, cooldown)→claim→grant scoped caps→docker-exec claude (non-interactive)→capture transcript→write runbook_runs + alert_outcomes('auto_fixed') + audit_log. Built + tested against the FAKE claude binary. Leaves a clean hook seam for the EPIC-012 maintenance-window cross-wire | BACKEND |
| STAGE-009-006 | Dry-run mode + approval flow: risky → plan-only claude run → store plan → surface → explicit user approval → real run. A risky runbook can NEVER bypass dry-run | BACKEND |
| STAGE-009-007 | Kill switch: `app_settings` boolean (`autofix_enabled`) + pre-run gate + mid-run `docker kill` of fixer-runner + audit on toggle + dashboard control with confirm-on-destructive; <100ms denial + mid-run-kill assertions | BACKEND (+ small FRONTEND control) |
| STAGE-009-008 | Per-runbook scoped-capability granting + fixer-runner egress control: how the config's declared capabilities (docker socket / bind-mounts / SSH target_id read-ref / egress allow-list) are actually granted to the fixer container at exec time; inbound denied, outbound allow-listed (Anthropic API always) | BACKEND/DEPLOY |
| STAGE-009-009 | Claude→user improvement-feedback channel: structured feedback output contract + `runbook_run_feedback` sibling table + UI review surface | BACKEND + FRONTEND |
| STAGE-009-010 | Auto-fix UI: Runbooks screen (replaces the 'Coming soon' nav placeholder; catalog cards) + manual "Run fix" trigger surface (no alert drawer exists — create the surface; dry-vs-real toggle, forced-dry for risky, confirm-on-destructive for real) | FRONTEND |
| STAGE-009-011 | Auto-fix history UI: filterable runbook_runs table + transcript viewer (full Claude session) + exit codes/durations/mode + improvement-feedback display | FRONTEND |
| STAGE-009-012 | Audit immutability + runbook_hash enrichment + transcript rotation: runbook_runs/audit rows not API-deletable; runbook_hash per run (detect runbook changed between runs); transcript rotation per §10.2 (keep last N=100, max age 365d, prune-not-silently-delete, audit row retained even after transcript file gone) | BACKEND |
| STAGE-009-013 | `pihole-restart-loop` example runbook (folder: markdown + config declaring docker capability for `pihole-unbound` action `restart`, `risk_tag: risky`), shipped in `runbooks/_examples/` NOT registered/enabled; end-to-end pipeline validation against the fake claude. **Epic-closing stage** | BACKEND/CONTENT |

## Dependency ordering rationale

Dependencies flow upward (lower number = earlier):

- **001 (schema)** is the foundation — the migration + config model everything else reads/writes.
- **002 + 003 (fixer infra)** stand up the `homelab-fixer` identity, ACLs, and the `fixer-runner` container the orchestrator execs into. 003 depends on 002's identity/ACLs.
- **004 (registry)** depends on 001 (schema) — loads/validates/registers runbook folders.
- **005 (orchestrator core)** is the keystone — it CANNOT be built before 001 (schema), 003 (fixer-runner to exec into), and 004 (registry to match against). Built against the fake claude.
- **006 (dry-run)**, **007 (kill switch)** layer safety gates onto the 005 orchestrator.
- **008 (capability granting + egress)** makes the scoped-capabilities the orchestrator grants actually real at exec time; depends on 003 (container) + 005 (orchestrator exec path).
- **009 (feedback)** depends on 005 (a run to attach feedback to) + the transcript contract.
- **010 + 011 (UI)** depend on the backend surfaces (registry API, orchestrator, runbook_runs, feedback) being present.
- **012 (audit immutability + rotation)** hardens the audit/transcript surface 005 writes.
- **013 (example runbook)** validates the whole pipeline end-to-end and closes the epic.

**Sequencing risk:** the orchestrator (005) is the bottleneck — schema (001) + fixer-runner (003) + registry (004) must all land first. The UI stages (010/011) and the audit-hardening (012) can be reordered among themselves but should follow 005/006/007/009.

## Cross-stage acceptance criteria

Same as EPIC-001 plus:

- **Every stage's tests must include the seven non-negotiables as assertions** wherever applicable. E.g.: an unauthenticated runbook trigger returns 401; a risky runbook can never bypass dry-run; the kill switch denies execution within ~100ms; rate-limit + cooldown are enforced; an audit row is written and is NOT API-deletable.
- **No auto-fix path bypasses any non-negotiable.** The code-reviewer (Opus) must explicitly verify this in every stage.
- **Test runs use a FAKE `claude` binary** — a shell script that records its args and emits a recorded transcript. REAL Claude API calls are NEVER made in CI. Every orchestration stage's tests use this fake.
- **100% kernel branch coverage**, `pyright --strict`, no bare `Any`, no `# type: ignore`.

## Dependencies

- **EPIC-001** (alerts, `alert_outcomes`, `audit_log`, dispatcher) — required. The stub `runbooks` / `runbook_runs` tables + `app_settings` (kill-switch home) ship from EPIC-001/004.
- **EPIC-003** (Docker) — the monitor already has docker-socket access (orchestrator execs into fixer-runner; docker-capability runbooks act via the socket).
- **EPIC-006** (Pi-hole) — `pihole-unbound` is the local container the example runbook targets.
- **EPIC-017** (SSH probe framework, DONE) — a runbook config may reference an existing SSH `target_id` for READ access (forced-command, read-only). EPIC-009 does not build new write-capable targets.
- EPICs 002/004/005/007/008 not strictly required, but real alerts make integration testing more realistic.

## What EPIC-009 provides downstream (contracts — do NOT break)

- **`runbook_runs` full records** (`mode` dry/real, `exit_code`, `transcript_path`, timestamps) → consumed by **EPIC-013 STAGE-013-004** (digest "auto-fix activity" section: Claude runs, dry-runs, exit codes, transcript links).
- **`alert_outcomes.outcome='auto_fixed'` rows** → consumed by **EPIC-010 STAGE-010-001/002** (tool-effectiveness action-rate aggregation).

## What EPIC-009 must NOT build (owned elsewhere)

- **Signature-driven alert-rule AUTO-GENERATION** from Drain log signatures (consuming EPIC-004's `homelab_log_signature_*` metrics). This is a SEPARATE future Claude-integration epic (TBD, ~EPIC-023+, unallocated — verified no epic 020/021/022 claims it). EPIC-009's auto-fix runs against ALLOW-LISTED ALERTS ONLY. Do NOT create stages for signature→rule generation.
- **The EPIC-012 maintenance-window cross-wire** (orchestrator opt-in to create a short maintenance window suppressing alert noise during a fix). EPIC-012 line 53 explicitly owns this as "a small follow-up after this epic lands." STAGE-009-005 leaves a clean hook seam; EPIC-012 wires it up. Do NOT build the maintenance-window integration here.

## Notes

- The first built-in example runbook (`pihole-restart-loop`) is intentionally simple. Real auto-fix runbooks live in the user's `homelab-monitor-overrides` repo. The public release ships exemplars only (Decision 4).
- Claude's runtime cost matters: each runbook execution invokes the Anthropic API. Rate-limit defaults are conservative (e.g., 5 runs/hour globally) to prevent runaway costs.
- "Risky" tagging is at the runbook author's discretion. The default for any new runbook is `risky=true` — it must be explicitly downgraded to `safe` in the runbook's own config after sufficient hands-on validation. Bias toward dry-run.
- **Original (stale) stage table:** the project-start bulk-brainstorm proposed 12 stages (STAGE-009-001..012) with a "container vs native decision" stage. That table is SUPERSEDED by the 2026-06-29 decomposition above (the container/native decision is pre-made; the stages are re-shaped around the four locked decisions). The original is preserved only in git history.
