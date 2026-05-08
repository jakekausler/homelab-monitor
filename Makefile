.PHONY: setup verify verify-ci lint format format-check typecheck test test-fast test-nocov dev backend-dev openapi-export clean crg-init ui-verify ui-dev ui-build ui-test _verify-parallel

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

verify: lint format-check typecheck _verify-parallel

.PHONY: _verify-parallel
_verify-parallel:
	@echo "Running backend tests and UI verify in parallel..."
	@bash -c '\
	  ($(MAKE) test 2>&1 | sed "s/^/[backend] /"; exit $${PIPESTATUS[0]}) & p1=$$!; \
	  ($(MAKE) ui-verify 2>&1 | sed "s/^/[ui] /"; exit $${PIPESTATUS[0]}) & p2=$$!; \
	  wait $$p1; r1=$$?; \
	  wait $$p2; r2=$$?; \
	  exit $$((r1 + r2))'

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

.PHONY: test-fast
test-fast:
	uv run --directory $(MONITOR_DIR) pytest -m "not slow" --no-cov

.PHONY: test-nocov
test-nocov:
	uv run --directory $(MONITOR_DIR) pytest --no-cov

backend-dev:
	uv run --directory $(MONITOR_DIR) uvicorn homelab_monitor.kernel.api.app:create_app \
		--factory --reload --host 0.0.0.0 --port 9090 \
		--reload-dir $(MONITOR_DIR)/homelab_monitor

openapi-export:
	bash scripts/export-openapi.sh

dev: backend-dev

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
	find . -path './.venv*' -prune -o \( -name '.coverage*' -type f \) -exec rm -f {} +
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
	@command -v code-review-graph >/dev/null 2>&1 || { echo "ERROR: code-review-graph missing — run 'make crg-init'"; exit 1; }
	uv tool run code-review-graph build
