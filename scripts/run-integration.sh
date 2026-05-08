#!/usr/bin/env bash
# Run the integration-tests compose stack to completion.
#
# Modes:
#   CI / one-shot:
#     bash scripts/run-integration.sh
#       Builds the test image, brings up VM + integration-tests, exits with
#       the integration-tests container's exit code.
#
#   Local dev iteration:
#     docker compose -f deploy/compose/docker-compose.test.yml up -d victoriametrics
#     VM_URL=http://localhost:8428 cd apps/monitor && uv run pytest tests/integration/
#       Bring up only VM, run host pytest pointed at the local VM port.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="${REPO_ROOT}/deploy/compose/docker-compose.test.yml"

cd "${REPO_ROOT}"

docker compose -f "${COMPOSE_FILE}" up \
  --build \
  --abort-on-container-exit \
  --exit-code-from integration-tests
