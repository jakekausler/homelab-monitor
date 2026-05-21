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
# Dev-only compose override: publishes sidecar host ports for HYBRID mode.
# Layered via `-f COMPOSE_FILE -f DEV_OVERRIDE`. NEVER passed in prod mode.
DEV_OVERRIDE="${REPO_ROOT}/deploy/dev/docker-compose.dev.yml"
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

  if [[ "${MODE}" == "prod" ]]; then
    # In prod mode, docker compose loads its own .env via --env-file.
    # Sourcing dev.env here would export dev defaults (e.g. HOMELAB_MONITOR_PORT=9090)
    # into the shell, which take precedence over --env-file (shell-env > --env-file).
    # Prod-mode host-side vars (HM_DEV_ADMIN_*) have safe inline defaults in up_prod().
    return 0
  fi

  set -a
  # shellcheck disable=SC1090
  source "${DEV_ENV_FILE}"
  set +a
}

# ----------------------------------------------------------------------------
# assert_master_key_set: refuse to continue with the placeholder key.
# ----------------------------------------------------------------------------
assert_master_key_set() {
  if [[ "${MODE}" == "prod" ]]; then
    # In prod mode, the master key is in deploy/compose/.env and loaded by
    # docker compose via --env-file (NOT exported to the shell). The
    # assertion below would always fail because HOMELAB_MONITOR_MASTER_KEY
    # is intentionally unset in the shell in prod mode.
    return 0
  fi
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
    "${HM_DEV_VM_PORT:-18428}"
    "${HM_DEV_VL_PORT:-19428}"
    "${HM_DEV_VMAGENT_PORT:-18429}"
    "${HM_DEV_AM_PORT:-19093}"
    "${HM_DEV_KARMA_PORT:-18080}"
    "${HM_DEV_GRAFANA_PORT:-13000}"
    "${HM_DEV_VMALERT_METRICS_PORT:-18880}"
    "${HM_DEV_VMALERT_LOGS_PORT:-18881}"
    "${HM_DEV_CADVISOR_PORT:-18081}"
    "${HM_DEV_VECTOR_PORT:-18686}"
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
  # Pass the dev override too so `down` resolves the same service set the
  # hybrid `up` created. `down` matches by project name (`homelab-monitor`)
  # and is unaffected by the override's `ports:`; including it is purely for
  # consistency with the `up` invocation.
  docker compose -f "${COMPOSE_FILE}" -f "${DEV_OVERRIDE}" down -v 2>/dev/null || true

  # Test compose may also be up if user was running integration tests.
  local test_compose="${REPO_ROOT}/deploy/compose/docker-compose.test.yml"
  if [[ -f "${test_compose}" ]]; then
    docker compose -f "${test_compose}" down -v 2>/dev/null || true
  fi

  # The dev SQLite DB is a HOST file (not a docker volume), so `down -v`
  # above does NOT wipe it. Remove it here so `--clean` truly yields a
  # fresh DB — otherwise a stale DB encrypted with an old master key
  # fails AES-GCM verification (SecretIntegrityError) on backend start.
  # Only handle sqlite (optionally with +aiosqlite driver) DSNs that point to
  # an absolute host file: sqlite[+aiosqlite]:////absolute/path.db (four
  # slashes = absolute path per the sqlalchemy DSN convention). Relative
  # paths (3 slashes) and the in-memory DSN (`:memory:`) are intentionally
  # ignored — they have no host file to remove.
  local db_url="${HOMELAB_MONITOR_DB_URL:-}"
  if [[ "${db_url}" =~ ^sqlite(\+aiosqlite)?:////(.+)$ ]]; then
    local db_path="/${BASH_REMATCH[2]}"
    if [[ "${db_path}" != "/:memory:" ]]; then
      _log "removing host dev DB: ${db_path}"
      rm -f "${db_path}" "${db_path}-wal" "${db_path}-shm"
    fi
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
  # In hybrid mode the backend runs on the HOST and talks to the sidecars
  # over 127.0.0.1:<host-port>. The prod compose file publishes NO sidecar
  # ports — so we layer deploy/dev/docker-compose.dev.yml (the DEV_OVERRIDE)
  # which adds the 127.0.0.1:1xxxx:cccc mappings. We list the sidecar service
  # names explicitly so the `monitor` container is never started in hybrid
  # mode. Grafana depends on victoriametrics health, so order matters —
  # `up -d` resolves depends_on automatically.
  _log "starting docker sidecars..."
  # HYBRID mode: layer the dev override so each sidecar publishes its
  # 127.0.0.1:1xxxx host port (the host-run backend reaches sidecars over
  # those ports). The prod compose file alone keeps sidecars container-only.
  # The `monitor` service is intentionally NOT listed below — in hybrid mode
  # the backend runs on the host, not in a container.
  # --no-deps: name the sidecar set explicitly and DO NOT let compose resolve
  # depends_on. Without this, alertmanager and vector both `depends_on: monitor`
  # (see deploy/compose/docker-compose.yml) so compose would transitively start
  # the `monitor` container, whose ports: 9090:9090 mapping collides with the
  # host-run backend ("address already in use"). config-init is listed
  # explicitly because --no-deps also skips it, and vector/alertmanager need its
  # render-config volume chmod to have run.
  docker compose -f "${COMPOSE_FILE}" -f "${DEV_OVERRIDE}" up -d --no-build --no-deps \
    config-init \
    victoriametrics \
    victorialogs \
    vmagent \
    alertmanager \
    karma \
    kthxbye \
    cadvisor \
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

  _log "starting host backend on ${HM_DEV_BIND_HOST:-127.0.0.1}:${HM_DEV_BACKEND_PORT}..."
  nohup bash -c "
    cd ${REPO_ROOT}/apps/monitor
    set -a; source ${DEV_ENV_FILE}; set +a
    exec uv run uvicorn homelab_monitor.kernel.api.app:create_app \
      --factory --host ${HM_DEV_BIND_HOST:-127.0.0.1} --port ${HM_DEV_BACKEND_PORT}
  " > "${LOG_DIR}/backend.log" 2>&1 &
  disown
  echo $! > "${LOG_DIR}/backend.pid"

  wait_for_url "http://127.0.0.1:${HM_DEV_BACKEND_PORT}/api/healthz" "backend" 60 || true

  _log "starting host UI dev server on ${HM_DEV_BIND_HOST:-127.0.0.1}:${HM_DEV_UI_PORT}..."
  nohup bash -c "
    cd ${REPO_ROOT}
    VITE_API_PROXY_TARGET=http://127.0.0.1:${HM_DEV_BACKEND_PORT} \
    VITE_DEV_PORT=${HM_DEV_UI_PORT} \
    VITE_DEV_HOST=${HM_DEV_BIND_HOST:-127.0.0.1} \
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
  docker compose --env-file "${REPO_ROOT}/deploy/compose/.env" -f "${COMPOSE_FILE}" up -d --build \
    || _die "docker compose up --build failed"

  wait_for_url "http://127.0.0.1:${HOMELAB_MONITOR_PORT:-9090}/api/healthz" "monitor" 90 || true

  # Bootstrap admin user inside the container (DB lives in the volume).
  _log "bootstrapping admin user inside the monitor container..."
  docker compose --env-file "${REPO_ROOT}/deploy/compose/.env" -f "${COMPOSE_FILE}" exec -T monitor bash -c "
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
    backend_url="http://${HM_DEV_BIND_HOST:-127.0.0.1}:${HM_DEV_BACKEND_PORT}"
    ui_url="http://${HM_DEV_BIND_HOST:-127.0.0.1}:${HM_DEV_UI_PORT}"
  fi

  cat <<EOF

============================================================================
  homelab-monitor dev rig is up (mode: ${MODE})
============================================================================

  UI:                ${ui_url}
  Backend API:       ${backend_url}/api
  Backend healthz:   ${backend_url}/api/healthz

  Sidecars (host-direct, hybrid mode only):
    Alertmanager:      http://127.0.0.1:${HM_DEV_AM_PORT:-19093}
    Karma:             http://127.0.0.1:${HM_DEV_KARMA_PORT:-18080}
    Grafana:           http://127.0.0.1:${HM_DEV_GRAFANA_PORT:-13000}
    VictoriaMetrics:   http://127.0.0.1:${HM_DEV_VM_PORT:-18428}
    VictoriaLogs:      http://127.0.0.1:${HM_DEV_VL_PORT:-19428}
    vmagent:           http://127.0.0.1:${HM_DEV_VMAGENT_PORT:-18429}
    vector:            http://127.0.0.1:${HM_DEV_VECTOR_PORT:-18686}
    vmalert (metrics): http://127.0.0.1:${HM_DEV_VMALERT_METRICS_PORT:-18880}
    vmalert (logs):    http://127.0.0.1:${HM_DEV_VMALERT_LOGS_PORT:-18881}

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
