#!/usr/bin/env bash
#
# scripts/hm-crontab-snapshot.sh — host-side crontab snapshot generator
# (STAGE-002-009, Option B crontab-snapshot fix).
#
# Installed to /usr/local/sbin/hm-crontab-snapshot and run by the
# homelab-monitor-crontab-snapshot.service systemd oneshot unit, which is
# triggered by BOTH:
#   * homelab-monitor-crontab-snapshot.path  — watches /var/spool/cron/crontabs
#   * homelab-monitor-crontab-snapshot.timer — periodic (~300s)
#
# WHY THIS EXISTS:
#   The monitor container (non-root, uid 995) must READ host user crontab
#   files for cron discovery. Those files are mode 0600 owned by each user —
#   unreadable by a non-root process. Granting a read ACL makes vixie-cron
#   reject the crontab as INSECURE MODE and refuse to run it. The cron-
#   sanctioned read path is `crontab -l -u <user>`, which never touches file
#   modes. This script runs as root, reads every user's crontab via that
#   path, and writes the output into a world-readable snapshot directory the
#   container CAN read. The real 0600 spool files are never modified.
#
# WHAT IT DOES:
#   For each user that has a crontab spool file, run `crontab -l -u <user>`
#   and write the verbatim output to <SNAPSHOT_DIR>/<user> (root-owned, 0644).
#   Writes are atomic (temp + rename). Snapshot files for users whose spool
#   file has been removed are pruned. A user whose `crontab -l` is empty or
#   errors is skipped (and any stale snapshot for them pruned).
#
#   The snapshot file content is the RAW `crontab -l` output — a user crontab
#   with no USER column — so the discoverer's existing user-crontab parser
#   consumes it unchanged. The filename is the username; that is the only
#   user->content mapping the discoverer needs.
#
#   Crontab schedules/commands are not secrets (discovery already secret-
#   scrubs commands at storage time); 0644 is intentional so the non-root
#   container can read the snapshot.
#
# Idempotent and safe: re-running is a no-op beyond refreshing file contents;
# an empty spool dir or a file vanishing mid-loop is handled gracefully.
#
set -euo pipefail

readonly USERNAME="homelab-monitor"

# Test-only root prefix (default "/"); the pytest harness sets it so the
# script resolves the spool dir + snapshot dir under a tmp tree. Unset in
# production. Mirrors hm-cron-apply.sh's HM_CRON_APPLY_ROOT.
readonly SNAP_ROOT="${HM_CRON_SNAPSHOT_TEST_ROOT:-/}"
readonly _ROOT="${SNAP_ROOT%/}"
readonly SPOOL_DIR="${_ROOT}/var/spool/cron/crontabs"
readonly SNAPSHOT_DIR="${_ROOT}/var/lib/homelab-monitor/crontab-snapshot"

# The crontab binary. Overridable for tests (a fake `crontab` script on a tmp
# path). In production this is the host's real `crontab`.
readonly CRONTAB_BIN="${HM_CRONTAB_BIN:-crontab}"

log() { printf '[hm-crontab-snapshot] %s\n' "$*"; }

if ! command -v "$CRONTAB_BIN" >/dev/null 2>&1; then
    log "WARN: '$CRONTAB_BIN' not found; cannot snapshot user crontabs."
    exit 0
fi

if [[ ! -d "$SPOOL_DIR" ]]; then
    log "WARN: $SPOOL_DIR not found; nothing to snapshot."
    exit 0
fi

mkdir -p "$SNAPSHOT_DIR"
chmod 0755 "$SNAPSHOT_DIR"

# --- 1. Collect the current set of spool users -----------------------------
declare -A CURRENT_USERS=()
shopt -s nullglob
for f in "$SPOOL_DIR"/*; do
    [[ -e "$f" ]] || continue          # glob non-match guard
    [[ -f "$f" ]] || continue          # skip subdirectories
    base="$(basename "$f")"
    case "$base" in
        .*) continue ;;                # skip dotfiles
    esac
    CURRENT_USERS["$base"]=1
done
shopt -u nullglob

# --- 2. Snapshot each user's crontab via the cron-sanctioned read path -----
for user in "${!CURRENT_USERS[@]}"; do
    content=""
    if ! content="$("$CRONTAB_BIN" -l -u "$user" 2>/dev/null)"; then
        # `crontab -l` failed (e.g. "no crontab for <user>"). Skip; the prune
        # pass below removes any stale snapshot for this user.
        log "skip $user: crontab -l returned non-zero"
        continue
    fi
    if [[ -z "$content" ]]; then
        log "skip $user: empty crontab"
        continue
    fi
    # Atomic write: temp file in the snapshot dir, then rename into place so
    # the discoverer never reads a partial file.
    tmp="$(mktemp "${SNAPSHOT_DIR}/.${user}.tmp.XXXXXX")" || {
        log "WARN: mktemp failed for $user; skipping"
        continue
    }
    printf '%s\n' "$content" > "$tmp" || {
        rm -f "$tmp"; log "WARN: write failed for $user; skipping"; continue
    }
    chmod 0644 "$tmp" || { rm -f "$tmp"; log "WARN: chmod failed for $user"; continue; }
    mv -f "$tmp" "$SNAPSHOT_DIR/$user" || {
        rm -f "$tmp"; log "WARN: rename failed for $user"; continue
    }
    log "snapshot $user: ok"
done

# --- 3. Prune stale snapshot files (users whose spool file is gone) --------
shopt -s nullglob
for snap in "$SNAPSHOT_DIR"/*; do
    [[ -e "$snap" ]] || continue
    [[ -f "$snap" ]] || continue
    base="$(basename "$snap")"
    case "$base" in
        .*) continue ;;                # never prune our own temp/dotfiles
    esac
    if [[ -z "${CURRENT_USERS[$base]+x}" ]]; then
        rm -f "$snap" && log "prune stale snapshot: $base"
    fi
done
shopt -u nullglob

# Also sweep any leftover temp files from an interrupted previous run.
shopt -s nullglob
for stray in "$SNAPSHOT_DIR"/.*.tmp.*; do
    [[ -e "$stray" ]] || continue
    rm -f "$stray"
done
shopt -u nullglob

log "OK: crontab snapshot pass complete."
