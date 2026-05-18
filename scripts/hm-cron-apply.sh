#!/usr/bin/env bash
#
# scripts/hm-cron-apply.sh — host-side cron-apply executor (STAGE-002-009).
#
# Installed to /usr/local/sbin/hm-cron-apply and run by the
# homelab-monitor-cron-apply.service systemd oneshot unit, which is triggered
# by the homelab-monitor-cron-apply.path watcher on the request directory.
#
# The monitor container (uid 995, ZERO host-write capability) writes a request
# JSON to <IPC>/requests/<id>.json. The request carries a LIST of operations.
# This script processes every pending request, applies the operation list
# ATOMICALLY (all-or-nothing with rollback), and writes <IPC>/results/<id>.json.
#
# Operations:
#   * wrap-crontab         — rewrite an already-present crontab line.
#   * write-wrapper-script — write the wrapper script to a FIXED host path.
#   * write-token          — write the heartbeat token to a FIXED host path.
#   * unwrap-crontab       — (future, STAGE-002-009A) strip the wrapper prefix.
#
# LOAD-BEARING SECURITY CONTROL:
#   * wrap-crontab: the target crontab path MUST be /etc/crontab,
#     /etc/cron.d/<name>, or a user crontab under /var/spool/cron/crontabs/<user>;
#     the request's old_line MUST exist VERBATIM in that file; the replacement
#     line is RE-DERIVED here from old_line + command — the request carries NO
#     caller-supplied new_line; wrap refuses an already-wrapped line.
#   * write-wrapper-script / write-token: the request carries ONLY the file
#     CONTENT. The destination is a FIXED constant in this script
#     (WRAPPER_SCRIPT_PATH / TOKEN_PATH). The script REFUSES any request that
#     supplies a path/target field or otherwise tries to redirect the write.
# Result: even a fully-compromised monitor can at most (a) wrap an
# already-discovered cron line, or (b) overwrite exactly the wrapper script
# and the token file with chosen content. It cannot inject new cron entries,
# choose write destinations, or run arbitrary commands.
#
# Runs as root (the systemd service runs as root, hardened). Idempotent per
# request id (a result file already present → request is skipped + deleted).
#
set -euo pipefail

readonly IPC_DIR="${HM_CRON_APPLY_IPC_DIR:-/var/lib/homelab-monitor/cron-apply}"
readonly REQUESTS_DIR="$IPC_DIR/requests"
readonly RESULTS_DIR="$IPC_DIR/results"
# APPLY_ROOT is a TEST-ONLY prefix (default "/"); the harness sets it so the
# script resolves crontab + fixed paths under a tmp tree. Unset in production.
readonly APPLY_ROOT="${HM_CRON_APPLY_ROOT:-/}"
readonly _ROOT="${APPLY_ROOT%/}"
readonly SPOOL_DIR="${_ROOT}/var/spool/cron/crontabs"
readonly SYSTEM_CRONTAB="${_ROOT}/etc/crontab"
readonly CRON_D_DIR="${_ROOT}/etc/cron.d"
# FIXED destinations for the file-write operations. The request NEVER carries
# a path — these are the only places write-wrapper-script / write-token write.
readonly WRAPPER_SCRIPT_PATH="${_ROOT}/usr/local/bin/cron-with-heartbeat.sh"
readonly TOKEN_DIR="${_ROOT}/etc/homelab-monitor"
readonly TOKEN_PATH="${TOKEN_DIR}/heartbeat.token"
readonly WRAPPER_PREFIX="/usr/local/bin/cron-with-heartbeat.sh -- "
readonly SCHEMA_VERSION=1
readonly RESULT_RETENTION_SECONDS=3600

log() { printf '[hm-cron-apply] %s\n' "$*"; }

# --- JSON helpers (require jq; fail loud if missing) ------------------------
if ! command -v jq >/dev/null 2>&1; then
    log "FATAL: jq not installed; cannot process cron-apply requests"
    exit 1
fi

# Write a result file atomically. $1=id $2=status $3=error_code(or empty) $4=message
write_result() {
    local id="$1" status="$2" error_code="$3" message="$4"
    local tmp out
    out="$RESULTS_DIR/$id.json"
    tmp="$RESULTS_DIR/.$id.json.tmp"
    if [[ -z "$error_code" ]]; then
        jq -n --arg id "$id" --arg status "$status" --arg msg "$message" \
            '{id:$id, status:$status, error_code:null, message:$msg}' > "$tmp"
    else
        jq -n --arg id "$id" --arg status "$status" --arg ec "$error_code" --arg msg "$message" \
            '{id:$id, status:$status, error_code:$ec, message:$msg}' > "$tmp"
    fi
    mv -f "$tmp" "$out"
    log "result $id: $status ${error_code:-} $message"
}

