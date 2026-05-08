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
- **Verify** — `make verify` (or `scripts/verify`) is the canonical check: ruff + black + pyright + pytest (with 100% kernel coverage gate) + tsc + vitest + UI build smoke + (optional) integration + (optional) Playwright. Pre-commit runs the fast subset. Run `make verify-ci` before pushing to simulate the full CI pipeline locally (backend + frontend + CRG build). For LOCAL iteration loops (Build phase, mid-stage fix waves), prefer `make test-fast` (skips `@pytest.mark.slow` e2e tests + skips coverage instrumentation; ~50-70s vs ~200s) or `make test-nocov` (full suite, no coverage; ~150s). `make verify` is REQUIRED at stage Finalize and before any commit that wraps a phase.
- **No `git add -A` ever** — always specific file paths. The doc-updater subagent and changelog flow rely on this.
- **All internal timestamps are UTC.** Display layer converts to `America/New_York` (configurable).
- **Plugins observe themselves** — `homelab_collector_run_*` metrics are mandatory.
- **Open-source-safe defaults** — generic public release defaults to A behavior on existing user scripts (observe, no edits). Host-specific overrides in the separate repo can be more aggressive.
- **`nginx-configuator` is the actual directory name** at `/storage/programs/nginx-configuator/` (sic — not "configurator"). Do not "fix" the spelling.
- **`uv run` working directory** — `uv run --directory apps/monitor <cmd>` runs the command WITH cwd set to `apps/monitor`, so any path arguments must be relative to that directory (e.g., `tests/test_db_migrations.py`, NOT `apps/monitor/tests/test_db_migrations.py`). The Makefile and pre-commit hooks both use this `--directory` form. When invoking `uv run` outside `make`, either match the same pattern or `cd apps/monitor && uv run <cmd>` (without `--directory`) and use repo-relative paths.
- **Always operate from `/storage/programs/homelab-monitor`.** The bash tool's cwd persists across calls but can be implicitly reset by other tools acting on absolute paths.
  - **Recovery rule (NON-NEGOTIABLE)**: When you find yourself in a different directory (e.g., `make verify` exits with `make: *** No rule to make target 'verify'`), recover by running `cd /storage/programs/homelab-monitor` AS A STAND-ALONE BASH CALL, with NO other command chained after `&&` or `;`. ONE bash call. ONE command. Then run your next command in a SEPARATE bash call. Do NOT combine `cd` with anything else.
  - WRONG: `cd /storage/programs/homelab-monitor && make verify` — this chains and is forbidden as a recovery.
  - RIGHT (recovery): `cd /storage/programs/homelab-monitor` (call 1, alone), then `make verify` (call 2, alone).
  - For commands that NEED to run from a subdir (e.g., `cd apps/ui && pnpm exec prettier --write .`), chaining `cd subdir && <cmd>` IS allowed because the chain executes in a single shell that can return to the parent dir naturally. The forbidden pattern is specifically chaining `cd /storage/programs/homelab-monitor` (recovering to root) with anything else.
  - Never `cd` deeper than the repo root for a duration spanning multiple bash calls. The cwd you leave behind persists, and the next call may find itself in the wrong place.

## Code Review Graph (CRG)

Installed via:

```bash
make crg-init
```

Which runs:
```bash
uv tool install code-review-graph   # isolated install, not in project venv
code-review-graph install           # auto-configures Claude Code MCP
code-review-graph build             # initial graph
crg-daemon add /storage/programs/homelab-monitor
crg-daemon start
```

The `crg-daemon` watches the repo and auto-rebuilds the graph as files change. Use the slash commands above during Build / Refinement / Finalize phases. `.code-review-graph/` is gitignored.

## Memory references

These project memories must be re-read at the start of any session that touches the affected concerns:

- `reference_homelab_inventory.md` — hardware, services, HA token+URL, Unifi gear, TLS setup
- `reference_docker_inventory.md` — active and disabled compose services
- `project_autofix_safety_model.md` — non-negotiable rules for the Claude auto-fix subsystem
- `project_repo_tooling.md` — monorepo, CRG, epic-stage-workflow choices
