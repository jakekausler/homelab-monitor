#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="$REPO_ROOT/packages/shared-types/openapi.json"
mkdir -p "$(dirname "$OUT")"

TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT

uv run --directory apps/monitor python -c "
import json, sys
from homelab_monitor.kernel.api.app import create_app
app = create_app(lifespan_enabled=False)
sys.stdout.write(json.dumps(app.openapi(), indent=2, sort_keys=True))
sys.stdout.write('\n')
" > "$TMP"

if [ ! -f "$OUT" ] || ! cmp -s "$TMP" "$OUT"; then
  mv "$TMP" "$OUT"
  echo "openapi-export: regenerated $OUT — stage and re-commit." >&2
  exit 1
fi
exit 0
