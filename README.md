# homelab-monitor

[![CI](https://github.com/jakekausler/homelab-monitor/actions/workflows/ci.yml/badge.svg)](https://github.com/jakekausler/homelab-monitor/actions/workflows/ci.yml)

Self-hosted monitoring service for a personal homelab. Detects issues and
anomalies across containers, hosts, network gear, NAS, ISP, and Home
Assistant — with optional auto-remediation via allow-listed runbooks.

**Status: under active development**

## Requirements

- Python 3.12+ (3.12.8 pinned via `.python-version`)
- [uv](https://docs.astral.sh/uv/) (workspace manager + dependency resolver)
- [code-review-graph](https://pypi.org/project/code-review-graph/) *(optional)* — auto-updates a local code graph on every file edit and commit; install once with `make crg-init`

## Getting started

```bash
make setup      # installs all runtime + dev dependencies into a workspace .venv
make verify     # ruff + pyright + pytest (must be green before any commit)
```

First-clone only (optional but recommended):

```bash
make crg-init   # installs code-review-graph, builds initial graph, starts crg-daemon
```

`make verify` works without CRG installed. `make setup` prints a reminder if CRG is not found.

> Note: `make dev` is a documented stub until the FastAPI app shell lands in STAGE-001-010.

See [design spec](docs/superpowers/specs/2026-05-04-homelab-monitor-design.md)
for architecture decisions and full feature map.

## Pre-commit hooks

```bash
uv run pre-commit install
```

## CI

Every pull request runs three parallel jobs:

| Job | What it checks |
|---|---|
| `backend` | ruff lint + format + pyright strict + pytest (100% coverage gate) |
| `frontend` | eslint + prettier + tsc + vitest (coverage) + vite build |
| `crg-build` | `code-review-graph build` on a clean checkout; uploads graph artifact (7-day retention) |

CodeQL static analysis runs separately on every PR and on a weekly schedule
(`.github/workflows/codeql.yml`).

To simulate CI locally before pushing:

```bash
make verify-ci
```

See [docs/repo-setup.md](docs/repo-setup.md) for the branch protection rules
to apply in the GitHub UI.

## Releases

Pushing a `v*` tag (e.g. `v0.1.0`) triggers `.github/workflows/release.yml`,
which builds multi-arch container images (linux/amd64, linux/arm64) and pushes
them to GHCR. The Dockerfile lands in STAGE-001-015; the workflow is dormant
scaffolding until then. No tags will be created until the release container path is complete (STAGE-001-015).
