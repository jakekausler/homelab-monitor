# Getting Started — homelab-monitor

Developer onboarding for first-time contributors.

---

## Prerequisites

| Tool | Version | Notes |
|------|---------|-------|
| Python | 3.12+ | Pinned to 3.12.8 via `.python-version` |
| [uv](https://docs.astral.sh/uv/getting-started/installation/) | latest | Workspace manager and task runner |
| Node.js | 20+ | Required for the React UI (`apps/ui/`) |
| [pnpm](https://pnpm.io/installation) | 9+ | Package manager for the frontend workspace |
| git | any | Pre-commit hooks run on every commit |

Install uv:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Install pnpm (via Corepack, bundled with Node 20+):

```bash
corepack enable
corepack prepare pnpm@latest --activate
```

---

## First-time setup

```bash
git clone <repo-url> homelab-monitor
cd homelab-monitor
make setup
```

`make setup` does the following:

```makefile
uv sync --directory apps/monitor --all-extras       # installs all Python deps including dev
uv run --directory apps/monitor pre-commit install  # wires hooks into .git/hooks
pnpm install                                        # installs frontend deps under apps/ui/
# if crg-daemon is on PATH: registers repo + starts daemon (idempotent)
```

**Optional — Code Review Graph (once per clone):**

```bash
make crg-init
```

This installs `code-review-graph` as an isolated `uv` tool, runs `code-review-graph install`
(auto-configures the Claude Code MCP integration), builds the initial graph, and starts
`crg-daemon` so the graph auto-updates on every file edit and git commit. CRG is not
required — `make verify` and CI work without it.

The virtual environment is created at `apps/monitor/.venv`. The workspace root
`pyproject.toml` declares `apps/monitor` as the single workspace member; `uv`
resolves the full dependency graph from there.

---

## The verify pipeline

```bash
make verify        # runs: backend checks → frontend checks
# or equivalently:
scripts/verify
```

`make verify` is the canonical gate. CI runs the same command. All steps must
pass before a commit merges. The pipeline has two legs:

**Backend** (`apps/monitor/`):

| Step | Command | What it checks |
|------|---------|---------------|
| `lint` | `ruff check .` | Style, imports, anti-patterns (E, F, I, B, UP, ANN, SIM, PL, RUF) |
| `format-check` | `ruff format --check .` | Formatting without modifying files |
| `typecheck` | `pyright` | Strict mode (`typeCheckingMode: strict`) across `homelab_monitor/` and `tests/` |
| `test` | `pytest --cov=homelab_monitor --cov-report=term-missing` | Unit tests with **100% branch coverage** gate |

The 100% coverage gate is enforced via `fail_under = 100` in `[tool.coverage.report]`.
`homelab_monitor/cli/__main__.py` is excluded from coverage measurement (entry-point shim).

**Frontend** (`apps/ui/`) — run via `make ui-verify`:

| Step | Command | What it checks |
|------|---------|---------------|
| `lint` | `pnpm eslint .` | ESLint flat config (TypeScript strict rules) |
| `format-check` | `pnpm prettier --check .` | Prettier (`semi: false`, single quotes) |
| `typecheck` | `pnpm tsc -b` | TypeScript strict mode, no emitted output |
| `test` | `pnpm vitest run --coverage` | Vitest unit tests with **100% coverage** gate |
| `build` | `pnpm vite build` | Production Vite build (verifies bundle succeeds) |

`src/main.tsx` is excluded from the frontend coverage gate (entry-point shim, same rationale as the backend).

---

## Day-to-day workflow

Before every commit:

```bash
make verify
```

If you have formatting violations, fix them automatically:

```bash
make format        # runs ruff format in-place
```

Then re-run `make verify` to confirm the pipeline is green before committing.

Pre-commit hooks run `ruff` (with `--fix`) and `ruff-format` on staged files,
plus YAML/TOML validation and whitespace checks. If a hook rewrites a file,
stage the changes and commit again.

When CI fails on a branch, run `make verify` locally — the output is identical
to CI. Start with the first failing step; later steps are likely noise.

---

## Running specific subsets

**Backend:**

```bash
make lint          # ruff check only
make format-check  # formatting check only (no writes)
make typecheck     # pyright only
make test          # pytest + coverage only
```

Verbose pytest output with test names:

```bash
uv run --directory apps/monitor pytest -v
```

Run a single test file:

```bash
uv run --directory apps/monitor pytest tests/test_smoke.py -v
```

Run without coverage (faster iteration):

```bash
uv run --directory apps/monitor pytest --no-cov -v
```

**Frontend:**

```bash
make ui-dev        # start Vite dev server at port 5173 (proxies /api → localhost:9090)
make ui-build      # production Vite build
make ui-test       # Vitest in run mode (with coverage)
make ui-verify     # full frontend gate: ESLint + Prettier check + tsc -b + Vitest + Vite build
```

You can also invoke scripts directly via pnpm:

```bash
pnpm --filter ui run dev       # same as make ui-dev
pnpm --filter ui run test      # same as make ui-test
pnpm --filter ui run lint      # ESLint only
pnpm --filter ui run typecheck # tsc -b only
```

---

## The `hm` CLI

The package exposes a `hm` entry point:

```bash
uv run --directory apps/monitor hm
```

Currently it prints the package version. Subcommands are added in later stages.
The entry point is defined in `apps/monitor/pyproject.toml`:

```toml
[project.scripts]
hm = "homelab_monitor.cli.main:main"
```

---

## Dev server

**Frontend:**

```bash
make ui-dev
```

Starts Vite at port 5173 (auto-selects the next available port if 5173 is taken).
All requests to `/api/*` are proxied to `http://localhost:9090` (the FastAPI backend).
Dark theme is the default per spec §9.3.

**Backend:**

```bash
make dev
```

This currently prints a stub message. The real FastAPI dev server lands in
**STAGE-001-010** (FastAPI app shell).

---

## Where things will go

For the planned repository layout, dependency graph, and service architecture,
see:

```
docs/superpowers/specs/2026-05-04-homelab-monitor-design.md  §11 repo layout
```

---

## Troubleshooting

**"Failed to spawn: ruff" or similar hook errors on commit**

You skipped `make setup`. Run it now:

```bash
make setup
```

**Pre-commit hooks not running on commit**

The hooks are not installed in `.git/hooks`. Fix:

```bash
make setup
# or directly:
uv run --directory apps/monitor pre-commit install
```

**Coverage failing under 100%**

Check the `omit` list in `apps/monitor/pyproject.toml`:

```toml
[tool.coverage.run]
omit = ["homelab_monitor/cli/__main__.py"]
```

If you added a new entry-point shim or an untestable file, add it to `omit`.
Otherwise, write the missing tests — the gate is intentionally strict.

**`make dev` prints a stub message**

Expected. The dev server is not yet implemented. See STAGE-001-010.

**`crg-daemon` not starting after `make setup`**

`make setup` only starts the daemon if `crg-daemon` is already on PATH. If you see
the "Tip: run 'make crg-init'" message, run:

```bash
make crg-init
```

This is a one-time step per clone. After it completes, subsequent `make setup` runs
will register and start the daemon automatically.

**`pnpm not found` after cloning**

pnpm is not installed or Corepack has not activated it. Fix:

```bash
corepack enable
corepack prepare pnpm@latest --activate
```

Alternatively, install pnpm directly via the standalone installer:

```bash
curl -fsSL https://get.pnpm.io/install.sh | sh -
```

Then re-run `make setup`.

**Vite dev server bound to a different port**

If port 5173 is already in use, Vite automatically selects the next available
port and logs the actual URL on startup. Check the terminal output for the
bound address. To pin a specific port, pass `--port` explicitly:

```bash
pnpm --filter ui run dev -- --port 5174
```

**`make clean` — when to use it**

Removes `__pycache__`, `.pytest_cache`, `.ruff_cache`, `.pyright`, and
`.coverage` artifacts. Use it if you see stale cache behavior. Does not
remove `.venv` or `node_modules`.
