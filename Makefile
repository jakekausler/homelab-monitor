.PHONY: setup verify lint format format-check typecheck test dev clean crg-init

.DEFAULT_GOAL := verify

MONITOR_DIR := apps/monitor

setup:
	uv sync --directory apps/monitor --all-extras
	uv run --directory apps/monitor pre-commit install
	@echo "Setup complete — workspace .venv is ready."

verify: lint format-check typecheck test

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

clean:
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
	find . -type d -name ".pytest_cache" -exec rm -rf {} +
	find . -type d -name ".ruff_cache" -exec rm -rf {} +
	find . -type d -name ".pyright" -exec rm -rf {} +
	find . -path './.venv*' -prune -o \( -name '.coverage' -type f \) -exec rm -f {} +
	find . -path './.venv*' -prune -o \( -type d -name 'htmlcov' \) -exec rm -rf {} +

crg-init:
	@echo "Run: pip install code-review-graph && code-review-graph install && code-review-graph build"
