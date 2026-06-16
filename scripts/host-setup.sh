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

Then restart the monitor: make compose-up
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
