# Getting Started — homelab-monitor

Developer onboarding for first-time contributors.

---

## Prerequisites

| Tool | Version | Notes |
|------|---------|-------|
| Python | 3.12+ | Pinned to 3.12.8 via `.python-version` |
| [uv](https://docs.astral.sh/uv/getting-started/installation/) | latest | Workspace manager and task runner |
| git | any | Pre-commit hooks run on every commit |

Install uv:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

---

## First-time setup

```bash
git clone <repo-url> homelab-monitor
cd homelab-monitor
make setup
```

`make setup` does two things:

```makefile
uv sync --directory apps/monitor --all-extras   # installs all deps including dev
uv run --directory apps/monitor pre-commit install  # wires hooks into .git/hooks
```

The virtual environment is created at `apps/monitor/.venv`. The workspace root
`pyproject.toml` declares `apps/monitor` as the single workspace member; `uv`
resolves the full dependency graph from there.

---

## The verify pipeline

```bash
make verify        # runs: lint → format-check → typecheck → test
# or equivalently:
scripts/verify
```

`make verify` is the canonical gate. CI runs the same command. All four steps
must pass before a commit merges.

| Step | Command | What it checks |
|------|---------|---------------|
| `lint` | `ruff check .` | Style, imports, anti-patterns (E, F, I, B, UP, ANN, SIM, PL, RUF) |
| `format-check` | `ruff format --check .` | Formatting without modifying files |
| `typecheck` | `pyright` | Strict mode (`typeCheckingMode: strict`) across `homelab_monitor/` and `tests/` |
| `test` | `pytest --cov=homelab_monitor --cov-report=term-missing` | Unit tests with **100% branch coverage** gate |

The 100% coverage gate is enforced via `fail_under = 100` in `[tool.coverage.report]`.
`homelab_monitor/cli/__main__.py` is excluded from coverage measurement (entry-point shim).

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

**`make clean` — when to use it**

Removes `__pycache__`, `.pytest_cache`, `.ruff_cache`, `.pyright`, and
`.coverage` artifacts. Use it if you see stale cache behavior. Does not
remove `.venv`.
