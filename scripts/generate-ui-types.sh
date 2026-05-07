#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCHEMA_IN="$REPO_ROOT/packages/shared-types/openapi.json"
SCHEMA_OUT="$REPO_ROOT/apps/ui/src/api/schema.ts"

if [ ! -f "$SCHEMA_IN" ]; then
  echo "generate-ui-types: $SCHEMA_IN missing — run scripts/export-openapi.sh first" >&2
  exit 1
fi

mkdir -p "$(dirname "$SCHEMA_OUT")"

cd "$REPO_ROOT/apps/ui"
pnpm exec openapi-typescript "$SCHEMA_IN" -o "$SCHEMA_OUT"
