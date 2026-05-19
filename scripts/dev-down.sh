#!/usr/bin/env bash
# homelab-monitor — dev rig tear-down (STAGE-001-021 Spec B).
#
# Detects the running mode (hybrid vs prod vs idle) and tears it down:
#   - Hybrid: kill host uvicorn + pnpm dev, then `docker compose down`
#   - Prod:   `docker compose down`
#   - Idle:   no-op (idempotent, exits 0)

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"

COMPOSE_FILE="${REPO_ROOT}/deploy/compose/docker-compose.yml"
# Dev-only override (sidecar host ports). Included on `down` so a hybrid
# stack tears down with the same file set it was brought up with.
DEV_OVERRIDE="${REPO_ROOT}/deploy/dev/docker-compose.dev.yml"
LOG_DIR="${REPO_ROOT}/deploy/dev/logs"

_log()  { printf '\033[1;36m[dev-down]\033[0m %s\n' "$*"; }
_warn() { printf '\033[1;33m[dev-down WARN]\033[0m %s\n' "$*" >&2; }

# ----------------------------------------------------------------------------
# Kill host-side processes by PID file; fall back to pkill if PID file is
# stale or missing.
# ----------------------------------------------------------------------------
kill_pidfile_then_name() {
  local pidfile="$1"
  local pname="$2"
  local label="$3"

  if [[ -f "${pidfile}" ]]; then
    local pid
    pid="$(cat "${pidfile}" 2>/dev/null || true)"
    if [[ -n "${pid}" ]] && kill -0 "${pid}" 2>/dev/null; then
      kill "${pid}" 2>/dev/null || true
      _log "stopped ${label} (pid=${pid})"
    fi
    rm -f "${pidfile}"
  fi

  # Belt-and-braces: also pkill by process name (catches manually launched runs).
  if pkill -f "${pname}" 2>/dev/null; then
    _log "killed stray ${label} processes"
  fi
}

# ----------------------------------------------------------------------------
# Detect what's running.
# ----------------------------------------------------------------------------
docker_running=0
if docker compose -f "${COMPOSE_FILE}" -f "${DEV_OVERRIDE}" ps --status running --quiet 2>/dev/null | grep -q .; then
  docker_running=1
fi

backend_running=0
if pgrep -f "uvicorn homelab_monitor" >/dev/null 2>&1; then
  backend_running=1
fi

ui_running=0
if pgrep -f "vite" >/dev/null 2>&1 || pgrep -f "pnpm.*dev" >/dev/null 2>&1; then
  ui_running=1
fi

if (( docker_running == 0 && backend_running == 0 && ui_running == 0 )); then
  _log "nothing to tear down"
  exit 0
fi

# ----------------------------------------------------------------------------
# Tear-down sequence: host processes first (they depend on docker sidecars),
# then docker.
# ----------------------------------------------------------------------------
if (( backend_running )); then
  kill_pidfile_then_name "${LOG_DIR}/backend.pid" "uvicorn homelab_monitor" "backend"
fi
if (( ui_running )); then
  kill_pidfile_then_name "${LOG_DIR}/ui-dev.pid" "pnpm.*dev" "ui-dev"
  pkill -f "vite" 2>/dev/null || true
fi

if (( docker_running )); then
  _log "stopping docker compose stack..."
  docker compose -f "${COMPOSE_FILE}" -f "${DEV_OVERRIDE}" down 2>/dev/null \
    || _warn "docker compose down failed (already down?)"
fi

_log "dev rig stopped"
