# EPIC-009: Auto-fix subsystem

## Status: Not Started

## Overview

Build the auto-fix subsystem under the strict safety model defined in spec §7.4 and project memory `project_autofix_safety_model.md`. Auto-fix invokes `claude --dangerously-skip-permissions -p <runbook-folder>` against allow-listed alert types only, runs as the dedicated low-privilege OS user `homelab-fixer` (with curated file ACLs and narrowly-scoped sudoers entries), records every run in full detail, supports dry-run gates for risky runbooks, enforces rate-limit and cooldown, and exposes a single dashboard kill switch.

This is the highest-blast-radius epic. Every stage requires extra scrutiny in code review.

## Source documents (MUST be read before any stage)

- Spec §7.4 (auto-fix isolation — non-negotiable rules), §3.1 (runbook orchestrator), §6.1 (`runbooks`, `runbook_runs` tables), §10.1 (fixer-runner OPTIONAL container), §10.2 (volume mounts).
- Project memory `project_autofix_safety_model.md` — the seven non-negotiables. ANY change to auto-fix must satisfy all seven; if not, flag explicitly and stop.

## The seven non-negotiables (recap)

1. **Trigger:** allow-list per alert type only. Default = manual button. Auto-trigger only for explicitly opted-in alert types.
2. **Scope:** dedicated runbook folder per issue class, each with its own `CLAUDE.md`. Never invoke Claude on broad/unrelated paths.
3. **Identity:** runs as `homelab-fixer`, never as `jakekausler` or root.
4. **Audit:** every run records alert_id, runbook_path, prompt, full transcript/stdout/stderr, exit_code, started/ended timestamps, runbook_hash, fixer_user, host.
5. **Dry-run:** runbooks tagged `risky` must produce a plan first; plan requires explicit user approval before a real run.
6. **Rate limit + cooldown:** max N runs/hour globally, per-runbook cooldown to prevent tight loops.
7. **Kill switch:** single dashboard control disables all auto-fix immediately. Killable mid-run.

## Stages (to decompose during epic Design phase)

| Likely stage | Theme |
|---|---|
| STAGE-009-001 | `homelab-fixer` user provisioning: setup script (in the host-overrides repo, but instructions documented in the public release) creates the user, sets ACLs, configures the narrow sudoers entry; documents the entire OS-side setup |
| STAGE-009-002 | `fixer-runner` container architecture decision (Design phase): container vs native exec. Recommendation: container. Build a minimal Dockerfile with `claude` CLI installed, configured to run as `homelab-fixer`. The orchestrator `docker exec`s into it to launch runbooks |
| STAGE-009-003 | Runbook registry: `runbooks` table CRUD, allow-list management UI in Settings; `runbook_match_patterns` (regex against alertname / labels), `risk_tag`, `dry_run_required`, `rate_limit_per_hour`, `cooldown_seconds` |
| STAGE-009-004 | Runbook orchestrator: receives an alert, looks up matching runbook, checks kill switch + allow-list + rate-limit + cooldown, claims the runbook, spawns Claude in fixer-runner, captures transcript, writes audit |
| STAGE-009-005 | Dry-run mode: when `risk_tag == "risky"`, the orchestrator first runs Claude with a prompt asking for a *plan only* (no execution); plan is stored, surfaced in dashboard, awaits explicit user approval before triggering the real run |
| STAGE-009-006 | Kill switch: a single boolean in `audit_log`-tracked config; orchestrator checks before every run; mid-run kill via signal/`docker kill`; dashboard control with confirm-on-destructive |
| STAGE-009-007 | Auto-fix history UI: filterable table, transcript viewer with Claude session output, before/after diffs (when generated), exit code, durations |
| STAGE-009-008 | "Run fix" button on the alert detail drawer (extends STAGE-001-019's right-side drawer): visible only when an alert matches a runbook; dry-run vs real toggle (forced to dry for risky) |
| STAGE-009-009 | First built-in example runbook: `pihole-restart-loop` — when Pi-hole crashes 5x in 10min, this runbook has a `CLAUDE.md` describing the allowed actions ("docker compose restart pihole-unbound", "tail logs", etc.) and is shipped as an _example only_ (not auto-enabled) |
| STAGE-009-010 | Per-runbook scratch space: `data/runbook-transcripts/<runbook>/<run-id>/` with allowed write permissions for `homelab-fixer`; ACL setup verified; transcript rotation per spec §10.2 |
| STAGE-009-011 | Audit log enrichment for compliance: every runbook_run row carries the runbook hash (so we know if the runbook itself was modified between runs); transcripts retained per retention policy; cannot be deleted via API (audit immutability) |
| STAGE-009-012 | Network egress control for fixer-runner: outbound allowed (Claude needs Anthropic API); inbound denied; explicit allow-list for any other endpoints the runbook needs (configured per runbook) |

## Cross-stage acceptance criteria

Same as EPIC-001 plus:

- **Every stage's tests must include the seven non-negotiables as assertions** wherever applicable. E.g., a test that an unauthenticated runbook trigger returns 401; a test that a risky runbook can never bypass dry-run; a test that the kill switch denies execution within 100ms.
- **No auto-fix path bypasses any non-negotiable.** Code reviewer (Opus) must explicitly verify this in every stage.
- **Test runs use a fake `claude` binary** (a shell script that records args and emits a recorded transcript). Real Claude API calls are NEVER made in CI.

## Dependencies

- EPIC-001 (alerts, audit log, dispatcher).
- EPICs 002–008 not strictly required, but having real alerts to fire makes integration testing more realistic.
- EPIC-005 (HA push) recommended — the dry-run "approval needed" notification routes there for fast response.

## Notes

- The first built-in example runbook (`pihole-restart-loop`) is intentionally simple. Real auto-fix runbooks live in the user's `homelab-monitor-overrides` repo. The public release ships exemplars only.
- Claude's runtime cost matters: each runbook execution invokes the Anthropic API. Rate-limit defaults are conservative (e.g., 5 runs/hour globally) to prevent runaway costs.
- "Risky" tagging is at the runbook author's discretion. The default for any new runbook is `risky=true` — it must be explicitly downgraded to `safe` in the runbook's own `CLAUDE.md` after sufficient hands-on validation. Bias toward dry-run.
- The fixer-runner container vs native exec choice is the only major architectural decision deferred from the spec; the spec acknowledges either is acceptable. STAGE-009-002 Design phase commits.
- **EPIC-004 brainstormed integration (2026-05-28):** EPIC-004 (logs pipeline) ships user-curated alert authoring (STAGES 004-042..044). It explicitly defers **auto-generated** alert rules from Drain log signatures to "a future Claude-integration epic." That follow-on epic — TBD epic number, likely after EPIC-019 — will: (a) consume the `homelab_log_signature_count` + `_first_seen_ts` + `_total` metrics from EPIC-004 STAGE-004-027, (b) call Claude with a curated set of signatures + their sample lines from the catalog (STAGE-004-028), (c) propose alert rules via the same persistence model EPIC-004 STAGE-004-042 ships (`log_user_rules` SQLite table + render-on-boot), (d) surface proposals in the homelab-monitor UI for user approval. Until that epic exists, EPIC-004 + EPIC-009 are deliberately decoupled — EPIC-009's auto-fix runs against allow-listed alerts only; signature-driven auto-generation is a separate concern.
