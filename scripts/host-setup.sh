#!/usr/bin/env bash
#
# scripts/host-setup.sh — one-time host setup for homelab-monitor.
#
# STAGE-002-007 (cron discovery): creates the `homelab-monitor` host user,
# adds it to the `crontab` group (Debian/Ubuntu), and grants read ACLs on
# /var/spool/cron/crontabs and its contents so the user (and therefore the
# container) can traverse and read all per-user crontabs. ACLs are set in three
# parts: directory traversal (rx), per-existing-file (r), and default for new
# files (r).
#
# STAGE-002-007A Fix B: `crontab -e` writes a temp file and rename()s it into
# the spool directory, producing a fresh 0600 file that does NOT inherit the
# directory's default ACL — so the container loses read access on every edit.
# To fix this permanently, this script also installs a systemd `.path` unit
# that watches /var/spool/cron/crontabs and re-applies the read ACLs on every
# change. Because of that watcher, this script only needs to be run ONCE per
# machine, ever — subsequent `crontab -e` edits are handled automatically.
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
readonly REFRESH_SCRIPT_SRC="$SCRIPT_DIR/refresh-crontab-acl.sh"
readonly REFRESH_SCRIPT_DEST="/usr/local/sbin/refresh-crontab-acl.sh"
readonly SYSTEMD_DEST_DIR="/etc/systemd/system"
readonly PATH_UNIT="homelab-monitor-crontab-acl.path"
readonly SERVICE_UNIT="homelab-monitor-crontab-acl.service"

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

# --- 3. Grant read ACL on /var/spool/cron/crontabs ---
# The container needs THREE kinds of ACLs:
#   1. Directory access (rx) — so the user can traverse into the directory
#   2. Directory default (r) — so NEW crontab files inherit read access
#   3. Per-existing-file (r) — for every existing crontab file in that directory
if [[ -d "$CRONTAB_DIR" ]]; then
    if ! command -v setfacl >/dev/null 2>&1; then
        log "WARN: setfacl not installed; install 'acl' package for per-user crontab access"
    else
        # 3a. Directory traversal ACL (rx) — required before the user can even list the dir
        dir_acl=$(getfacl --absolute-names "$CRONTAB_DIR" 2>/dev/null | grep "user:$USERNAME:.*r.*x" || true)
        if [[ -n "$dir_acl" ]]; then
            log "OK: directory traversal ACL already set on $CRONTAB_DIR"
        else
            do_or_check "setfacl -m 'u:$USERNAME:rx' '$CRONTAB_DIR'"
        fi

        # 3b. Apply ACL to every existing entry
        shopt -s nullglob
        for f in "$CRONTAB_DIR"/*; do
            [[ -e "$f" ]] || continue  # handle glob non-match
            current_acl=$(getfacl --absolute-names "$f" 2>/dev/null | grep "user:$USERNAME:" || true)
            if [[ -n "$current_acl" ]] && [[ "$current_acl" == *"r--"* ]]; then
                log "OK: ACL already set on $f"
            else
                do_or_check "setfacl -m 'u:$USERNAME:r' '$f'"
            fi
        done
        shopt -u nullglob

        # 3c. Set default ACL so newly-created crontabs are also readable
        current_default=$(getfacl --absolute-names "$CRONTAB_DIR" 2>/dev/null | grep "default:user:$USERNAME:" || true)
        if [[ -n "$current_default" ]] && [[ "$current_default" == *"r--"* ]]; then
            log "OK: default ACL already set on $CRONTAB_DIR"
        else
            do_or_check "setfacl -d -m 'u:$USERNAME:r' '$CRONTAB_DIR'"
        fi
    fi
else
    log "WARN: $CRONTAB_DIR not found; user crontabs cannot be discovered until cron is installed"
fi

# --- 4. Install systemd path unit that auto-refreshes the ACL ---
# `crontab -e` rename()s a new 0600 file into CRONTAB_DIR; that file does NOT
# inherit the directory's default ACL, so the container loses read access on
# every edit. The .path unit below watches CRONTAB_DIR and re-applies the ACL
# on every change, making this whole script a one-time-per-machine operation.
if ! command -v systemctl >/dev/null 2>&1; then
    log "WARN: systemctl not found; non-systemd host. Skipping ACL-watcher install."
    log "WARN: re-run host-setup.sh manually after each 'crontab -e' on this host."
else
    # 4a. Install the refresh script to a stable absolute path.
    if [[ -f "$REFRESH_SCRIPT_DEST" ]] \
        && cmp -s "$REFRESH_SCRIPT_SRC" "$REFRESH_SCRIPT_DEST"; then
        log "OK: $REFRESH_SCRIPT_DEST already up to date"
    else
        do_or_check "install -m 0755 '$REFRESH_SCRIPT_SRC' '$REFRESH_SCRIPT_DEST'"
    fi

    # 4b. Install the two unit files.
    for unit in "$SERVICE_UNIT" "$PATH_UNIT"; do
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

    # 4d. Enable + start the .path unit (idempotent — enable --now is safe to
    #     re-run; it just ensures the symlink exists and the watcher is active).
    #     Skip if CRONTAB_DIR does not exist yet — the unit would immediately
    #     error because PathChanged= targets a non-existent directory. Install
    #     cron(d) first, then re-run host-setup.sh to activate the watcher.
    if [[ ! -d "$CRONTAB_DIR" ]]; then
        log "WARN: $PATH_UNIT not enabled — $CRONTAB_DIR does not exist (install cron(d) first, then re-run host-setup.sh)"
    elif systemctl is-enabled "$PATH_UNIT" >/dev/null 2>&1 \
        && systemctl is-active "$PATH_UNIT" >/dev/null 2>&1; then
        log "OK: $PATH_UNIT already enabled and active"
    else
        do_or_check "systemctl enable --now '$PATH_UNIT'"
    fi

    # 4e. Initial ACL apply so existing files are covered immediately (the
    #     watcher only fires on FUTURE changes). Section 3 above already
    #     applied ACLs to existing files; this re-runs the installed refresh
    #     script for parity with what the watcher will do on every change.
    if [[ -x "$REFRESH_SCRIPT_DEST" ]]; then
        do_or_check "'$REFRESH_SCRIPT_DEST'"
    fi
fi

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

Then restart the monitor: docker compose up -d --force-recreate monitor
EOF

# Handle env file writing if requested
if [[ -n "$WRITE_ENV_FILE" ]]; then
    if [[ $CHECK_ONLY -eq 1 ]]; then
        log "CHECK: would write to $WRITE_ENV_FILE:"
        log "  HM_CRON_HOST_UID=$UID_VAL"
        log "  HM_CRON_HOST_GID=$GID_VAL"
        log "  HM_HOST_HOSTNAME=$HOSTNAME_VAL"
    else
        if update_env_var "$WRITE_ENV_FILE" "HM_CRON_HOST_UID" "$UID_VAL" && \
           update_env_var "$WRITE_ENV_FILE" "HM_CRON_HOST_GID" "$GID_VAL" && \
           update_env_var "$WRITE_ENV_FILE" "HM_HOST_HOSTNAME" "$HOSTNAME_VAL"; then
            log "WROTE: $WRITE_ENV_FILE (updated HM_CRON_HOST_UID, HM_CRON_HOST_GID, HM_HOST_HOSTNAME)"
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
