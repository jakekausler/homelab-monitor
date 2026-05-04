# homelab-monitor

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
