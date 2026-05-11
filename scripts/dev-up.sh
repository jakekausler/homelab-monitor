#!/usr/bin/env bash
# homelab-monitor — dev rig launcher (STAGE-001-021 Spec B).
#
# Modes:
#   bash scripts/dev-up.sh                   # hybrid: docker sidecars + host backend/UI
#   bash scripts/dev-up.sh --clean           # kill existing, then hybrid
#   bash scripts/dev-up.sh --prod            # full prod compose stack (validates Dockerfile)
#   bash scripts/dev-up.sh --clean --prod    # kill existing, then prod
#
# Idempotent: re-running without --clean is safe; the script detects
# already-running services and skips them.

set -euo pipefail

# ----------------------------------------------------------------------------
# Locate repo root and source paths.
# ----------------------------------------------------------------------------
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"

DEV_ENV_EXAMPLE="${REPO_ROOT}/deploy/dev/dev.env.example"
DEV_ENV_FILE="${REPO_ROOT}/deploy/dev/dev.env"
COMPOSE_FILE="${REPO_ROOT}/deploy/compose/docker-compose.yml"
LOG_DIR="${REPO_ROOT}/deploy/dev/logs"

# ----------------------------------------------------------------------------
# Flag parsing.
# ----------------------------------------------------------------------------
MODE="hybrid"
DO_CLEAN=0

for arg in "$@"; do
  case "${arg}" in
    --clean) DO_CLEAN=1 ;;
    --prod)  MODE="prod" ;;
    -h|--help)
      sed -n '2,15p' "$0"
      exit 0
      ;;
    *)
      echo "error: unknown flag: ${arg}" >&2
      echo "usage: bash scripts/dev-up.sh [--clean] [--prod]" >&2
      exit 2
      ;;
  esac
done

# ----------------------------------------------------------------------------
# Logging helpers.
# ----------------------------------------------------------------------------
_log()  { printf '\033[1;36m[dev-up]\033[0m %s\n' "$*"; }
_warn() { printf '\033[1;33m[dev-up WARN]\033[0m %s\n' "$*" >&2; }
_die()  { printf '\033[1;31m[dev-up ERROR]\033[0m %s\n' "$*" >&2; exit 1; }

# ----------------------------------------------------------------------------
# load_env: ensure deploy/dev/dev.env exists, then export every var from it.
# ----------------------------------------------------------------------------
load_env() {
  if [[ ! -f "${DEV_ENV_FILE}" ]]; then
    _log "deploy/dev/dev.env not found — copying from dev.env.example"
    cp "${DEV_ENV_EXAMPLE}" "${DEV_ENV_FILE}"
    chmod 600 "${DEV_ENV_FILE}"
    _warn "Edit ${DEV_ENV_FILE} and replace HOMELAB_MONITOR_MASTER_KEY before re-running."
    _warn "Generate a key with:"
    _warn "  python3 -c \"import os, base64; print(base64.b64encode(os.urandom(32)).decode())\""
    exit 1
  fi

  # Tighten perms if previous run left it world-readable.
  chmod 600 "${DEV_ENV_FILE}"

  set -a
  # shellcheck disable=SC1090
  source "${DEV_ENV_FILE}"
  set +a
}

# ----------------------------------------------------------------------------
# assert_master_key_set: refuse to continue with the placeholder key.
# ----------------------------------------------------------------------------
assert_master_key_set() {
  if [[ "${HOMELAB_MONITOR_MASTER_KEY:-}" == "GENERATE_ME_WITH_SCRIPT" ]] \
    || [[ -z "${HOMELAB_MONITOR_MASTER_KEY:-}" ]]; then
    _die "HOMELAB_MONITOR_MASTER_KEY is the placeholder. Generate a real key:
    python3 -c \"import os, base64; print(base64.b64encode(os.urandom(32)).decode())\"
  Then update ${DEV_ENV_FILE}."
  fi
}

