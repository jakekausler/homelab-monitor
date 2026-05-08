# EPIC-001: Foundation

## Status: Not Started

## Overview

Lay the architectural foundation for the homelab-monitor service: repo skeleton (backend + frontend), database, secrets, kernel (plugin host + scheduler + concurrency + subprocess support), API + auth, the first end-to-end vertical slice (a real `host` collector emitting a metric that flows through the dispatcher to the dashboard), and finally the integrated sidecars (VictoriaMetrics, VictoriaLogs, Alertmanager, vmalert ×2, Karma, Grafana) wired to the monitor with a canonical e2e integration test.

After EPIC-001 is complete, the system has: a working CI pipeline, a verified development workflow, the kernel + plugin contract, real time-series and log storage, real alerting routed through Alertmanager and surfaced via Karma, dashboards-as-code in Grafana, the alert lifecycle UI embedded into our own dashboard, and an integration test rig that proves the full path from a metric to a dispatched alert. Subsequent epics layer integrations and features on top without touching the foundation.

## Source documents (must be read by every session before working in this epic)

- **Spec:** `docs/superpowers/specs/2026-05-04-homelab-monitor-design.md` (the source of truth — every load-bearing decision lives here, with §-references on each row of the decisions table)
- **Project memory** at `/home/jakekausler/.claude/projects/-storage-programs-homelab-monitor/memory/`:
  - `reference_homelab_inventory.md` — homelab hardware, HA URL+token, TLS setup
  - `reference_docker_inventory.md` — running and disabled containers
  - `project_autofix_safety_model.md` — auto-fix non-negotiables (relevant later but read once)
  - `project_repo_tooling.md` — monorepo, CRG, workflow choices
- **CLAUDE.md** at the repo root — project conventions, verify command, status values, code review graph slash commands.

## Stages

| Stage         | Name                                                  | Status      |
| ------------- | ----------------------------------------------------- | ----------- |
| STAGE-001-001 | Backend Python skeleton                               | Complete    |
| STAGE-001-002 | Frontend skeleton                                     | Complete    |
| STAGE-001-003 | CI + Code Review Graph + Dependabot                   | Complete    |
| STAGE-001-004 | SQLite + Alembic + first migration                    | Complete    |
| STAGE-001-005 | Encrypted secrets store                               | Complete    |
| STAGE-001-006 | Collector protocol + base classes                     | Complete    |
| STAGE-001-007 | In-process plugin loader + scheduler                  | Complete    |
| STAGE-001-008 | Concurrency groups + failure budget + quarantine      | Complete |
| STAGE-001-009 | Subprocess plugin runner + JSON line protocol         | Complete |
| STAGE-001-010 | FastAPI app shell + healthz + structured logging      | Complete    |
| STAGE-001-011 | Local auth (bcrypt + sessions + CSRF)                 | Complete    |
| STAGE-001-012 | First built-in `host` collector                       | Complete    |
| STAGE-001-013 | Alert ingestor + first `inproc-dashboard` channel     | Complete    |
| STAGE-001-014 | UI shell + login + Overview live-tile                 | Complete |
| STAGE-001-015 | VictoriaMetrics + vmagent                             | Complete |
| STAGE-001-015A | Backup + disk budget + minimal test rig extension    | Complete |
| STAGE-001-016 | VictoriaLogs + vector                                 | Complete |
| STAGE-001-017 | Alertmanager + vmalert (metrics) + first rule         | Complete    |
| STAGE-001-018 | vmalert (logs) + first log-derived rule               | Complete    |
| STAGE-001-019 | Karma + kthxbye                                       | Not Started |
| STAGE-001-020 | Grafana + dashboards-as-code provisioning             | Not Started |
| STAGE-001-021 | Full integration test rig + canonical e2e test        | Not Started |

## Current Stage: STAGE-001-019
## Current Phase: Design (Complete)

## Cross-stage acceptance criteria

Every stage in this epic must finish with:

1. `make verify` (or `scripts/verify`) green — the canonical multi-language verify pipeline. Concrete contents per the spec §12. Do not skip steps; do not introduce new tools without justification.
2. New tests for the stage's behavior. The kernel coverage gate is 100%; plugin coverage is aspirational with case-by-case exemptions.
3. A clear, demoable vertical slice: either a UI change visible at `http://localhost:9090`, or an API response, or a deterministic integration test that proves the new behavior.
4. No `git add -A` or `git add .` — only specific file paths.
5. Documentation: changelog entry in `changelog/$(date +%Y-%m-%d).changelog.md` with stage ID, summary, and commit hash; relevant ADR (`docs/adr/`) for any architecture-changing decision.

## Sequential dependency notes

- 001 → 002: Backend skeleton must exist before frontend so workspace tooling can sit at the repo root with the right shape.
- 003 depends on 001 + 002: CI runs both verify pipelines.
- 004 depends on 003: migrations need CI to lint them.
- 005 depends on 004: secrets table is one of the migrations.
- 006 → 007 → 008 → 009: kernel plugin pieces, in order.
- 010 (API shell) can run in parallel with 006-009 but is shown after for narrative simplicity.
- 011 (auth) depends on 004 (users/sessions tables) and 010 (API shell).
- 012 (first collector) depends on 007 (loader + scheduler) and 010 (API shell to surface status).
- 013 (alert ingestor) depends on 010 (API), 004 (alerts table — added in this stage's migration, see stage notes).
- 014 (UI live tile) depends on 002, 010, 011, 012, 013.
- 015–020 depend on most prior stages and on each other in the order shown (VM before vmalert; AM before Karma; vmalert metrics before vmalert logs).
- 021 finishes the epic with the integration test rig that exercises everything.

## Notes

- This epic is intentionally larger than future epics because it delivers the foundation everything else stands on. Each stage is sized to a single session.
- The `homelab-fixer` user, runbook orchestrator, and auto-fix subsystem do **not** appear in EPIC-001. They live in EPIC-009. The kernel must not couple to those concepts here.
- The discovery engine, suggestion queue, and tool-effectiveness analyzer also do **not** appear in EPIC-001. The data model created in stage 004 includes their tables (so later epics can land additive migrations), but no logic.
- The `homelab-monitor-overrides` repo is **not** required for EPIC-001 to be considered complete. It will be wired in during EPIC-002 or EPIC-003.
