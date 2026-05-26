#!/usr/bin/env bash
# generate-compose-override.sh
# Reads build-sources.yaml and writes deploy/compose/docker-compose.override.yml.
# Usage: scripts/generate-compose-override.sh [path-to-build-sources.yaml]
#
# Default source: /var/lib/homelab-monitor/overrides/docker/build-sources.yaml
# Override via:   BUILD_SOURCES_PATH env var
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

SRC="${BUILD_SOURCES_PATH:-/var/lib/homelab-monitor/overrides/docker/build-sources.yaml}"
OUT="${OUT_OVERRIDE:-${REPO_ROOT}/deploy/compose/docker-compose.override.yml}"

if [[ ! -f "$SRC" ]]; then
  echo "ERROR: build-sources.yaml not found at $SRC" >&2
  exit 1
fi

# Extract unique host_prefix values (lines matching "  host_prefix: /...")
mapfile -t PREFIXES < <(grep 'host_prefix:' "$SRC" | sed 's/.*host_prefix:\s*//' | sed 's/\s*$//' | sort -u)

if [[ ${#PREFIXES[@]} -eq 0 ]]; then
  echo "WARNING: no build_context_roots found in $SRC — writing empty override." >&2
fi

{
  echo "# auto-generated from build-sources.yaml — DO NOT EDIT"
  echo "# source: $SRC"
  echo "# generated: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "services:"
  echo "  monitor:"
  echo "    volumes:"
  if [[ ${#PREFIXES[@]} -eq 0 ]]; then
    echo "      [] # no build_context_roots defined"
  else
    for prefix in "${PREFIXES[@]}"; do
      echo "      - ${prefix}:${prefix}:ro"
    done
  fi
} > "$OUT"

echo "Written: $OUT"
echo "Entries: ${#PREFIXES[@]}"
