#!/usr/bin/env bash
#
# scripts/host-setup.sh — one-time host setup for homelab-monitor.
#
# STAGE-002-007 (cron discovery): creates the `homelab-monitor` host user,
# adds it to the `crontab` group (Debian/Ubuntu), and grants read ACLs on
# /etc/crontab and /etc/cron.d/ so the container can read system crontabs.
#
# STAGE-002-007A & STAGE-002-009: The original spool-ACL read mechanism (the
# crontab-acl watcher that re-applied ACLs on every crontab -e edit) is
# RETIRED. STAGE-002-009 (Option B crontab-snapshot fix) replaces it with a
# root-side host script (hm-crontab-snapshot) that runs `crontab -l -u <user>`
# and writes a world-readable snapshot the container reads instead. Real
# 0600 spool files are NEVER given an ACL — an ACL makes vixie-cron reject
# the crontab as INSECURE MODE and refuse to run it. The snapshot is refreshed
# via a systemd .path watcher on /var/spool/cron/crontabs AND a ~300s .timer.
#
# STAGE-002-009 (cron-apply executor): the monitor container now has ZERO
# host-write capability. The host-side cron-apply executor (running as root)
# handles all crontab writes. The container only READS crontabs (via snapshot)
# for discovery. This script grants READ-ONLY ACLs on /etc/crontab and
# /etc/cron.d/ and installs the executor (apply script + 2 systemd units) +
# the snapshot script + the snapshot systemd units + the IPC directory tree.
#
# This script is intentionally minimal and idempotent: running it twice is
# a no-op. Future stages (systemd discovery, NAS mount perms) will extend it
# with additional capabilities — the structure is designed to accommodate.
#
# Usage:
#   sudo bash scripts/host-setup.sh                    # apply
#   sudo bash scripts/host-setup.sh --check            # report current state, make no changes
#   sudo bash scripts/host-setup.sh --write-env <path> # apply and write UID/GID/HOSTNAME to env file
#   sudo bash scripts/host-setup.sh --check --write-env <path> # dry-run: show what would be written
#
set -euo pipefail

readonly USERNAME="homelab-monitor"
readonly CRONTAB_DIR="/var/spool/cron/crontabs"

# Repo paths — host-setup.sh is run as `sudo bash scripts/host-setup.sh` from
# the repo root, but resolve our own location so it works from anywhere.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
readonly REPO_ROOT
readonly SYSTEMD_SRC_DIR="$REPO_ROOT/deploy/systemd"
readonly SNAPSHOT_SCRIPT_SRC="$SCRIPT_DIR/hm-crontab-snapshot.sh"
readonly SNAPSHOT_SCRIPT_DEST="/usr/local/sbin/hm-crontab-snapshot"
readonly SNAPSHOT_DIR="/var/lib/homelab-monitor/crontab-snapshot"
readonly SNAPSHOT_PATH_UNIT="homelab-monitor-crontab-snapshot.path"
readonly SNAPSHOT_SERVICE_UNIT="homelab-monitor-crontab-snapshot.service"
readonly SNAPSHOT_TIMER_UNIT="homelab-monitor-crontab-snapshot.timer"
readonly APPLY_SCRIPT_SRC="$SCRIPT_DIR/hm-cron-apply.sh"
readonly APPLY_SCRIPT_DEST="/usr/local/sbin/hm-cron-apply"
readonly SYSTEMD_DEST_DIR="/etc/systemd/system"
readonly APPLY_PATH_UNIT="homelab-monitor-cron-apply.path"
readonly APPLY_SERVICE_UNIT="homelab-monitor-cron-apply.service"
readonly APPLY_TIMER_UNIT="homelab-monitor-cron-apply.timer"

# STAGE-003-010: homelab-compose group + shared-file ACLs (see section near
# end of script for the implementing functions and why this group exists).
readonly COMPOSE_GROUP="homelab-compose"
# Override via env: COMPOSE_GROUP_DESKTOP_USER=alice sudo bash scripts/host-setup.sh
# Defaults to invoker's $SUDO_USER if available, otherwise to the host's primary 1000-uid user.
readonly COMPOSE_GROUP_DESKTOP_USER="${COMPOSE_GROUP_DESKTOP_USER:-${SUDO_USER:-jakekausler}}"
readonly SHARED_FILES_CONF="$SCRIPT_DIR/host-setup-shared-files.conf"