# ----------------------------------------------------------------------------
# assert_ports_free: warn (do NOT abort) if any dev port is taken.
# ----------------------------------------------------------------------------
assert_ports_free() {
  local ports=(
    "${HM_DEV_UI_PORT:-5180}"
    "${HM_DEV_BACKEND_PORT:-19090}"
    "${HM_DEV_VM_PORT:-8428}"
    "${HM_DEV_VL_PORT:-9428}"
    "${HM_DEV_AM_PORT:-9093}"
    "${HM_DEV_KARMA_PORT:-8081}"
    "${HM_DEV_GRAFANA_PORT:-3000}"
    "${HM_DEV_VMALERT_METRICS_PORT:-8880}"
    "${HM_DEV_VMALERT_LOGS_PORT:-8881}"
  )
  local detector=""
  if command -v ss >/dev/null 2>&1; then detector="ss"
  elif command -v lsof >/dev/null 2>&1; then detector="lsof"
  else
    _warn "neither ss nor lsof available — skipping port-collision check"
    return 0
  fi

  local taken=()
  for p in "${ports[@]}"; do
    case "${detector}" in
      ss)   ss -ltn "sport = :${p}" 2>/dev/null | grep -q LISTEN && taken+=("${p}") ;;
      lsof) lsof -nP -iTCP:"${p}" -sTCP:LISTEN >/dev/null 2>&1 && taken+=("${p}") ;;
    esac
  done

  if [[ ${#taken[@]} -gt 0 ]]; then
    _warn "ports already in use: ${taken[*]}"
    _warn "  -> dev-up will continue (may fail if THIS rig owns them)."
    _warn "  -> override in deploy/dev/dev.env (see HM_DEV_*_PORT vars)."
  fi
}

# ----------------------------------------------------------------------------
# clean_existing: kill host processes + tear down docker.
# ----------------------------------------------------------------------------
clean_existing() {
  _log "tearing down any existing dev rig..."

  # Kill host-side processes by name. Suppress 'no process found' noise.
  pkill -f "uvicorn homelab_monitor" 2>/dev/null || true
  pkill -f "vite"                    2>/dev/null || true
  pkill -f "pnpm.*dev"               2>/dev/null || true

  # Tear down BOTH the prod and test compose stacks (covers all modes).
  docker compose -f "${COMPOSE_FILE}" down -v 2>/dev/null || true

  # Test compose may also be up if user was running integration tests.
  local test_compose="${REPO_ROOT}/deploy/compose/docker-compose.test.yml"
  if [[ -f "${test_compose}" ]]; then
    docker compose -f "${test_compose}" down -v 2>/dev/null || true
  fi

  _log "clean complete"
}

# ----------------------------------------------------------------------------
# ensure_data_dirs: create the host-side dirs the rig writes into.
# ----------------------------------------------------------------------------
ensure_data_dirs() {
  mkdir -p "${LOG_DIR}"
  mkdir -p /tmp/hm-dev/backup /tmp/hm-dev/runbook-transcripts
}

# ----------------------------------------------------------------------------
# ensure_admin_user: create the dev admin if it does not exist (idempotent).
# ----------------------------------------------------------------------------
ensure_admin_user() {
  local user="${HM_DEV_ADMIN_USERNAME:-admin}"
  local pw="${HM_DEV_ADMIN_PASSWORD:-admin-dev-password}"

  # Pattern from docker-compose.test.yml:310 — pipe `pw\npw\n` to satisfy the
  # interactive prompts; suppress duplicate-user errors.
  printf '%s\n%s\n' "${pw}" "${pw}" \
    | (cd "${REPO_ROOT}/apps/monitor" && uv run hm user create "${user}") \
    >/dev/null 2>&1 \
    && _log "created admin user (${user})" \
    || _log "admin user (${user}) already exists — skipping"
}

# ----------------------------------------------------------------------------
# wait_for_url: poll a URL until it returns 200, with timeout.
# ----------------------------------------------------------------------------
wait_for_url() {
  local url="$1"
  local label="$2"
  local timeout_s="${3:-30}"
  local elapsed=0
  while (( elapsed < timeout_s )); do
    if curl -fsS -o /dev/null "${url}" 2>/dev/null; then
      _log "${label} ready"
      return 0
    fi
    sleep 1
    elapsed=$((elapsed + 1))
  done
  _warn "${label} did not become ready within ${timeout_s}s (URL: ${url})"
  return 1
}

# ----------------------------------------------------------------------------
# up_hybrid: docker sidecars + host backend + host UI.
# ----------------------------------------------------------------------------
up_hybrid() {
  _log "mode: hybrid (docker sidecars + host backend + host UI)"

  # Bring up sidecar services only (NOT the monitor container itself).
  # The monitor service in prod compose has hardcoded VM/VL/AM URLs for the
  # docker-network DNS names; in hybrid mode we want the host backend to
  # talk to 127.0.0.1:<host-port>, which means publishing the sidecar ports.
  #
  # The prod compose binds these ports to 127.0.0.1 already; for hybrid mode
  # we bring up exactly the services we need with explicit names. Grafana
  # depends on victoriametrics health, so order matters — `up -d` resolves
  # depends_on automatically.
  _log "starting docker sidecars..."
  docker compose -f "${COMPOSE_FILE}" up -d --no-build \
    victoriametrics \
    victorialogs \
    vmagent \
    alertmanager \
    karma \
    kthxbye \
    vmalert-metrics \
    vmalert-logs \
    vector \
    grafana \
    || _die "docker compose up failed"

  # Run alembic migrations against the SQLite DB before booting uvicorn.
  # AUTO_MIGRATE=1 also handles this on app startup, but doing it here
  # surfaces failures with cleaner output.
  _log "running migrations..."
  (cd "${REPO_ROOT}/apps/monitor" && uv run hm migrate up) >/dev/null 2>&1 \
    || _warn "migration command failed — AUTO_MIGRATE=1 will retry on backend start"

  ensure_admin_user

  _log "starting host backend on 127.0.0.1:${HM_DEV_BACKEND_PORT}..."
  nohup bash -c "
    cd ${REPO_ROOT}/apps/monitor
    set -a; source ${DEV_ENV_FILE}; set +a
    exec uv run uvicorn homelab_monitor.kernel.api.app:create_app \
      --factory --host 127.0.0.1 --port ${HM_DEV_BACKEND_PORT}
  " > "${LOG_DIR}/backend.log" 2>&1 &
  disown
  echo $! > "${LOG_DIR}/backend.pid"

  wait_for_url "http://127.0.0.1:${HM_DEV_BACKEND_PORT}/api/healthz" "backend" 60 || true

  _log "starting host UI dev server on 127.0.0.1:${HM_DEV_UI_PORT}..."
  nohup bash -c "
    cd ${REPO_ROOT}
    VITE_API_PROXY_TARGET=http://127.0.0.1:${HM_DEV_BACKEND_PORT} \
    VITE_DEV_PORT=${HM_DEV_UI_PORT} \
    VITE_DEV_HOST=127.0.0.1 \
    pnpm --filter ui run dev
  " > "${LOG_DIR}/ui-dev.log" 2>&1 &
  disown
  echo $! > "${LOG_DIR}/ui-dev.pid"

  wait_for_url "http://127.0.0.1:${HM_DEV_UI_PORT}/" "ui-dev-server" 30 || true
}

# ----------------------------------------------------------------------------
# up_prod: full prod compose stack from local Dockerfile build.
# ----------------------------------------------------------------------------
up_prod() {
  _log "mode: prod (full compose stack, monitor built from local Dockerfile)"

  # The prod monitor service requires a UI bundle baked into the image
  # (apps/ui/dist). Build it before docker build runs.
  if [[ ! -d "${REPO_ROOT}/apps/ui/dist" ]] \
    || [[ -z "$(ls -A "${REPO_ROOT}/apps/ui/dist" 2>/dev/null)" ]]; then
    _log "building UI bundle (apps/ui/dist)..."
    (cd "${REPO_ROOT}" && pnpm --filter ui run build) \
      || _die "UI build failed — fix before retrying"
  else
    _log "UI bundle already present — skipping rebuild (delete apps/ui/dist to force)"
  fi

  _log "building + starting full prod stack..."
  docker compose -f "${COMPOSE_FILE}" up -d --build \
    || _die "docker compose up --build failed"

  wait_for_url "http://127.0.0.1:${HOMELAB_MONITOR_PORT:-9090}/api/healthz" "monitor" 90 || true

  # Bootstrap admin user inside the container (DB lives in the volume).
  _log "bootstrapping admin user inside the monitor container..."
  docker compose -f "${COMPOSE_FILE}" exec -T monitor bash -c "
    printf '%s\n%s\n' '${HM_DEV_ADMIN_PASSWORD:-admin-dev-password}' '${HM_DEV_ADMIN_PASSWORD:-admin-dev-password}' \
      | hm user create '${HM_DEV_ADMIN_USERNAME:-admin}' 2>/dev/null \
      || echo '[bootstrap] admin user already exists'
  " || _warn "admin bootstrap exec failed — check 'docker compose logs monitor'"
}

# ----------------------------------------------------------------------------
# print_banner: show URLs + creds at the end.
# ----------------------------------------------------------------------------
print_banner() {
  local backend_url ui_url
  if [[ "${MODE}" == "prod" ]]; then
    backend_url="http://127.0.0.1:${HOMELAB_MONITOR_PORT:-9090}"
    ui_url="${backend_url}"   # prod monitor serves the UI itself
  else
    backend_url="http://127.0.0.1:${HM_DEV_BACKEND_PORT}"
    ui_url="http://127.0.0.1:${HM_DEV_UI_PORT}"
  fi

  cat <<EOF

============================================================================
  homelab-monitor dev rig is up (mode: ${MODE})
============================================================================

  UI:                ${ui_url}
  Backend API:       ${backend_url}/api
  Backend healthz:   ${backend_url}/api/healthz

  Sidecars (host-direct, hybrid mode only):
    Alertmanager:    http://127.0.0.1:${HM_DEV_AM_PORT:-9093}
    Karma:           http://127.0.0.1:${HM_DEV_KARMA_PORT:-8081}
    Grafana:         http://127.0.0.1:${HM_DEV_GRAFANA_PORT:-3000}
    VictoriaMetrics: http://127.0.0.1:${HM_DEV_VM_PORT:-8428}
    VictoriaLogs:    http://127.0.0.1:${HM_DEV_VL_PORT:-9428}
    vmalert (metrics): http://127.0.0.1:${HM_DEV_VMALERT_METRICS_PORT:-8880}
    vmalert (logs):    http://127.0.0.1:${HM_DEV_VMALERT_LOGS_PORT:-8881}

  Login:
    username:        ${HM_DEV_ADMIN_USERNAME:-admin}
    password:        ${HM_DEV_ADMIN_PASSWORD:-admin-dev-password}

  Logs (hybrid mode):
    backend:         ${LOG_DIR}/backend.log
    ui-dev:          ${LOG_DIR}/ui-dev.log

  Logs (prod mode):
    docker compose -f deploy/compose/docker-compose.yml logs -f

  Tear down:
    make dev-down

============================================================================
EOF
}

# ----------------------------------------------------------------------------
# Main flow.
# ----------------------------------------------------------------------------
main() {
  load_env
  assert_master_key_set
  ensure_data_dirs

  if (( DO_CLEAN )); then
    clean_existing
  fi

  assert_ports_free

  case "${MODE}" in
    hybrid) up_hybrid ;;
    prod)   up_prod ;;
    *)      _die "unknown mode: ${MODE}" ;;
  esac

  print_banner
}

main "$@"
