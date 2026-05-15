#!/usr/bin/env bash
#
# scripts/refresh-crontab-acl.sh — re-apply homelab-monitor read ACLs on the
# host crontab spool directory and every file in it.
#
# STAGE-002-007A Fix B: `crontab -e` writes a temp file and rename()s it into
# /var/spool/cron/crontabs/, producing a fresh 0600 file that does NOT inherit
# the directory's default ACL. The homelab-monitor-crontab-acl.path systemd
# unit watches that directory and runs this script on every change, so the
# monitor container never loses read access to a crontab.
#
# Idempotent and safe: re-running is a no-op; an empty directory or a file
# vanishing mid-loop is handled gracefully. Intended to be invoked by the
# homelab-monitor-crontab-acl.service oneshot unit, but also runnable by hand.
#
set -euo pipefail

readonly USERNAME="homelab-monitor"
readonly CRONTAB_DIR="/var/spool/cron/crontabs"

log() { printf '[refresh-crontab-acl] %s\n' "$*"; }

if ! command -v setfacl >/dev/null 2>&1; then
    log "WARN: setfacl not installed; cannot refresh crontab ACLs. Install the 'acl' package."
    exit 0
fi

if [[ ! -d "$CRONTAB_DIR" ]]; then
    log "WARN: $CRONTAB_DIR not found; nothing to do."
    exit 0
fi

if ! id "$USERNAME" >/dev/null 2>&1; then
    log "WARN: user $USERNAME does not exist; run host-setup.sh first."
    exit 0
fi

# 1. Directory traversal ACL (rx) — required before the user can list the dir.
setfacl -m "u:$USERNAME:rx" "$CRONTAB_DIR"

# 2. Directory default ACL (r) — so the NEXT crontab created in place inherits.
setfacl -d -m "u:$USERNAME:r" "$CRONTAB_DIR"

# 3. Per-existing-file ACL (r) — covers files that arrived via crontab -e's
#    rename() and therefore did NOT inherit the default ACL.
shopt -s nullglob
for f in "$CRONTAB_DIR"/*; do
    [[ -e "$f" ]] || continue   # file vanished between glob and loop body
    setfacl -m "u:$USERNAME:r" "$f" 2>/dev/null \
        || log "WARN: could not set ACL on $f (file may have vanished)"
done
shopt -u nullglob

log "OK: refreshed read ACLs for $USERNAME on $CRONTAB_DIR"