# STAGE-009-002: auto-fix transcript directory + POSIX default ACLs.
# The fixer-runner container (STAGE-009-003) writes Claude transcripts here; the
# monitor container READS them (transcript viewer + audit). The dir is bind-
# mounted into the monitor at /data/runbook-transcripts (READ-ONLY for the
# monitor — non-negotiable #4 audit integrity). Default ACLs make every file the
# runner writes inherit monitor-readable (rX) + fixer-writable (rwX).
readonly FIXER_TRANSCRIPTS_DIR="${HM_FIXER_TRANSCRIPTS_SRC:-/var/lib/homelab-monitor/runbook-transcripts}"
# Numeric identity the ACL grants. UID = the in-container homelab-fixer user
# (created in STAGE-009-003); GID = the shared-GID fallback group when setfacl is
# absent. Defaults 1002:1002 avoid colliding with this project's own service IDs
# (1000 homelab, 2000 amconfig, 999 docker, 994 homelab-compose) but are NOT
# guaranteed free against arbitrary host users — verify against your host's
# /etc/passwd and /etc/group and override via env if 1002 is taken, e.g.
#   HM_FIXER_UID=5001 HM_FIXER_GID=5001 sudo bash scripts/host-setup.sh
readonly FIXER_UID="${HM_FIXER_UID:-1002}"
readonly FIXER_GID="${HM_FIXER_GID:-1002}"
# Fallback group name (used only when setfacl is unavailable — shared-GID + setgid dir).
readonly FIXER_FALLBACK_GROUP="homelab-fixer"

CHECK_ONLY=0
WRITE_ENV_FILE=""

