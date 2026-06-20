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
3. **REFINEMENT** — Validate against PRODUCTION reality. Nearly everything in this project needs real-host
   validation, so Refinement is a single production-focused pass (no separate dev-rig sub-phase):
   - Validate against the full `docker compose up -d` production stack with REAL host data (real
     /etc/crontab, real /var/spool/cron/crontabs/*, real network probes, real services like the live UDM /
     Synology / Pi-hole), including any host setup scripts (e.g., `scripts/host-setup.sh`). Confirms the
     deployment surface actually works against this host's reality.
   - For **frontend** stages, this includes dual sign-off (Desktop AND Mobile) against the production stack.
     For **backend / CLI / host-integration** stages, validate the real integration end-to-end (real data,
     real targets, real error paths) — not just a re-run of Build's unit tests.
   - The prod rig stays up between stages/epics (it's the user's real monitor — do NOT tear it down). If a
     genuinely fast synthetic/dev-rig spot-check helps mid-iteration, it's optional and informal; the
     Refinement GATE is the production validation above.
4. **FINALIZE** — Tests, code review, docs, commit (all via subagents)

## Commands

| Command | Purpose |
| --- | --- |
| `/next_task` | Find next work by scanning epic/stage hierarchy |
| `/epic-stats` | Calculate progress across epics |
| `/code-review-graph:build-graph` | Build/rebuild the local code review graph |
| `/code-review-graph:review-delta` | Review changes since last commit |
| `/code-review-graph:review-pr` | Full PR review with blast-radius analysis |

## Local Refinement / dev environment

Frontend stage Refinement requires a logged-in browser session against a running backend with all sidecars (Karma, Grafana, vmalert) up. As of STAGE-001-021 Spec B, this is one command:

```bash
# First time only — script will copy deploy/dev/dev.env.example to dev.env
# and abort with a "generate master key" message.
make dev

# Generate the master key and paste it into deploy/dev/dev.env:
python3 -c "import os, base64; print(base64.b64encode(os.urandom(32)).decode())"

# Re-run — brings up sidecars + host backend (port 19090) + host UI (port 5180).
make dev
```

Login defaults: `admin` / `admin-dev-password` (override via `HM_DEV_ADMIN_*` in `deploy/dev/dev.env`).

| Command | What it does |
| --- | --- |
| `make dev` | Hybrid: docker sidecars + host backend + host UI dev server. Use this for daily coding. |
| `make dev-clean` | Tear down everything (incl. volumes), then `make dev`. Use for fresh-DB scenarios. |
| `make dev-prod` | Full prod compose stack (monitor built from local Dockerfile). Use to validate Dockerfile / alembic / compose changes. |
| `make dev-down` | Stop everything, preserve volumes. Use when done for the day. |
| `make generate-build-mounts` | Regenerate compose volume mounts for local-build contexts. Re-run after editing build-sources.yaml. |

Full operator guide: `docs/dev/local-environment.md` (port map, troubleshooting, sidecar visibility, master-key rotation).

### Port Map (dev vs prod)

Dev and prod published host ports differ so both can coexist on the same host.
All host bindings are `127.0.0.1`-only (isolated to localhost).

| Service              | Dev host (1xxxx) | Prod host (2xxxx) | Container | Notes |
| -------------------- | ---------------- | ----------------- | --------- | ----- |
| monitor backend      | 19090            | 29090             | 9090      | Host :9090 is bound by an unrelated process. Bind host configurable via `HOMELAB_MONITOR_BIND_HOST` (default `127.0.0.1`). Set to `0.0.0.0` for LAN access in trusted homelab networks. |
| UI dev server (Vite) | 5180             | n/a               | n/a       | Prod serves built UI from the monitor container. |
| VictoriaMetrics      | 18428            | container-internal | 8428    | Dev publishes for hybrid-mode backend. Prod reaches it via `victoriametrics:8428`. |
| VictoriaLogs         | 19428            | container-internal | 9428    | |
| vmagent              | 18429            | container-internal | 8429    | |
| vector               | 18686            | container-internal | 8686    | |
| Alertmanager         | 19093            | container-internal | 9093    | |
| Karma                | 18080            | container-internal | 8080    | Host :8080 is bound by pihole-unbound. Never publish Karma to :8080. |
| vmalert (metrics)    | 18880            | container-internal | 8880    | |
| vmalert (logs)       | 18881            | container-internal | 8880    | Two containers, distinct host ports, same container port. |
| Grafana              | 13000            | container-internal | 3000    | Host :3000 is collision-magnet. Never publish Grafana to :3000. |
| UDM syslog (vector)  | 15514            | 5514 (0.0.0.0)     | 5514    | DELIBERATE EXCEPTION: a LAN syslog source (the UDM Pro) cannot reach a container-internal sidecar, so this is the ONE prod sidecar port host-published, and on `0.0.0.0` (not `127.0.0.1`) by necessity. UDP. Dev binds `127.0.0.1` only (no real UDM traffic in dev). |

**Invariant:** Dev published ports start with `1xxxx`. Prod publishes only the monitor backend on `2xxxx` (29090). All other sidecars are container-internal in prod and proxied via the monitor's `/api/<sidecar>/` endpoints.

**Documented exception (STAGE-007-016):** the UDM syslog listener (vector, UDP `5514`) is the sole prod sidecar port host-published, bound `0.0.0.0` not `127.0.0.1`, because an off-host LAN syslog source cannot reach a container-internal sidecar. Its prod port (`5514`) does not follow the `2xxxx` convention because it must match the syslog default the UDM emits to.

`make dev-prod` (full prod compose stack on this dev host) uses the same `2xxxx` mappings as production.

**Common gotchas (still relevant when debugging):**
- Host port 9090 is bound by an unrelated process. The dev rig uses 19090 (`HM_DEV_BACKEND_PORT` in `dev.env`); the prod compose stack uses 29090 (`HOMELAB_MONITOR_PORT` in `deploy/compose/.env`). Never use 9090 on this host.
- Vite proxy env var is `VITE_API_PROXY_TARGET`, NOT `API_PROXY_TARGET`. Wrong var → API calls return HTML (vite SPA fallback) → React error #31 ("object with keys {code, message, details}"). The dev-up script sets this correctly.
- `hm user create` requires interactive password input. Pipe via `printf 'pw\npw\n' | uv run hm user create <name>`. Min password length: 12 chars. `dev-up.sh` handles this automatically.
- `deploy/dev/dev.env` contains the master key. The script enforces `chmod 600` on every run; the file is gitignored.

### Manual fallback (when `make dev` fails)

If the script breaks in a new way, fall back to the pre-Spec-B manual pattern. This is the same recipe that powered STAGE-019 Refinement:

```bash
# 1. Generate a master key + write env file
mkdir -p /tmp/hm-refine
cat > /tmp/hm-refine/.env <<EOF
HOMELAB_MONITOR_DB_URL=sqlite+aiosqlite:////tmp/hm-refine/homelab.db
HOMELAB_MONITOR_MASTER_KEY=$(python3 -c "import os, base64; print(base64.b64encode(os.urandom(32)).decode())")
HOMELAB_MONITOR_HTTPS_ONLY_COOKIES=false
HOMELAB_MONITOR_AUTO_MIGRATE=1
EOF
chmod 600 /tmp/hm-refine/.env

# 2. Start backend on port 19090 (port 9090 is bound on this host).
cd apps/monitor && set -a && source /tmp/hm-refine/.env && set +a && \
HOMELAB_MONITOR_BCRYPT_COST=4 nohup uv run uvicorn homelab_monitor.kernel.api.app:create_app \
  --factory --host 127.0.0.1 --port 19090 > /tmp/hm-refine/backend.log 2>&1 &
disown

# 3. Create user (interactive — pipe stdin to skip prompts; ≥12 chars).
HOMELAB_MONITOR_BCRYPT_COST=4 printf 'refinement-test-pw\nrefinement-test-pw\n' \
  | uv run hm user create admin

# 4. Start UI dev server.
cd ../.. && VITE_API_PROXY_TARGET=http://127.0.0.1:19090 \
  pnpm --filter ui run dev > /tmp/hm-refine/ui-dev.log 2>&1 &
```

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
- **Integration tests auto-skip fast when the rig is absent.** Tests under `apps/monitor/tests/integration/` call `require_rig_components(...)` (from `tests/integration/helpers/rig_health.py`), which runs a session-cached 2s health probe per rig component. If a required component is unreachable, the test SKIPS immediately (no 30s `Rig.boot()` block) with a reason naming the down component. To actually RUN them, bring the rig up via `make integration` (full `docker-compose.test.yml` stack). `make verify` and `make test-fast` never run them (deselected by `-m 'not integration'` / `-m "not slow and not integration"`). A full `-m ''` run SKIPS them fast (~14s) instead of blocking (~14min) when no rig is present. Partial rigs are supported: only tests whose required components are down skip; the rest run. Integration tests only FAIL on genuine assertion failures with the rig present — never on rig absence.
- **No `git add -A` ever** — always specific file paths. The doc-updater subagent and changelog flow rely on this.
- **NEVER invoke the `AskUserQuestion` tool.** Ask the user questions as plain text in the response instead — one question at a time when there are several. This is a hard rule.
- **All internal timestamps are UTC.** Display layer converts to `America/New_York` (configurable).
- **Plugins observe themselves** — `homelab_collector_run_*` metrics are mandatory.
- **Open-source-safe defaults** — generic public release defaults to A behavior on existing user scripts (observe, no edits). Host-specific overrides in the separate repo can be more aggressive.
- **`nginx-configuator` is the actual directory name** at `/storage/programs/nginx-configuator/` (sic — not "configurator"). Do not "fix" the spelling.
- **NEVER run `uv run` directly — always use `make uv ARGS="..."`.** This is a hard rule, not a preference. Raw `uv run` bypasses the RTK output filter (see RTK section below) and wastes context tokens. The `make uv` target forwards `ARGS` verbatim to `uv run`, and RTK's hook rewrites it so the filter applies. `ARGS` holds everything that comes *after* `uv run`:
  - Run tests: `make uv ARGS="--directory apps/monitor pytest tests/test_db_migrations.py"`
  - Run ruff: `make uv ARGS="--directory apps/monitor ruff check ."`
  - Any `uv run <x>` becomes `make uv ARGS="<x>"`.
  - **`uv run` working directory** — `--directory apps/monitor` sets cwd to `apps/monitor`, so path arguments must be relative to that directory (e.g., `tests/test_db_migrations.py`, NOT `apps/monitor/tests/test_db_migrations.py`). The Makefile and pre-commit hooks use this `--directory` form.
  - **The only two exceptions** where raw `uv run` is still permitted: (1) long-running backgrounded processes (`nohup uv run uvicorn ...` for the manual-fallback dev backend) and (2) interactive stdin-piped commands (`printf '...' | uv run hm user create ...`). A `make` recipe wrapper is fragile for `nohup`/backgrounding and stdin piping; these stay raw.
- **Always operate from `/storage/programs/homelab-monitor`.** The bash tool's cwd persists across calls but can be implicitly reset by other tools acting on absolute paths.
  - **Recovery rule (NON-NEGOTIABLE)**: When you find yourself in a different directory (e.g., `make verify` exits with `make: *** No rule to make target 'verify'`), recover by running `cd /storage/programs/homelab-monitor` AS A STAND-ALONE BASH CALL, with NO other command chained after `&&` or `;`. ONE bash call. ONE command. Then run your next command in a SEPARATE bash call. Do NOT combine `cd` with anything else.
  - WRONG: `cd /storage/programs/homelab-monitor && make verify` — this chains and is forbidden as a recovery.
  - RIGHT (recovery): `cd /storage/programs/homelab-monitor` (call 1, alone), then `make verify` (call 2, alone).
  - For commands that NEED to run from a subdir (e.g., `cd apps/ui && pnpm exec prettier --write .`), chaining `cd subdir && <cmd>` IS allowed because the chain executes in a single shell that can return to the parent dir naturally. The forbidden pattern is specifically chaining `cd /storage/programs/homelab-monitor` (recovering to root) with anything else.
  - Never `cd` deeper than the repo root for a duration spanning multiple bash calls. The cwd you leave behind persists, and the next call may find itself in the wrong place.
- **Long-running or large-output commands MUST tee output to a log file.** Any command whose output you might need to inspect later — `make verify`, `make integration`, `pytest`, `pnpm test`, `pnpm build`, `bash scripts/run-integration.sh`, `docker compose up`, build commands, log tails, etc. — MUST be run as `<command> 2>&1 | tee /tmp/<descriptive-name>-$(date +%s).log`. Then any subsequent `grep`/`tail`/inspection works against the log file instead of forcing a re-run. Subagents (verifier, tester, e2e-tester, etc.) MUST follow this convention. Rationale: re-running `make verify` to grep for one detail wastes 1-2 minutes per re-run; integration runs waste 5-10 minutes. Naming convention: `/tmp/<command>-<context>-<timestamp>.log` (e.g. `/tmp/make-verify-stage021-1715432100.log`, `/tmp/run-integration-stage021-1715432100.log`).
- **Inspecting log/command output MUST go through a subagent.** When the main agent needs to read, grep, tail, or filter the contents of a tee'd log file (or any large output), it MUST dispatch a subagent (Explore for read-only structured analysis, or general-purpose for more complex extraction) and have the subagent return a condensed report. Direct `cat`/`grep`/`sed`/`awk` from the main agent on large outputs pollutes context with raw text and forces re-reads. Subagents condense findings to ~10-30 lines. Rationale: a 40 KB CI log read directly into main context costs ~10K tokens and forces re-reads on follow-up questions; a subagent extracts the 10 facts you actually need.
  - **Log-inspection subagents are STRICTLY READ-ONLY** with strict scope limits:
    - **Read only. NEVER re-run the source command.** If the log file is missing, truncated, or doesn't contain the expected data, REPORT that as the finding and STOP. The main agent decides whether to re-run.
    - **Report only — no diagnosis, no fix.** If the log shows a failure, quote the failure verbatim and stop. Do NOT investigate root cause, propose code changes, or search the codebase. Diagnosis is a separate phase; conflating report+diagnosis+fix wastes 5-10x the time.
    - **One log, one report.** Read ONE log file (or the specified list), extract the requested facts, return. Do NOT inspect adjacent logs (cycle N-1, N+1) unless explicitly told.
    - **Allowed tools:** Read, Bash (`ls`/`cat`/`grep`/`tail` on log files ONLY).
    - **Forbidden tools:** Bash for re-running source commands (`make verify`, `pytest`, `docker compose`, `npm test`), Edit, Write.
    - **Main agent prompt pattern:** "Read the log at `<path>` and extract VERBATIM: [facts]. If the log is missing or incomplete, report that and stop — do NOT regenerate."
    - **If log is missing:** Log-inspection subagent reports the fact → main agent dispatches SEPARATE bash call to re-run command with tee → separate dispatch to inspect new log. Never bundled.

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

## RTK output filter

`.rtk/filters.toml` ships a project-specialized `[filters.make]` filter for [RTK](https://github.com/rtk-ai/rtk), the token-optimization CLI proxy. It strips deterministic noise from clean `make verify` / `make test*` / `make ui-verify` / `make uv` runs (pytest progress dots + 100%-coverage rows, vitest jsdom spam, pnpm/make chatter) — ~53% byte reduction on a clean run — while preserving 100% of error / failure / warning signal (validated by exhaustive fault injection across ruff, pyright, pytest, coverage, eslint).

- **One-time per checkout:** run `rtk trust` so RTK loads the project-local filter (it is sha256-pinned). **Re-run `rtk trust` after any edit to `.rtk/filters.toml`** — a changed file is untrusted until re-approved.
- The filter only applies when a command runs as `rtk make <target>`. RTK's Claude Code hook auto-rewrites `make verify` → `rtk make verify`, so no behavior change is needed for the `make` targets.
- **All `uv run` invocations MUST go through `make uv ARGS="..."`** (the hook rewrites it to `rtk make uv ...` so the filter applies). Raw `uv run` is forbidden except the two cases listed in the Repo conventions `uv run` rule (backgrounded processes, interactive stdin). Quoting inside `ARGS` is passed verbatim to `/bin/sh`.
- Inline filter tests run via `rtk verify --filter make`.

## Memory references

These project memories must be re-read at the start of any session that touches the affected concerns:

- `reference_homelab_inventory.md` — hardware, services, HA token+URL, Unifi gear, TLS setup
- `reference_docker_inventory.md` — active and disabled compose services
- `project_autofix_safety_model.md` — non-negotiable rules for the Claude auto-fix subsystem
- `project_repo_tooling.md` — monorepo, CRG, epic-stage-workflow choices
