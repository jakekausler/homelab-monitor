#!/usr/bin/env bash
#
# validate-vector-template.sh
#
# CI/host gate: render deploy/vector/vector.toml.template with dummy values
# and run `vector validate` against the rendered output. Catches VRL errors
# (e.g. E651) in the template automatically, without booting the full
# integration stack.
#
# Render logic MIRRORS apps/monitor/tests/integration/test_vector_template_validate.py
# ::_render_template() EXACTLY. Any divergence defeats the purpose of this gate.
#
# Wired into:
#   - .github/workflows/ci.yml  (integration job, before run-integration.sh)
#   - make validate-vector-template  (called from make verify-ci)
#
set -euo pipefail

# Vector image tag. MUST match deploy/compose/docker-compose.yml (service
# `vector`) and deploy/compose/docker-compose.test.yml. If you bump the image
# there, bump it here too.
VECTOR_IMAGE="timberio/vector:0.41.1-debian"

# Resolve repo root from this script's location (script lives in <repo>/scripts).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

TEMPLATE_PATH="$REPO_ROOT/deploy/vector/vector.toml.template"

if [[ ! -f "$TEMPLATE_PATH" ]]; then
  echo "FAIL: template not found at $TEMPLATE_PATH" >&2
  exit 1
fi

# Temp file for the rendered config; cleaned up on any exit.
RENDERED="$(mktemp /tmp/vector-validate.XXXXXX.toml)"
cleanup() { rm -f "$RENDERED"; }
trap cleanup EXIT

echo "==> Rendering $TEMPLATE_PATH with dummy values -> $RENDERED"

# Render via the production redact helpers + the test's exact dummy values.
# stdin is the Python program; argv[1] = template path, argv[2] = output path.
uv run --directory "$REPO_ROOT/apps/monitor" python - "$TEMPLATE_PATH" "$RENDERED" <<'PYEOF'
import sys
from pathlib import Path

from homelab_monitor.kernel.config import DEFAULT_REDACT_PATTERNS
from homelab_monitor.kernel.cron.render import (
    build_redact_metric_entries,
    build_redact_strip_markers,
    build_redact_vrl,
)

template_path = Path(sys.argv[1])
output_path = Path(sys.argv[2])

text = template_path.read_text(encoding="utf-8")

pats = list(DEFAULT_REDACT_PATTERNS)

# These nine substitutions mirror
# tests/integration/test_vector_template_validate.py::_render_template() EXACTLY.
# TODO: these substitutions are duplicated in tests/integration/test_vector_template_validate.py
# and tests/test_vector_template.py; if a 4th consumer appears, extract a shared render helper.
text = text.replace("${CRON_EVENTS_INGEST_TOKEN}", "dummy-token-for-test")
text = text.replace("${VECTOR_DOCKER_EXCLUDE}", "[]")
text = text.replace("${VECTOR_REDACT_TRANSFORMS}", build_redact_vrl(pats))
text = text.replace("${VECTOR_REDACT_STRIP_MARKERS}", build_redact_strip_markers(pats))
text = text.replace("${VECTOR_REDACT_METRICS}", build_redact_metric_entries(pats))
text = text.replace("${HOMELAB_MONITOR_LOG_JSON_MAX_DEPTH}", "8")
text = text.replace("${HOMELAB_MONITOR_LOG_JSON_MAX_FIELDS}", "100")
text = text.replace("${HM_SYNOLOGY_SYSLOG_BIND_HOST}", "0.0.0.0")
text = text.replace("${HM_SYNOLOGY_SYSLOG_PORT}", "5515")

output_path.write_text(text, encoding="utf-8")
PYEOF

echo "==> Validating rendered config with $VECTOR_IMAGE"

if docker run --rm -v "$RENDERED:/validate.toml:ro" "$VECTOR_IMAGE" validate /validate.toml; then
  echo "PASS: vector validate succeeded on rendered template."
else
  echo "FAIL: vector validate rejected the rendered template (see output above)." >&2
  exit 1
fi