# Parse arguments
while [[ $# -gt 0 ]]; do
    case "$1" in
        --check)
            CHECK_ONLY=1
            shift
            ;;
        --write-env)
            if [[ $# -lt 2 ]]; then
                echo "ERROR: --write-env requires a path argument" >&2
                exit 1
            fi
            WRITE_ENV_FILE="$2"
            shift 2
            ;;
        *)
            echo "ERROR: unknown argument '$1'" >&2
            exit 1
            ;;
    esac
done

if [[ $EUID -ne 0 ]]; then
    echo "ERROR: must run as root (or via sudo)" >&2
    exit 1
fi

log() { printf '[host-setup] %s\n' "$*"; }
do_or_check() {
    if [[ $CHECK_ONLY -eq 1 ]]; then
        log "WOULD: $*"
    else
        log "EXEC: $*"
        eval "$@"
    fi
}

# Idempotent env file update: replace existing KEY=value or append if missing
update_env_var() {
    local file="$1" key="$2" value="$3"

    # Check if file exists
    if [[ ! -f "$file" ]]; then
        log "ERROR: env file not found: $file"
        return 1
    fi

    # Remember original permissions
    local orig_perms
    orig_perms=$(stat -c %a "$file" 2>/dev/null || stat -f %A "$file" 2>/dev/null || echo "600")

    # Check if key exists (commented or not)
    if grep -qE "^#?\s*${key}=" "$file" 2>/dev/null; then
        # Replace existing line (works for both commented and uncommented)
        sed -i.bak "s|^#\?\s*${key}=.*|${key}=${value}|" "$file"
        rm -f "${file}.bak"
    else
        # Append new line
        echo "${key}=${value}" >> "$file"
    fi

    # Restore permissions
    chmod "$orig_perms" "$file"
}

# ---------------------------------------------------------------------------
# STAGE-003-010: homelab-compose group + shared-file ACLs
# ---------------------------------------------------------------------------
#
# The Pull & Restart action runs `docker compose pull <svc> && docker compose
# up -d <svc>` from inside the monitor container. compose needs to read .env
# for variable interpolation; .env is normally 0600 owned by the user. We
# create a dedicated `homelab-compose` group so the container's user
# (homelab-monitor, uid 995) and the desktop user (jakekausler, uid 1000)
# can both read .env via group membership. The script reads the path list
# from scripts/host-setup-shared-files.conf and applies chgrp+chmod
# idempotently.

ensure_compose_group() {
    if getent group "$COMPOSE_GROUP" > /dev/null; then
        log "PRESENT: group $COMPOSE_GROUP already exists"
    else
        do_or_check "groupadd --system $COMPOSE_GROUP"
    fi
}

ensure_user_in_compose_group() {
    local user="$1"
    if ! getent passwd "$user" > /dev/null; then
        log "WARN: user $user not found on host — skipping group membership"
        return
    fi
    if id -nG "$user" 2>/dev/null | tr ' ' '\n' | grep -qx "$COMPOSE_GROUP"; then
        log "PRESENT: user $user already in $COMPOSE_GROUP"
    else
        do_or_check "usermod -aG $COMPOSE_GROUP $user"
    fi
}

apply_compose_group_to_shared_files() {
    if [[ ! -f $SHARED_FILES_CONF ]]; then
        log "WARN: $SHARED_FILES_CONF not found — skipping shared-file ACLs"
        return
    fi
    while IFS= read -r line; do
        # Trim whitespace and skip blank/comment lines.
        line="${line#"${line%%[![:space:]]*}"}"
        line="${line%"${line##*[![:space:]]}"}"
        [[ -z $line || ${line:0:1} == "#" ]] && continue
        if [[ ! -e $line ]]; then
            log "WARN: $line listed in shared-files conf but does not exist — skipping"
            continue
        fi
        local target
        target="$(readlink -f "$line")"
        local cur_group
        cur_group="$(stat -c '%G' "$target")"
        local cur_mode
        cur_mode="$(stat -c '%a' "$target")"
        local want_mode
        if [[ -d $target ]]; then
            want_mode="0750"
        else
            want_mode="0640"
        fi
        if [[ $cur_group != "$COMPOSE_GROUP" ]]; then
            do_or_check "chgrp $COMPOSE_GROUP $target"
        else
            log "PRESENT: $target already owned by group $COMPOSE_GROUP"
        fi
        if [[ $cur_mode != "$want_mode" ]]; then
            do_or_check "chmod $want_mode $target"
        else
            log "PRESENT: $target already at mode $want_mode"
        fi
    done < "$SHARED_FILES_CONF"
}

# --- 1. Ensure the user exists ---
if id "$USERNAME" >/dev/null 2>&1; then
    log "OK: user $USERNAME already exists"
else
    do_or_check "useradd --system --shell /usr/sbin/nologin --no-create-home '$USERNAME'"
fi

# --- 2. Add to the `crontab` group (Debian/Ubuntu convention) ---
if getent group crontab >/dev/null 2>&1; then
    if id -nG "$USERNAME" 2>/dev/null | tr ' ' '\n' | grep -qx crontab; then
        log "OK: $USERNAME already in crontab group"
    else
        do_or_check "usermod -a -G crontab '$USERNAME'"
    fi
else
    log "WARN: crontab group not found; this is RHEL/CentOS or non-standard. Skipping group add."
fi

# --- 3. (REMOVED) user-crontab read ACLs ---
# Option B crontab-snapshot fix (STAGE-002-009): the monitor container no
# longer reads /var/spool/cron/crontabs directly. A root-side snapshot script
# (hm-crontab-snapshot, installed below) runs `crontab -l -u <user>` and
# writes a world-readable snapshot the container reads instead. The real
# 0600 spool files are NEVER given an ACL — an ACL makes vixie-cron reject
# the crontab as INSECURE MODE. No setfacl on the spool dir at all now.
if [[ ! -d "$CRONTAB_DIR" ]]; then
    log "WARN: $CRONTAB_DIR not found; user crontabs cannot be snapshotted until cron is installed"
fi

# --- 3.5. Grant read-only ACL on /etc/crontab and /etc/cron.d/ ---
# The container ONLY READS system crontabs for discovery. All crontab writes
# (including to /etc/crontab and /etc/cron.d/) happen on the host via the
# cron-apply executor (host-side root process).
if ! command -v setfacl >/dev/null 2>&1; then
    log "WARN: setfacl not installed; skipping /etc/crontab and /etc/cron.d/ ACLs"
else
    # 3.5a. /etc/crontab (single file) — skip with WARN if absent
    if [[ -f "/etc/crontab" ]]; then
        current_acl=$(getfacl --absolute-names "/etc/crontab" 2>/dev/null | grep "user:$USERNAME:" || true)
        if [[ -n "$current_acl" ]] && [[ "$current_acl" == *"r--"* ]]; then
            log "OK: ACL already set on /etc/crontab"
        else
            do_or_check "setfacl -m 'u:$USERNAME:r' '/etc/crontab'"
        fi
    else
        log "WARN: /etc/crontab not found; skipping"
    fi

    # 3.5b. /etc/cron.d/ (directory) — directory read (rx) for traversal + listing
    if [[ -d "/etc/cron.d" ]]; then
        # Directory traversal (rx)
        dir_acl=$(getfacl --absolute-names "/etc/cron.d" 2>/dev/null | grep "user:$USERNAME:r-x" || true)
        if [[ -n "$dir_acl" ]]; then
            log "OK: directory ACL already set on /etc/cron.d"
        else
            do_or_check "setfacl -m 'u:$USERNAME:rx' '/etc/cron.d'"
        fi

        # Per-existing-file read-only (r)
        shopt -s nullglob
        for f in "/etc/cron.d"/*; do
            [[ -e "$f" ]] || continue  # handle glob non-match
            current_acl=$(getfacl --absolute-names "$f" 2>/dev/null | grep "user:$USERNAME:" || true)
            if [[ -n "$current_acl" ]] && [[ "$current_acl" == *"r--"* ]]; then
                log "OK: ACL already set on $f"
            else
                do_or_check "setfacl -m 'u:$USERNAME:r' '$f'"
            fi
        done
        shopt -u nullglob

        # Default ACL for new files (r)
        current_default=$(getfacl --absolute-names "/etc/cron.d" 2>/dev/null | grep "default:user:$USERNAME:" || true)
        if [[ -n "$current_default" ]] && [[ "$current_default" == *"r--"* ]]; then
            log "OK: default ACL already set on /etc/cron.d"
        else
            do_or_check "setfacl -d -m 'u:$USERNAME:r' '/etc/cron.d'"
        fi
    else
        log "WARN: /etc/cron.d not found; skipping"
    fi
fi

# --- 3.6. Cron-apply IPC directory (STAGE-002-009 host-side executor) ---
# The monitor container writes request JSON files into requests/; the
# host-side hm-cron-apply executor writes result JSON into results/. The dir
# is bind-mounted into the container at /host-ipc.
#
# Ownership model:
#   - requests/ : container WRITES, executor READS+DELETES.
#                 Owned by $USERNAME so the container (uid 995) can create
#                 files; the executor runs as root so it can always read/del.
#   - results/  : executor WRITES (as root), container READS.
#                 Owned by root; world-readable files so the container reads.
readonly IPC_DIR="/var/lib/homelab-monitor/cron-apply"
readonly IPC_REQUESTS="$IPC_DIR/requests"
readonly IPC_RESULTS="$IPC_DIR/results"
for d in "$IPC_DIR" "$IPC_REQUESTS" "$IPC_RESULTS"; do
    if [[ -d "$d" ]]; then
        log "OK: $d already exists"
    else
        do_or_check "mkdir -p '$d'"
    fi
done
# requests/ owned by the monitor user so the container can create files there.
do_or_check "chown root:root '$IPC_DIR'"
do_or_check "chmod 0755 '$IPC_DIR'"
do_or_check "chown '$USERNAME':'$USERNAME' '$IPC_REQUESTS'"
do_or_check "chmod 0755 '$IPC_REQUESTS'"
# results/ owned by root; the executor writes here, the container only reads.
do_or_check "chown root:root '$IPC_RESULTS'"
do_or_check "chmod 0755 '$IPC_RESULTS'"

# --- 3.7. Check for jq dependency ---
# The host-side cron-apply executor requires jq to process request/result JSON.
if ! command -v jq >/dev/null 2>&1; then
    log "WARN: jq not installed — install the 'jq' package; hm-cron-apply requires it"
fi

# --- 3.8. Crontab snapshot directory (STAGE-002-009 Option B fix) ---
# The host-side hm-crontab-snapshot script writes one file per user here
# (filename = username, content = that user's raw `crontab -l` output). The
# directory is bind-mounted READ-ONLY into the monitor container; the
# discoverer reads it instead of the 0600 spool files.
#
# Ownership: root-owned, world-readable. Crontab schedules/commands are not
# secrets (discovery secret-scrubs commands at storage time) and the non-root
# container must be able to read the snapshot.
for d in "/var/lib/homelab-monitor" "$SNAPSHOT_DIR"; do
    if [[ -d "$d" ]]; then
        log "OK: $d already exists"
    else
        do_or_check "mkdir -p '$d'"
    fi
done
do_or_check "chown root:root '$SNAPSHOT_DIR'"
do_or_check "chmod 0755 '$SNAPSHOT_DIR'"

# --- 3.9. STAGE-009-002: runbook transcript dir + POSIX default ACLs ---
# The auto-fix fixer-runner (STAGE-009-003) WRITES Claude transcripts into this
# directory; the monitor container only READS them (transcript viewer + audit).
# We apply POSIX DEFAULT ACLs so every file the runner creates INHERITS:
#   - monitor UID : rX  (READ-only — non-negotiable #4: the monitor must never
#                        be able to mutate an in-progress audit transcript)
#   - fixer  UID : rwX  (the runner writes here)
# Default ACLs survive cross-container UID/umask differences (the monitor runs
# at a host-specific runtime UID that need not equal the build-time 1000).
#
# This stage does NOT create the in-container homelab-fixer OS user (that is the
# runner image's job in STAGE-009-003) and grants NO docker group / NO sudoers
# to the fixer — only this single directory's read/write boundary (#3).
readonly MONITOR_RUNTIME_UID="${HM_CRON_HOST_UID:-$(id -u "$USERNAME" 2>/dev/null || echo 1000)}"

# 3.9a. Ensure the directory exists.
for d in "/var/lib/homelab-monitor" "$FIXER_TRANSCRIPTS_DIR"; do
    if [[ -d "$d" ]]; then
        log "OK: $d already exists"
    else
        do_or_check "mkdir -p '$d'"
    fi
done

if ! command -v setfacl >/dev/null 2>&1; then
    # 3.9b-fallback. setfacl unavailable: WARN-degrade to a shared supplementary
    # GID + setgid directory (chmod 2770), mirroring the amconfig GID-2000 idiom.
    # The monitor (group member) and the fixer (group member) both get group
    # rwx; the setgid bit makes new files inherit the group. NOTE: under the
    # fallback the monitor gets group-WRITE too (group rwx) — this is a weaker
    # posture than the ACL path (which gives the monitor read-only). The ACL
    # path is STRONGLY preferred; install the `acl` package (Debian/Ubuntu:
    # apt install acl) to get the read-only-monitor guarantee of #4.
    log "WARN: setfacl not installed; falling back to shared GID + setgid dir."
    log "WARN: install the 'acl' package for the read-only-monitor ACL posture (#4)."
    if getent group "$FIXER_FALLBACK_GROUP" >/dev/null 2>&1; then
        log "OK: group $FIXER_FALLBACK_GROUP already exists"
    else
        do_or_check "groupadd --system --gid '$FIXER_GID' '$FIXER_FALLBACK_GROUP' || groupadd --system '$FIXER_FALLBACK_GROUP'"
        if [[ $CHECK_ONLY -eq 0 ]]; then
            _actual_fallback_gid=$(getent group "$FIXER_FALLBACK_GROUP" 2>/dev/null | cut -d: -f3 || echo "$FIXER_GID")
            if [[ "$_actual_fallback_gid" != "$FIXER_GID" ]]; then
                log "WARN: $FIXER_FALLBACK_GROUP was created with GID $_actual_fallback_gid (requested GID $FIXER_GID was already taken on this host)."
                log "WARN: the summary and --write-env below will report the REQUESTED GID ($FIXER_GID)."
                log "WARN: update HM_FIXER_GID=$_actual_fallback_gid in your overrides env and re-run host-setup.sh."
            fi
        fi
    fi
    do_or_check "chgrp '$FIXER_FALLBACK_GROUP' '$FIXER_TRANSCRIPTS_DIR'"
    do_or_check "chmod 2770 '$FIXER_TRANSCRIPTS_DIR'"
    log "WARN: under the fallback, add BOTH the monitor user and the fixer user to"
    log "WARN: group $FIXER_FALLBACK_GROUP (the runner does this for the fixer in 003;"
    log "WARN: the monitor needs group_add for $FIXER_FALLBACK_GROUP's GID in compose)."
else
    # 3.9b. setfacl present (preferred path). Base ownership: dir owned by the
    # monitor runtime user so the monitor can traverse/read; mode 0750.
    do_or_check "chown '$MONITOR_RUNTIME_UID':'$MONITOR_RUNTIME_UID' '$FIXER_TRANSCRIPTS_DIR'"
    do_or_check "chmod 0750 '$FIXER_TRANSCRIPTS_DIR'"

    if [[ $CHECK_ONLY -eq 1 ]]; then
        # --check mode: print intended ACL actions (do_or_check already logs WOULD:).
        monitor_acl=$(getfacl --absolute-names "$FIXER_TRANSCRIPTS_DIR" 2>/dev/null | grep "user:$MONITOR_RUNTIME_UID:r-x" || true)
        if [[ -n "$monitor_acl" ]]; then
            log "OK: access ACL (monitor rx) already set on $FIXER_TRANSCRIPTS_DIR"
        else
            do_or_check "setfacl -m 'u:$MONITOR_RUNTIME_UID:rx' '$FIXER_TRANSCRIPTS_DIR'"
        fi
        fixer_acl=$(getfacl --absolute-names "$FIXER_TRANSCRIPTS_DIR" 2>/dev/null | grep "user:$FIXER_UID:rwx" || true)
        if [[ -n "$fixer_acl" ]]; then
            log "OK: access ACL (fixer rwx) already set on $FIXER_TRANSCRIPTS_DIR"
        else
            do_or_check "setfacl -m 'u:$FIXER_UID:rwx' '$FIXER_TRANSCRIPTS_DIR'"
        fi
        monitor_default=$(getfacl --absolute-names "$FIXER_TRANSCRIPTS_DIR" 2>/dev/null | grep "default:user:$MONITOR_RUNTIME_UID:r-x" || true)
        if [[ -n "$monitor_default" ]]; then
            log "OK: default ACL (monitor rx) already set on $FIXER_TRANSCRIPTS_DIR"
        else
            do_or_check "setfacl -d -m 'u:$MONITOR_RUNTIME_UID:rx' '$FIXER_TRANSCRIPTS_DIR'"
        fi
        fixer_default=$(getfacl --absolute-names "$FIXER_TRANSCRIPTS_DIR" 2>/dev/null | grep "default:user:$FIXER_UID:rwx" || true)
        if [[ -n "$fixer_default" ]]; then
            log "OK: default ACL (fixer rwx) already set on $FIXER_TRANSCRIPTS_DIR"
        else
            do_or_check "setfacl -d -m 'u:$FIXER_UID:rwx' '$FIXER_TRANSCRIPTS_DIR'"
        fi
    else
        # Live mode: apply all four ACLs in a single non-fatal block.
        # If ANY setfacl call fails (e.g. host filesystem rejects numeric UID ACLs),
        # WARN and degrade to the shared-GID + setgid fallback rather than aborting
        # the entire script under set -e. An `if ! { ... }` compound command does
        # NOT trigger set -e on inner failures — only the compound result matters.
        _setfacl_ok=1
        monitor_acl=$(getfacl --absolute-names "$FIXER_TRANSCRIPTS_DIR" 2>/dev/null | grep "user:$MONITOR_RUNTIME_UID:r-x" || true)
        fixer_acl=$(getfacl --absolute-names "$FIXER_TRANSCRIPTS_DIR" 2>/dev/null | grep "user:$FIXER_UID:rwx" || true)
        monitor_default=$(getfacl --absolute-names "$FIXER_TRANSCRIPTS_DIR" 2>/dev/null | grep "default:user:$MONITOR_RUNTIME_UID:r-x" || true)
        fixer_default=$(getfacl --absolute-names "$FIXER_TRANSCRIPTS_DIR" 2>/dev/null | grep "default:user:$FIXER_UID:rwx" || true)

        if [[ -n "$monitor_acl" ]]; then
            log "OK: access ACL (monitor rx) already set on $FIXER_TRANSCRIPTS_DIR"
        fi
        if [[ -n "$fixer_acl" ]]; then
            log "OK: access ACL (fixer rwx) already set on $FIXER_TRANSCRIPTS_DIR"
        fi
        if [[ -n "$monitor_default" ]]; then
            log "OK: default ACL (monitor rx) already set on $FIXER_TRANSCRIPTS_DIR"
        fi
        if [[ -n "$fixer_default" ]]; then
            log "OK: default ACL (fixer rwx) already set on $FIXER_TRANSCRIPTS_DIR"
        fi

        if { \
            { [[ -n "$monitor_acl" ]] || setfacl -m "u:$MONITOR_RUNTIME_UID:rx" "$FIXER_TRANSCRIPTS_DIR"; } && \
            { [[ -n "$fixer_acl" ]] || setfacl -m "u:$FIXER_UID:rwx" "$FIXER_TRANSCRIPTS_DIR"; } && \
            { [[ -n "$monitor_default" ]] || setfacl -d -m "u:$MONITOR_RUNTIME_UID:rx" "$FIXER_TRANSCRIPTS_DIR"; } && \
            { [[ -n "$fixer_default" ]] || setfacl -d -m "u:$FIXER_UID:rwx" "$FIXER_TRANSCRIPTS_DIR"; }; \
        }; then
            log "EXEC: ACLs applied on $FIXER_TRANSCRIPTS_DIR (monitor rx, fixer rwx, defaults)"
        else
            _setfacl_ok=0
            log "WARN: one or more setfacl calls failed on $FIXER_TRANSCRIPTS_DIR."
            log "WARN: degrading to shared-GID + setgid fallback (weaker posture — monitor gets group-write)."
            log "WARN: install the 'acl' package and re-run for the read-only-monitor ACL guarantee (#4)."
            if getent group "$FIXER_FALLBACK_GROUP" >/dev/null 2>&1; then
                log "OK: group $FIXER_FALLBACK_GROUP already exists"
            else
                groupadd --system --gid "$FIXER_GID" "$FIXER_FALLBACK_GROUP" || groupadd --system "$FIXER_FALLBACK_GROUP"
                _actual_gid=$(getent group "$FIXER_FALLBACK_GROUP" 2>/dev/null | cut -d: -f3 || echo "$FIXER_GID")
                _EFFECTIVE_FIXER_GID="$_actual_gid"
                if [[ "$_EFFECTIVE_FIXER_GID" != "$FIXER_GID" ]]; then
                    log "WARN: $FIXER_FALLBACK_GROUP was created with GID $_EFFECTIVE_FIXER_GID (requested $FIXER_GID was taken)."
                    log "WARN: update HM_FIXER_GID=$_EFFECTIVE_FIXER_GID in your overrides env and re-run host-setup.sh."
                fi
            fi
            chgrp "$FIXER_FALLBACK_GROUP" "$FIXER_TRANSCRIPTS_DIR"
            chmod 2770 "$FIXER_TRANSCRIPTS_DIR"
            log "WARN: add BOTH the monitor user and the fixer user to group $FIXER_FALLBACK_GROUP."
        fi
    fi
fi

# --- 4. Install systemd units and executor scripts ---
# Install the snapshot script + units (which refresh the crontab snapshot on
# spool changes and on a periodic timer) and the cron-apply executor (apply
# script + systemd units) which processes cron-wrapper install requests from
# the container.
if ! command -v systemctl >/dev/null 2>&1; then
    log "WARN: systemctl not found; non-systemd host. Skipping systemd unit install."
    log "WARN: snapshot script will not be triggered on crontab -e; run it manually or via cron."
else
    # 4-retire. Remove stale crontab-acl units from older host-setup.sh runs.
    # The crontab-acl .path watcher + refresh-crontab-acl.sh applied a read ACL
    # directly on the 0600 spool files, which caused vixie-cron to reject the
    # crontab as INSECURE MODE. These units are no longer shipped by the repo.
    # This block is a clean no-op on hosts that never had them.
    for _old_unit in homelab-monitor-crontab-acl.path homelab-monitor-crontab-acl.service; do
        if systemctl cat "$_old_unit" >/dev/null 2>&1; then
            log "RETIRE: disabling stale unit $_old_unit"
            do_or_check "systemctl disable --now '$_old_unit' || true"
        else
            log "OK: stale unit $_old_unit not present (nothing to retire)"
        fi
    done
    do_or_check "rm -f '/etc/systemd/system/homelab-monitor-crontab-acl.path' \
        '/etc/systemd/system/homelab-monitor-crontab-acl.service'"
    do_or_check "rm -f '/usr/local/sbin/refresh-crontab-acl.sh'"
    # daemon-reload is done at step 4c below; no extra reload needed here.

    # 4-retire-acl. Strip stale homelab-monitor ACLs from the crontab spool.
    # The old approach applied ACLs directly to /var/spool/cron/crontabs and to
    # each user's crontab file. Those ACLs cause vixie-cron to reject the
    # crontab as INSECURE MODE. Strip them now so existing hosts get clean files.
    # This is a no-op on hosts that never had the old setup.
    if ! command -v setfacl >/dev/null 2>&1; then
        log "WARN: setfacl not installed; cannot strip stale crontab spool ACLs — verify manually"
    elif [[ ! -d "$CRONTAB_DIR" ]]; then
        log "OK: $CRONTAB_DIR not present; no stale crontab spool ACLs to strip"
    else
        # Strip homelab-monitor access ACL + default ACL from the spool directory.
        # Use targeted -x so we only remove our entry, not any other legitimate ACL.
        do_or_check "setfacl -x 'u:$USERNAME' '$CRONTAB_DIR' || true"
        do_or_check "setfacl -d -x 'u:$USERNAME' '$CRONTAB_DIR' || true"
        log "RETIRE-ACL: stripped homelab-monitor ACL from $CRONTAB_DIR"

        # Strip ALL ACLs from every crontab file in the spool dir.
        # vixie-cron requires zero ACLs on spool files; setfacl -b is the only
        # way to guarantee a clean file even if a mask entry lingers after -x.
        shopt -s nullglob
        for _crontab_file in "$CRONTAB_DIR"/*; do
            [[ -f "$_crontab_file" ]] || continue
            do_or_check "setfacl -b '$_crontab_file' || true"
            log "RETIRE-ACL: stripped all ACLs from $_crontab_file"
        done
        shopt -u nullglob
    fi

    # 4a. Install the crontab-snapshot script to a stable absolute path.
    if [[ -f "$SNAPSHOT_SCRIPT_DEST" ]] \
        && cmp -s "$SNAPSHOT_SCRIPT_SRC" "$SNAPSHOT_SCRIPT_DEST"; then
        log "OK: $SNAPSHOT_SCRIPT_DEST already up to date"
    else
        do_or_check "install -m 0755 '$SNAPSHOT_SCRIPT_SRC' '$SNAPSHOT_SCRIPT_DEST'"
    fi

    # 4a-bis. Install the cron-apply executor script.
    if [[ -f "$APPLY_SCRIPT_DEST" ]] \
        && cmp -s "$APPLY_SCRIPT_SRC" "$APPLY_SCRIPT_DEST"; then
        log "OK: $APPLY_SCRIPT_DEST already up to date"
    else
        do_or_check "install -m 0755 '$APPLY_SCRIPT_SRC' '$APPLY_SCRIPT_DEST'"
    fi

    # 4b. Install the unit files (3 snapshot + 3 cron-apply).
    for unit in "$SNAPSHOT_SERVICE_UNIT" "$SNAPSHOT_PATH_UNIT" "$SNAPSHOT_TIMER_UNIT" \
                "$APPLY_SERVICE_UNIT" "$APPLY_PATH_UNIT" "$APPLY_TIMER_UNIT"; do
        src="$SYSTEMD_SRC_DIR/$unit"
        dest="$SYSTEMD_DEST_DIR/$unit"
        if [[ ! -f "$src" ]]; then
            log "ERROR: unit file not found in repo: $src"
            exit 1
        fi
        if [[ -f "$dest" ]] && cmp -s "$src" "$dest"; then
            log "OK: $dest already up to date"
        else
            do_or_check "install -m 0644 '$src' '$dest'"
        fi
    done

    # 4c. Reload systemd so it sees the (possibly new/changed) units.
    do_or_check "systemctl daemon-reload"

    # 4d. Enable + start the crontab-snapshot .path watcher. Skip if
    #     CRONTAB_DIR does not exist yet (PathChanged= on a missing dir errors).
    if [[ ! -d "$CRONTAB_DIR" ]]; then
        log "WARN: $SNAPSHOT_PATH_UNIT not enabled — $CRONTAB_DIR does not exist (install cron(d) first, then re-run host-setup.sh)"
    elif systemctl is-enabled "$SNAPSHOT_PATH_UNIT" >/dev/null 2>&1 \
        && systemctl is-active "$SNAPSHOT_PATH_UNIT" >/dev/null 2>&1; then
        log "OK: $SNAPSHOT_PATH_UNIT already enabled and active"
    else
        do_or_check "systemctl enable --now '$SNAPSHOT_PATH_UNIT'"
    fi

    # 4d-snap-timer. Enable + start the periodic snapshot .timer (no dir guard
    #     needed — the service handles a missing spool dir gracefully).
    if systemctl is-enabled "$SNAPSHOT_TIMER_UNIT" >/dev/null 2>&1 \
        && systemctl is-active "$SNAPSHOT_TIMER_UNIT" >/dev/null 2>&1; then
        log "OK: $SNAPSHOT_TIMER_UNIT already enabled and active"
    else
        do_or_check "systemctl enable --now '$SNAPSHOT_TIMER_UNIT'"
    fi

    # 4d-bis. Enable + start the cron-apply .path unit (idempotent). The IPC dir was
    #     created in section 3.6 above, so no skip-guard needed.
    if systemctl is-enabled "$APPLY_PATH_UNIT" >/dev/null 2>&1 \
        && systemctl is-active "$APPLY_PATH_UNIT" >/dev/null 2>&1; then
        log "OK: $APPLY_PATH_UNIT already enabled and active"
    else
        do_or_check "systemctl enable --now '$APPLY_PATH_UNIT'"
    fi

    # 4d-ter. Enable + start the periodic cron-apply .timer (safety net for
    #     missed .path watcher edges). No dir guard — the executor handles a
    #     missing requests dir gracefully.
    if systemctl is-enabled "$APPLY_TIMER_UNIT" >/dev/null 2>&1 \
        && systemctl is-active "$APPLY_TIMER_UNIT" >/dev/null 2>&1; then
        log "OK: $APPLY_TIMER_UNIT already enabled and active"
    else
        do_or_check "systemctl enable --now '$APPLY_TIMER_UNIT'"
    fi

    # 4e. Initial snapshot run so the snapshot dir is populated immediately
    #     (the .path watcher fires only on future changes; the .timer only on
    #     its schedule). Safe to run by hand — idempotent.
    if [[ -x "$SNAPSHOT_SCRIPT_DEST" ]]; then
        do_or_check "'$SNAPSHOT_SCRIPT_DEST'"
    fi
fi

# --- 4.5. STAGE-003-010: homelab-compose group + shared-file ACLs ---
log "Setting up homelab-compose group for shared file access (STAGE-003-010)..."
ensure_compose_group
ensure_user_in_compose_group "$USERNAME"
ensure_user_in_compose_group "$COMPOSE_GROUP_DESKTOP_USER"
apply_compose_group_to_shared_files

# --- 5. Print UID/GID for dev.env / production env ---
UID_VAL=$(id -u "$USERNAME" 2>/dev/null || echo "<not-created>")
GID_VAL=$(id -g "$USERNAME" 2>/dev/null || echo "<not-created>")
HOSTNAME_VAL=$(hostname -f 2>/dev/null || hostname 2>/dev/null || echo "<unable-to-determine>")

cat <<EOF

### Setup output ###
Paste these into deploy/dev/dev.env (or your production env):

  HM_CRON_HOST_UID=$UID_VAL
  HM_CRON_HOST_GID=$GID_VAL
  HM_HOST_HOSTNAME=$HOSTNAME_VAL

The auto-fix (STAGE-009-002) transcript ACL was granted for:

  HM_FIXER_UID=$FIXER_UID
  HM_FIXER_GID=$FIXER_GID

(These are the values this run's ACL used. They MUST match the homelab-fixer
user that STAGE-009-003's fixer-runner image creates. Set the real values in
your overrides env BEFORE running this script if 1002 collides on your host.)

Then restart the monitor: docker compose up -d --force-recreate monitor
EOF

# Handle env file writing if requested
if [[ -n "$WRITE_ENV_FILE" ]]; then
    if [[ $CHECK_ONLY -eq 1 ]]; then
        log "CHECK: would write to $WRITE_ENV_FILE:"
        log "  HM_CRON_HOST_UID=$UID_VAL"
        log "  HM_CRON_HOST_GID=$GID_VAL"
        log "  HM_HOST_HOSTNAME=$HOSTNAME_VAL"
        log "  HM_FIXER_UID=$FIXER_UID"
        log "  HM_FIXER_GID=$FIXER_GID"
    else
        if update_env_var "$WRITE_ENV_FILE" "HM_CRON_HOST_UID" "$UID_VAL" && \
           update_env_var "$WRITE_ENV_FILE" "HM_CRON_HOST_GID" "$GID_VAL" && \
           update_env_var "$WRITE_ENV_FILE" "HM_HOST_HOSTNAME" "$HOSTNAME_VAL" && \
           update_env_var "$WRITE_ENV_FILE" "HM_FIXER_UID" "$FIXER_UID" && \
           update_env_var "$WRITE_ENV_FILE" "HM_FIXER_GID" "$FIXER_GID"; then
            log "WROTE: $WRITE_ENV_FILE (updated HM_CRON_HOST_UID, HM_CRON_HOST_GID, HM_HOST_HOSTNAME, HM_FIXER_UID, HM_FIXER_GID)"
        else
            log "ERROR: failed to write env file"
            exit 1
        fi
    fi
fi

if [[ $CHECK_ONLY -eq 1 ]]; then
    log "DONE (check mode): no changes applied."
else
    log "DONE: host setup complete."
fi
