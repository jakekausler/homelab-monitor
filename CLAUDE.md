# homelab-monitor — Claude operating instructions

This file is loaded by Claude Code on every session in this project. It encodes the conventions and workflow for working in this repo.

## Project overview

Self-hosted homelab monitoring service. Single user. Detects issues / anomalies / failures across containers, hosts, network gear, NAS, ISP, and HA, with optional auto-remediation via `claude --dangerously-skip-permissions` against allow-listed runbook folders.

**Source of truth:** `docs/superpowers/specs/2026-05-04-homelab-monitor-design.md`. Read this BEFORE making any non-trivial change. Future sessions do not have access to the original brainstorming dialogue; the spec captures every load-bearing decision.

**Architecture in one line:** Python/FastAPI kernel + plugin layer; sidecars are VictoriaMetrics, VictoriaLogs, Alertmanager, vmalert ×2, Karma + kthxbye, Grafana, Netdata, vector, local-watchdog, optional fixer-runner. React + Vite + TS strict frontend. SQLite (Core, not ORM) for state.

**Auto-fix safety model:** allow-list per alert type; dedicated `homelab-fixer` low-priv user; per-runbook rate-limit + cooldown; risky runbooks require dry-run + ack first; full audit; global kill switch. Defined in §7.4 of the spec and in project memory `project_autofix_safety_model.md`.

## Development Workflow

This project uses **epic-stage-workflow** for all implementation work. The brainstorm produced a detailed design spec; epics and stages decompose that spec into trackable, session-sized units.

### Hierarchy

- **Epic** = a coherent feature or capability (e.g., "Foundation", "Home Assistant integration", "Auto-fix subsystem")
- **Stage** = a single component or interaction within an epic; sized to be done in a single session
- **Phase** = Design → Build → Refinement → Finalize, executed sequentially per stage

### Phase Cycle Per Stage

Each stage goes through 4 phases, each typically in a separate session:

1. **DESIGN** — Present approach options, user picks, confirm seed data and acceptance test
2. **BUILD** — Implement to make verify pass; add seed data and placeholders
3. **REFINEMENT** — Dual sign-off (Desktop AND Mobile, plus any cross-cutting concerns)
4. **FINALIZE** — Tests, code review, docs, commit (all via subagents)

## Commands

| Command | Purpose |
| --- | --- |
| `/next_task` | Find next work by scanning epic/stage hierarchy |
| `/epic-stats` | Calculate progress across epics |
| `/code-review-graph:build-graph` | Build/rebuild the local code review graph |
| `/code-review-graph:review-delta` | Review changes since last commit |
| `/code-review-graph:review-pr` | Full PR review with blast-radius analysis |

## Stage Tracking Documents

### Location

```
epics/EPIC-XXX-name/STAGE-XXX-YYY.md
```

### Status Values

- `Not Started` — Work not yet begun
- `Design` — In design phase
- `Build` — In build phase
- `Refinement` — In refinement phase
- `Finalize` — In finalize phase
- `Complete` — All phases done
- `Skipped` — Intentionally skipped

## Repo conventions

- **Monorepo** with `apps/monitor` (Python/FastAPI), `apps/ui` (React/Vite), `packages/` (shared types, plugin SDK), `deploy/` (compose, grafana dashboards-as-code, vmalert rules, vector config), `runbooks/` (built-in runbooks). The host-specific override repo (`homelab-monitor-overrides`) is separate, gitignored from this public repo, and mounted as a volume into the running container.
- **Strict typing** — `pyright --strict` and TypeScript strict; no `Any` without a written exception in code review.
- **Verify** — `make verify` (or `scripts/verify`) is the canonical check: ruff + black + pyright + pytest (with 100% kernel coverage gate) + tsc + vitest + UI build smoke + (optional) integration + (optional) Playwright. Pre-commit runs the fast subset.
- **No `git add -A` ever** — always specific file paths. The doc-updater subagent and changelog flow rely on this.
- **All internal timestamps are UTC.** Display layer converts to `America/New_York` (configurable).
- **Plugins observe themselves** — `homelab_collector_run_*` metrics are mandatory.
- **Open-source-safe defaults** — generic public release defaults to A behavior on existing user scripts (observe, no edits). Host-specific overrides in the separate repo can be more aggressive.
- **`nginx-configuator` is the actual directory name** at `/storage/programs/nginx-configuator/` (sic — not "configurator"). Do not "fix" the spelling.

## Code Review Graph (CRG)

Installed via:

```bash
pip install code-review-graph
code-review-graph install     # auto-configures Claude Code MCP
code-review-graph build       # initial graph
```

The `crg-daemon` watches the repo and auto-rebuilds the graph as files change. Use the slash commands above during Build / Refinement / Finalize phases. `.code-review-graph/` is gitignored.

## Memory references

These project memories must be re-read at the start of any session that touches the affected concerns:

- `reference_homelab_inventory.md` — hardware, services, HA token+URL, Unifi gear, TLS setup
- `reference_docker_inventory.md` — active and disabled compose services
- `project_autofix_safety_model.md` — non-negotiable rules for the Claude auto-fix subsystem
- `project_repo_tooling.md` — monorepo, CRG, epic-stage-workflow choices
