.PHONY: setup verify verify-ci lint format format-check typecheck test dev clean crg-init ui-verify ui-dev ui-build ui-test

.DEFAULT_GOAL := verify

MONITOR_DIR := apps/monitor

setup:
	uv sync --directory apps/monitor --all-extras
	uv run --directory apps/monitor pre-commit install
	pnpm install
	@if command -v crg-daemon >/dev/null 2>&1; then \
		crg-daemon add /storage/programs/homelab-monitor 2>/dev/null || true; \
		crg-daemon start 2>/dev/null || true; \
		echo "Setup complete — workspace .venv is ready. CRG daemon registered and started."; \
	else \
		echo "Setup complete — workspace .venv is ready."; \
		echo "  Tip: run 'make crg-init' once to install the Code Review Graph daemon (optional)."; \
	fi

verify: lint format-check typecheck test ui-verify

lint:
	uv run --directory $(MONITOR_DIR) ruff check .

format-check:
	uv run --directory $(MONITOR_DIR) ruff format --check .

format:
	uv run --directory $(MONITOR_DIR) ruff format .

typecheck:
	uv run pyright

test:
	uv run --directory $(MONITOR_DIR) pytest --cov=homelab_monitor --cov-report=term-missing

dev:
	@echo "dev server lands in STAGE-001-010 (FastAPI app shell)"
	@echo "run \`make verify\` for the canonical check pipeline"

ui-verify:
	pnpm --filter ui run verify

ui-dev:
	pnpm --filter ui run dev

ui-build:
	pnpm --filter ui run build

ui-test:
	pnpm --filter ui run test

clean:
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
	find . -type d -name ".pytest_cache" -exec rm -rf {} +
	find . -type d -name ".ruff_cache" -exec rm -rf {} +
	find . -type d -name ".pyright" -exec rm -rf {} +
	find . -path './.venv*' -prune -o \( -name '.coverage' -type f \) -exec rm -f {} +
	find . -path './.venv*' -prune -o \( -type d -name 'htmlcov' \) -exec rm -rf {} +
	find ./apps -path '*/node_modules' -type d -prune -exec rm -rf {} + 2>/dev/null || true
	find ./apps -name 'dist' -type d -prune -exec rm -rf {} + 2>/dev/null || true

crg-init:
	uv tool install --force code-review-graph
	code-review-graph install
	code-review-graph build
	crg-daemon add /storage/programs/homelab-monitor 2>/dev/null || true
	crg-daemon start 2>/dev/null || true
	@echo "CRG installed, graph built, and daemon started. Graph auto-updates on file edits and commits."

verify-ci:
	@echo "Simulating CI: backend + frontend + crg-build"
	$(MAKE) verify
	command -v code-review-graph >/dev/null 2>&1 && code-review-graph build || echo "tip: run 'make crg-init' to enable Code Review Graph"