# Resolve a wrap-crontab target_crontab → an absolute file path, or empty
# string if invalid. Honors APPLY_ROOT via the SPOOL_DIR / SYSTEM_CRONTAB /
# CRON_D_DIR constants (those already include the test-only root prefix).
resolve_target() {
    local target="$1"
    case "$target" in
        /etc/crontab)
            printf '%s' "$SYSTEM_CRONTAB" ;;
        /etc/cron.d/*)
            local name="${target#/etc/cron.d/}"
            # Reject path traversal / nested paths.
            if [[ -z "$name" || "$name" == *"/"* || "$name" == *".."* ]]; then
                printf '' ; return
            fi
            printf '%s' "$CRON_D_DIR/$name" ;;
        crontab:*)
            local user="${target#crontab:}"
            if [[ -z "$user" || "$user" == *"/"* || "$user" == *".."* ]]; then
                printf '' ; return
            fi
            printf '%s' "$SPOOL_DIR/$user" ;;
        *)
            printf '' ;;
    esac
}

# --- Rollback bookkeeping ----------------------------------------------------
# As each operation in a request is applied, we record how to undo it. On any
# later failure we replay the undo log in reverse and report status=error.
#   - wrap-crontab: snapshot the crontab file to a temp before the write;
#                   undo = restore the snapshot (preserving mode + ownership).
#   - write-wrapper-script / write-token: if the file did NOT pre-exist,
#                   undo = delete it. If it DID pre-exist, snapshot it first;
#                   undo = restore the snapshot. (A re-install overwrites an
#                   existing wrapper/token; rollback must restore, not delete.)
ROLLBACK_LOG=()   # each entry: "restore <snapshot> <target> <mode>" | "delete <target>"

rollback_all() {
    local i entry
    for (( i=${#ROLLBACK_LOG[@]}-1; i>=0; i-- )); do
        entry="${ROLLBACK_LOG[i]}"
        # shellcheck disable=SC2086
        set -- $entry
        case "$1" in
            restore) mv -f "$2" "$3" 2>/dev/null || true ;;
            delete)  rm -f "$2" 2>/dev/null || true ;;
        esac
    done
    ROLLBACK_LOG=()
}

# Process a single request file. $1=path to requests/<id>.json
process_request() {
    local req_file="$1"
    local id base
    base="$(basename "$req_file")"
    id="${base%.json}"
    ROLLBACK_LOG=()

    # Idempotency: if a result already exists, this request was handled.
    if [[ -f "$RESULTS_DIR/$id.json" ]]; then
        rm -f "$req_file"
        return
    fi

    # --- Parse + validate the request envelope ----------------------------
    local raw version op_count
    if ! raw="$(cat "$req_file" 2>/dev/null)" || ! jq -e . >/dev/null 2>&1 <<<"$raw"; then
        write_result "$id" "error" "bad_request" "malformed JSON"
        rm -f "$req_file"; return
    fi
    version="$(jq -r '.schema_version // empty' <<<"$raw")"
    if [[ "$version" != "$SCHEMA_VERSION" ]]; then
        write_result "$id" "error" "bad_request" "unsupported schema_version: $version"
        rm -f "$req_file"; return
    fi
    if ! jq -e '.operations | type == "array" and length > 0' >/dev/null 2>&1 <<<"$raw"; then
        write_result "$id" "error" "bad_request" "operations must be a non-empty array"
        rm -f "$req_file"; return
    fi
    op_count="$(jq -r '.operations | length' <<<"$raw")"

    # --- Apply each operation in order; abort + roll back on first failure -
    local i op_json op_kind err_code err_msg
    err_code=""; err_msg=""
    for (( i=0; i<op_count; i++ )); do
        op_json="$(jq -c ".operations[$i]" <<<"$raw")"
        op_kind="$(jq -r '.operation // empty' <<<"$op_json")"
        case "$op_kind" in
            wrap-crontab)
                apply_wrap_crontab "$op_json" || { err_code="$RC_CODE"; err_msg="$RC_MSG"; break; } ;;
            write-wrapper-script)
                apply_write_file "$op_json" "$WRAPPER_SCRIPT_PATH" 0755 || { err_code="$RC_CODE"; err_msg="$RC_MSG"; break; } ;;
            write-token)
                apply_write_file "$op_json" "$TOKEN_PATH" 0644 || { err_code="$RC_CODE"; err_msg="$RC_MSG"; break; } ;;
            *)
                err_code="bad_request"; err_msg="unknown operation: $op_kind"; break ;;
        esac
    done

    if [[ -n "$err_code" ]]; then
        rollback_all
        write_result "$id" "error" "$err_code" "$err_msg (rolled back)"
        rm -f "$req_file"; return
    fi

    write_result "$id" "ok" "" "applied $op_count operations"
    rm -f "$req_file"
}

# Operation helpers set RC_CODE + RC_MSG and return non-zero on failure.
RC_CODE=""; RC_MSG=""
_fail() { RC_CODE="$1"; RC_MSG="$2"; return 1; }

# apply_wrap_crontab <operation-json> — rewrite one crontab line.
apply_wrap_crontab() {
    local op="$1"
    local target old_line command file new_line snap supplied_new_line
    target="$(jq -r '.target_crontab // empty' <<<"$op")"
    old_line="$(jq -r '.old_line // empty' <<<"$op")"
    command="$(jq -r '.command // empty' <<<"$op")"
    supplied_new_line="$(jq -r '.new_line // empty' <<<"$op")"

    [[ -n "$old_line" ]] || { _fail "bad_request" "empty old_line"; return 1; }
    [[ -n "$command"  ]] || { _fail "bad_request" "empty command"; return 1; }

    file="$(resolve_target "$target")"
    [[ -n "$file" ]] || { _fail "bad_path" "disallowed target: $target"; return 1; }
    [[ -f "$file" ]] || { _fail "crontab_missing" "crontab not found: $file"; return 1; }

    case "$old_line" in
        *"$WRAPPER_PREFIX"*)
            _fail "already_wrapped" "line already wrapped"; return 1 ;;
    esac
    # Re-derive: replace the LAST occurrence of `command` in `old_line` with
    # WRAPPER_PREFIX + command. Mirrors install.py's _rewrite_line rfind+insert.
    # This RE-DERIVED value is authoritative — the request's new_line (if any)
    # is only a cross-check below.
    new_line="$(replace_last "$old_line" "$command" "${WRAPPER_PREFIX}${command}")" \
        || { _fail "bad_request" "command not found in old_line"; return 1; }

    # Defense-in-depth cross-check: if the request supplied a new_line, it MUST
    # equal the executor's independently re-derived value. A mismatch means the
    # monitor and executor disagree about the rewrite — reject rather than
    # silently applying the (authoritative) re-derived line.
    if [[ -n "$supplied_new_line" && "$supplied_new_line" != "$new_line" ]]; then
        _fail "bad_request" "supplied new_line disagrees with re-derived new_line"
        return 1
    fi

    # old_line must exist VERBATIM as a full line in the file.
    grep -qxF -- "$old_line" "$file" \
        || { _fail "line_not_found" "old_line not present verbatim"; return 1; }

    # Snapshot for rollback BEFORE the write.
    snap="$(mktemp "${RESULTS_DIR}/.snap.XXXXXX")" || { _fail "write_failed" "mktemp failed"; return 1; }
    cp -p "$file" "$snap" || { rm -f "$snap"; _fail "write_failed" "snapshot failed"; return 1; }

    if ! apply_line_replace "$file" "$old_line" "$new_line"; then
        rm -f "$snap"
        _fail "write_failed" "failed to write $file"; return 1
    fi
    ROLLBACK_LOG+=("restore $snap $file -")
    return 0
}

# apply_write_file <operation-json> <fixed-dest-path> <mode>
# Writes the operation's `content` to the FIXED dest. Refuses if the request
# tries to supply a path/target field (defense-in-depth — the dest is never
# caller-controlled).
apply_write_file() {
    local op="$1" dest="$2" mode="$3"
    local content snap parent
    # Defense-in-depth: a file-write op must NOT carry a path/target.
    if jq -e 'has("path") or has("target") or has("target_crontab")' >/dev/null 2>&1 <<<"$op"; then
        _fail "bad_request" "file-write op must not carry a destination path"; return 1
    fi
    if ! jq -e 'has("content")' >/dev/null 2>&1 <<<"$op"; then
        _fail "bad_request" "file-write op missing content"; return 1
    fi
    content="$(jq -r '.content' <<<"$op")"

    parent="$(dirname "$dest")"
    mkdir -p "$parent" || { _fail "write_failed" "mkdir $parent failed"; return 1; }

    if [[ -e "$dest" ]]; then
        # Snapshot existing file for rollback (re-install overwrites).
        snap="$(mktemp "${RESULTS_DIR}/.snap.XXXXXX")" || { _fail "write_failed" "mktemp failed"; return 1; }
        cp -p "$dest" "$snap" || { rm -f "$snap"; _fail "write_failed" "snapshot failed"; return 1; }
        ROLLBACK_LOG+=("restore $snap $dest -")
    else
        ROLLBACK_LOG+=("delete $dest")
    fi

    local tmp
    tmp="$(mktemp "${dest}.hmtmp.XXXXXX")" || { _fail "write_failed" "mktemp failed"; return 1; }
    printf '%s' "$content" > "$tmp" || { rm -f "$tmp"; _fail "write_failed" "write failed"; return 1; }
    chmod "$mode" "$tmp"            || { rm -f "$tmp"; _fail "write_failed" "chmod failed"; return 1; }
    mv -f "$tmp" "$dest"            || { rm -f "$tmp"; _fail "write_failed" "mv failed"; return 1; }
    return 0
}

# replace_last <haystack> <needle> <replacement> → echo result; non-zero if
# the needle is absent. Splices the replacement in at the LAST occurrence of
# the needle — mirrors Python str.rfind()+slice, the same rule install.py's
# _rewrite_line uses, guaranteeing byte-identical wrapped lines.
replace_last() {
    local h="$1" n="$2" r="$3" idx=-1 i
    local hlen=${#h} nlen=${#n}
    if (( nlen == 0 )); then printf '' ; return 1; fi
    for (( i=0; i<=hlen-nlen; i++ )); do
        if [[ "${h:i:nlen}" == "$n" ]]; then idx=$i; fi
    done
    if (( idx < 0 )); then printf '' ; return 1; fi
    printf '%s%s%s' "${h:0:idx}" "$r" "${h:idx+nlen}"
}

# apply_line_replace <file> <old_line> <new_line>
# Replace exactly the one matching line; preserve mode + ownership.
apply_line_replace() {
    local file="$1" old_line="$2" new_line="$3"
    local tmp owner_uid owner_gid mode
    tmp="$(mktemp "${file}.hmtmp.XXXXXX")" || return 1
    # Read original metadata.
    owner_uid="$(stat -c %u "$file")"
    owner_gid="$(stat -c %g "$file")"
    case "$file" in
        /var/spool/cron/crontabs/*) mode=0600 ;;   # vixie-cron requires 0600
        *) mode="$(stat -c %a "$file")" ;;          # /etc/crontab, /etc/cron.d/* keep mode
    esac
    # KNOWN LIMITATION: byte-identical duplicate crontab lines produce an
    # identical fingerprint, so the registry holds a single row for them and
    # this rewrite wraps only the FIRST occurrence. The duplicate line is left
    # unwrapped. This is acceptable: identical cron lines are a crontab
    # misconfiguration, and discovery converges the registry on the wrapped
    # form on the next scan regardless.
    # Rewrite: replace only the first verbatim full-line match.
    # awk with exact string compare; replace at most once.
    awk -v old="$old_line" -v new="$new_line" '
        BEGIN { done=0 }
        { if (!done && $0 == old) { print new; done=1 } else { print } }
    ' "$file" > "$tmp" || { rm -f "$tmp"; return 1; }
    # Re-apply the crontab file's original owner uid/gid to the temp file so
    # the rename preserves ownership. Under the test harness (APPLY_ROOT set)
    # owner is the test user, so this is effectively a no-op; in production it
    # restores e.g. the crontab's per-user ownership.
    chown "$owner_uid:$owner_gid" "$tmp" || { rm -f "$tmp"; return 1; }
    chmod "$mode" "$tmp" || { rm -f "$tmp"; return 1; }
    mv -f "$tmp" "$file" || { rm -f "$tmp"; return 1; }
    return 0
}

# --- main -------------------------------------------------------------------
if [[ ! -d "$REQUESTS_DIR" ]]; then
    log "WARN: $REQUESTS_DIR does not exist; nothing to do."
    exit 0
fi
mkdir -p "$RESULTS_DIR"

# Sweep any leftover temp files from an interrupted previous run, mirroring
# hm-crontab-snapshot.sh's temp sweep. mktemp-created files are:
#   * <dest>.hmtmp.* beside the wrapper script + token (fixed paths)
#   * .snap.* rollback snapshots in RESULTS_DIR
shopt -s nullglob
for stray in \
    "$RESULTS_DIR"/.snap.* \
    "${WRAPPER_SCRIPT_PATH}".hmtmp.* \
    "${TOKEN_PATH}".hmtmp.*; do
    [[ -e "$stray" ]] || continue
    rm -f "$stray" && log "swept stray temp file: $stray"
done
shopt -u nullglob

shopt -s nullglob
for req in "$REQUESTS_DIR"/*.json; do
    [[ -e "$req" ]] || continue
    process_request "$req" || log "WARN: process_request failed for $req"
done
shopt -u nullglob

# --- prune old result files -------------------------------------------------
now="$(date +%s)"
shopt -s nullglob
for res in "$RESULTS_DIR"/*.json; do
    [[ -e "$res" ]] || continue
    mtime="$(stat -c %Y "$res")"
    if (( now - mtime > RESULT_RETENTION_SECONDS )); then
        rm -f "$res"
    fi
done
shopt -u nullglob

log "OK: cron-apply pass complete."
