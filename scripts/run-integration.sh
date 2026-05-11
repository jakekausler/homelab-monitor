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

# Pre-seed alertmanager.yml so AM can start before monitor's bootstrap CMD runs.
# Without this, AM and monitor deadlock: monitor depends_on AM healthy, AM
# needs the file (rendered by monitor) to load. The pre-seed file is a
# valid-but-empty AM config; monitor's render_config atomically overwrites it
# with the real config (containing the bearer token) during lifespan startup.
#
# Use docker for the cp so it runs as root and can overwrite any leftover
# file from a previous run (which would be root-owned from container writes).
docker run --rm \
  -v "$(pwd)/deploy/compose/test-fixtures/am-config-seed:/seed" \
  alpine:latest \
  sh -c "cp /seed/alertmanager.bootstrap.yml /seed/alertmanager.yml && chown 1000:2000 /seed/alertmanager.yml && chmod 640 /seed/alertmanager.yml && chown 1000:2000 /seed && chmod 2775 /seed"

docker compose -f "${COMPOSE_FILE}" up \
  --build \
  --abort-on-container-exit \
  --exit-code-from integration-tests
