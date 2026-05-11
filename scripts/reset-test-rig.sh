#!/usr/bin/env bash
# Reset the integration test rig: tear down volumes, rebuild all images.
#
# Use cases:
#   - After pulling new commits that change the test rig topology
#   - When images get into a weird state (corrupted layer cache, etc.)
#   - As the "nuke and pave" first step in a flaky test investigation
#
# Does NOT bring the rig back up -- run scripts/run-integration.sh or
# `make integration` for that.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="${REPO_ROOT}/deploy/compose/docker-compose.test.yml"

cd "${REPO_ROOT}"

echo "[reset-test-rig] tearing down volumes + orphan containers..."
docker compose -f "${COMPOSE_FILE}" down -v --remove-orphans

echo "[reset-test-rig] rebuilding all images (--no-cache)..."
docker compose -f "${COMPOSE_FILE}" build --no-cache

echo "[reset-test-rig] done. Run scripts/run-integration.sh or 'make integration' to bring up."
