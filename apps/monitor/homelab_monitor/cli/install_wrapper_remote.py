"""Standalone remote CLI for installing/uninstalling the heartbeat wrapper on a foreign host.

This module is distributed as a standalone script (EPIC-019 will PyInstaller it).
It imports ONLY stdlib — NO homelab_monitor.kernel.* imports — so it runs on any
host with just python3.

It reproduces the fingerprint algorithm and wrapper invocation format in isolation,
with comments pointing to kernel modules as the source of truth.

Usage:
    python3 install_wrapper_remote.py \
        [--monitor-url URL] [--token TOKEN] [--crontab PATH] [--confirm] [--uninstall]
    # or via environment variables:
    HM_MONITOR_URL=http://... HM_HEARTBEAT_TOKEN=... \
        python3 install_wrapper_remote.py [--crontab PATH] [--confirm] [--uninstall]
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import hashlib
import json
import os
import socket
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

# ===== Constants (must match kernel/cron/wrapper_constants.py) =====
WRAPPER_PATH = "/usr/local/bin/cron-with-heartbeat.sh"
WRAPPER_SEPARATOR = "--"
WRAPPER_INVOCATION_PREFIX = f"{WRAPPER_PATH} {WRAPPER_SEPARATOR} "
TOKEN_FILE_PATH = "/etc/homelab-monitor/heartbeat.token"
TOKEN_FILE_DIR = "/etc/homelab-monitor"

# Minimum whitespace-separated fields a crontab job line must have:
# 5 schedule fields + (user +) command.
_MIN_CRONTAB_FIELDS = 6
# Inclusive-exclusive bounds of the HTTP 2xx success range.
_HTTP_OK_MIN = 200
_HTTP_OK_MAX = 300


def unwrap_command(command: str) -> str:
    """Strip the wrapper prefix if present. Source of truth:
    kernel/cron/wrapper_constants.py unwrap_command()."""
    if command.startswith(WRAPPER_INVOCATION_PREFIX):
        return command[len(WRAPPER_INVOCATION_PREFIX) :]
    return command


def compute_fingerprint(
    host: str,
    source_path: str,
    schedule: str,
    command: str,
) -> str:
    """Compute fingerprint (SHA256 of canonical JSON).

    Source of truth: kernel/cron/fingerprint.py compute_fingerprint().
    """
    data = {
        "host": host,
        "source_path": source_path,
        "schedule": schedule,
        "command": command,
    }
    canonical_json = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def fetch_wrapper_template(monitor_url: str, token: str) -> str:
    """Fetch the canonical wrapper-script template from the monitor API.

    Source of truth: data/cron-with-heartbeat.sh.tmpl, served by
    GET /api/crons/wrapper-template. The standalone installer NEVER embeds
    its own template copy — it fetches and substitutes, so the produced
    wrapper is byte-identical to install.py:_build_wrapper_content().
    """
    url = f"{monitor_url}/api/crons/wrapper-template"
    req = Request(
        url,
        headers={"Authorization": f"Bearer {token}"},
        method="GET",
    )
    with urlopen(req, timeout=10) as resp:
        if not (_HTTP_OK_MIN <= resp.status < _HTTP_OK_MAX):
            raise RuntimeError(f"template fetch returned HTTP {resp.status}")
        return resp.read().decode("utf-8")


def _atomic_write_text(path: Path, content: str) -> None:
    """Atomically write text to a file using temp+rename pattern."""
    fd, temp_path = tempfile.mkstemp(dir=path.parent, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(temp_path, path)
    except Exception:
        with contextlib.suppress(Exception):
            os.unlink(temp_path)
        raise


def _rmdir_if_empty(path: Path) -> None:
    """Remove a directory if it is empty."""
    with contextlib.suppress(OSError):
        # Directory not empty or already missing — best-effort, ignore
        path.rmdir()


def parse_crontab_lines(content: str) -> list[tuple[int, str]]:
    """Parse crontab file, return list of (line_index, line) for non-comment lines."""
    lines: list[tuple[int, str]] = []
    for idx, line in enumerate(content.splitlines(keepends=False)):
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            lines.append((idx, line))
    return lines


def _build_arg_parser() -> argparse.ArgumentParser:
    """Construct the CLI argument parser."""
    parser = argparse.ArgumentParser(
        description="Install heartbeat wrapper on a remote/local cron",
    )
    parser.add_argument(
        "--monitor-url",
        default=os.environ.get("HM_MONITOR_URL", ""),
        help="Monitor base URL (env: HM_MONITOR_URL)",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("HM_HEARTBEAT_TOKEN", ""),
        help="Heartbeat token (env: HM_HEARTBEAT_TOKEN)",
    )
    parser.add_argument(
        "--crontab",
        default="",
        help="Crontab file path or 'crontab:<user>' (prompts if not set)",
    )
    parser.add_argument(
        "--line",
        type=int,
        default=0,
        help="Line number (1-indexed) to install wrapper for (prompts if not set)",
    )
    parser.add_argument(
        "--host",
        default=socket.gethostname(),
        help="Hostname (defaults to socket.gethostname())",
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Actually write files (omit for dry-run preview)",
    )
    parser.add_argument(
        "--uninstall",
        action="store_true",
        help="Remove the wrapper from the selected line instead of installing it",
    )
    return parser


def _resolve_crontab_file(crontab_spec: str) -> Path:
    """Map a crontab spec ('/etc/crontab', 'crontab:<user>', or a path) to a Path."""
    if crontab_spec == "/etc/crontab":
        return Path("/etc/crontab")
    if crontab_spec.startswith("crontab:"):
        user = crontab_spec[len("crontab:") :]
        return Path("/var/spool/cron/crontabs") / user
    return Path(crontab_spec)


def _parse_job_line(old_line: str, crontab_spec: str) -> tuple[str, str] | None:
    """Extract (schedule, command) from a crontab job line.

    Returns None if the line cannot be parsed; the caller prints the error.

    Mirrors kernel/cron/cron_parser.py _parse_fielded_line():
    - USER_CRONTAB:           `m h dom mon dow CMD...`      (5 schedule + command)
    - SYSTEM_WITH_USER_FIELD: `m h dom mon dow USER CMD...` (5 schedule + user + command)
    """
    # Split off 5 schedule fields, a possible user field, and keep the command
    # remainder intact: maxsplit=6 yields at most 7 parts (parts[6] is the rest).
    parts = old_line.split(None, _MIN_CRONTAB_FIELDS)
    if len(parts) < _MIN_CRONTAB_FIELDS:
        return None

    schedule = " ".join(parts[:5])
    user_or_cmd = parts[5]

    # Heuristic: a username-looking field on /etc/crontab means a user column.
    is_system = (
        crontab_spec == "/etc/crontab"
        and user_or_cmd
        and not any(c in user_or_cmd for c in ["$", "/", ";"])
    )
    if is_system:
        # System crontab: parts[5] is the user; the command is the rest.
        if len(parts) <= _MIN_CRONTAB_FIELDS:
            return None
        command = " ".join(parts[_MIN_CRONTAB_FIELDS:])
    else:
        # User crontab (or system line whose 6th field is clearly a command):
        # the command is everything after the 5 schedule fields.
        command = " ".join(parts[5:])
    return schedule, command


def _write_token_file(
    token_file: Path, token_dir: Path, token: str, undo: list[Callable[[], None]]
) -> None:
    """Write token file and record undo action."""
    token_dir_preexisted = token_dir.exists()
    token_dir.mkdir(parents=True, exist_ok=True, mode=0o755)
    if not token_dir_preexisted:
        undo.append(lambda: _rmdir_if_empty(token_dir))
    token_preexisted = token_file.exists()
    token_snapshot = token_file.read_text(encoding="utf-8") if token_preexisted else None
    token_file.write_text(token, encoding="utf-8")
    # 0644: cron-runner-readable, matches hm-cron-apply.sh (write-token mode).
    token_file.chmod(0o644)
    if token_preexisted:
        assert token_snapshot is not None
        _tsnap = token_snapshot

        def _undo_token_restore(_f: Path = token_file, _s: str = _tsnap) -> None:
            _f.write_text(_s, encoding="utf-8")

        undo.append(_undo_token_restore)
    else:
        undo.append(lambda: token_file.unlink(missing_ok=True))
    print(f"Wrote token to {token_file}")


def _register_with_monitor(
    monitor_url: str, fingerprint: str, token: str, reg_payload: dict[str, object]
) -> None:
    """Register with monitor (best-effort; errors are logged but not raised)."""
    try:
        reg_url = f"{monitor_url}/api/hb/{fingerprint}/register"
        req = Request(
            reg_url,
            data=json.dumps(reg_payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urlopen(req, timeout=10) as resp:
            if not (_HTTP_OK_MIN <= resp.status < _HTTP_OK_MAX):
                print(f"WARNING: registration returned {resp.status}", file=sys.stderr)
        print("Registered with monitor")
    except HTTPError as exc:
        print(f"WARNING: registration failed: {exc}", file=sys.stderr)
    except Exception as exc:
        print(f"WARNING: registration error: {exc}", file=sys.stderr)


def _write_files_and_register(  # noqa: PLR0913 -- apply-path: each kwarg is a distinct install input
    *,
    crontab_file: Path,
    crontab_content: str,
    line_index: int,
    new_line: str,
    wrapper_content: str,
    monitor_url: str,
    fingerprint: str,
    token: str,
    reg_payload: dict[str, object],
) -> int:
    """Apply path: write wrapper + token, rewrite crontab, register.

    Implements its own snapshot + rollback (the standalone installer has no
    host-side executor). Mutations are ordered so the crontab rewrite is LAST.
    On ANY failure after the first mutation, every applied mutation is undone
    in reverse: a file that did not pre-exist is deleted; a file that did
    pre-exist is restored from a snapshot; the crontab is restored from the
    original text. Returns the process exit code.
    """
    wrapper_path = Path(WRAPPER_PATH)
    token_file = Path(TOKEN_FILE_PATH)
    token_dir = Path(TOKEN_FILE_DIR)

    # Undo log: list of zero-arg callables, replayed in REVERSE on failure.
    undo: list[Callable[[], None]] = []

    def _rollback() -> None:
        for action in reversed(undo):
            try:
                action()
            except Exception as exc:  # best-effort — keep undoing the rest
                print(f"WARNING: rollback step failed: {exc}", file=sys.stderr)

    try:
        # --- 1. Write wrapper script ---
        wrapper_preexisted = wrapper_path.exists()
        wrapper_snapshot = wrapper_path.read_text(encoding="utf-8") if wrapper_preexisted else None
        wrapper_path.write_text(wrapper_content, encoding="utf-8")
        wrapper_path.chmod(0o755)
        if wrapper_preexisted:
            assert wrapper_snapshot is not None
            _snap = wrapper_snapshot

            def _undo_wrapper_restore(_f: Path = wrapper_path, _s: str = _snap) -> None:
                _f.write_text(_s, encoding="utf-8")

            undo.append(_undo_wrapper_restore)
        else:
            undo.append(lambda: wrapper_path.unlink(missing_ok=True))
        print(f"Wrote wrapper to {wrapper_path}")

        # --- 2. Write token file ---
        _write_token_file(token_file, token_dir, token, undo)

        # --- 3. Rewrite crontab (LAST mutation) ---
        lines_list = crontab_content.splitlines(keepends=False)
        lines_list[line_index] = new_line
        new_content = "\n".join(lines_list)
        if crontab_content.endswith("\n"):
            new_content += "\n"
        # Snapshot = the original text we already hold. Undo restores it
        # via the same atomic temp+rename used for the forward write.
        _orig = crontab_content
        undo.append(lambda: _atomic_write_text(crontab_file, _orig))
        _atomic_write_text(crontab_file, new_content)
        print(f"Rewrote crontab {crontab_file}")
    except Exception as exc:
        print(f"ERROR: install failed: {exc}; rolling back", file=sys.stderr)
        _rollback()
        print("Rollback complete; host left unchanged.", file=sys.stderr)
        return 1

    # --- 4. Register with monitor (best-effort; NOT rolled back) ---
    _register_with_monitor(monitor_url, fingerprint, token, reg_payload)

    print(f"Wrapper installed for {fingerprint}")
    return 0


def _run_uninstall(  # noqa: PLR0913
    *,
    monitor_url: str,
    token: str,
    host: str,
    crontab_spec: str,
    crontab_file: Path,
    crontab_content: str,
    line_index: int,
    old_line: str,
    schedule: str,
    command: str,
    confirm: bool,
) -> int:
    """Uninstall path: strip the wrapper prefix from one crontab line.

    Pure crontab-line edit — the shared wrapper script and token file are
    NEVER touched (D1/D2). Implements its own atomic temp+rename rewrite with
    rollback (the standalone installer has no host-side executor).
    """
    if WRAPPER_INVOCATION_PREFIX not in old_line:
        print(
            f"ERROR: crontab line is not wrapped; nothing to remove: {old_line}",
            file=sys.stderr,
        )
        return 1

    idx = old_line.find(WRAPPER_INVOCATION_PREFIX)
    new_line = old_line[:idx] + old_line[idx + len(WRAPPER_INVOCATION_PREFIX) :]
    inner_command = unwrap_command(command)
    fingerprint = compute_fingerprint(host, crontab_spec, schedule, inner_command)

    reg_payload: dict[str, object] = {
        "host": host,
        "source_path": crontab_spec,
        "schedule": schedule,
        "command": inner_command,
        "wrapper": False,
    }

    if not confirm:
        print("=== Crontab diff ===")
        print(f"File: {crontab_spec}")
        print(f"- {old_line}")
        print(f"+ {new_line}")
        return 0

    try:
        lines_list = crontab_content.splitlines(keepends=False)
        lines_list[line_index] = new_line
        new_content = "\n".join(lines_list)
        if crontab_content.endswith("\n"):
            new_content += "\n"
        _atomic_write_text(crontab_file, new_content)
        print(f"Rewrote crontab {crontab_file}")
    except Exception as exc:
        print(f"ERROR: uninstall failed: {exc}", file=sys.stderr)
        # Best-effort restore the original text.
        with contextlib.suppress(Exception):
            _atomic_write_text(crontab_file, crontab_content)
        print("Rollback complete; host left unchanged.", file=sys.stderr)
        return 1

    _register_with_monitor(monitor_url, fingerprint, token, reg_payload)
    print(f"Wrapper removed for {fingerprint}")
    return 0


def main() -> int:  # noqa: PLR0911, PLR0912, PLR0915 -- CLI entry point: linear validate-then-act flow; each early return is a distinct user-input error path
    """Main entry point."""
    args = _build_arg_parser().parse_args()

    monitor_url = args.monitor_url.rstrip("/")
    token = args.token
    crontab_spec = args.crontab
    line_num = args.line
    host = args.host

    # Validate inputs
    if not monitor_url:
        print("ERROR: --monitor-url or HM_MONITOR_URL required", file=sys.stderr)
        return 1
    if not token:
        print("ERROR: --token or HM_HEARTBEAT_TOKEN required", file=sys.stderr)
        return 1

    # Resolve crontab file
    if not crontab_spec:
        print("Available crontabs:")
        print("  1. /etc/crontab")
        for user_path in sorted(Path("/var/spool/cron/crontabs").glob("*")):
            if user_path.is_file():
                print(f"  crontab:{user_path.name}")
        crontab_spec = input("Enter crontab (e.g., crontab:root): ").strip()

    crontab_file = _resolve_crontab_file(crontab_spec)
    if not crontab_file.exists():
        print(f"ERROR: crontab file not found: {crontab_file}", file=sys.stderr)
        return 1

    # Read and parse crontab
    try:
        crontab_content = crontab_file.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        print(f"ERROR: failed to read crontab: {exc}", file=sys.stderr)
        return 1

    crontab_lines = parse_crontab_lines(crontab_content)
    if not crontab_lines:
        print("ERROR: no crontab lines found", file=sys.stderr)
        return 1

    # If line not specified, prompt
    if not line_num:
        print("Crontab lines:")
        for i, (_, line) in enumerate(crontab_lines, 1):
            print(f"  {i}. {line}")
        try:
            line_num = int(input("Select line (1-indexed): ").strip())
        except (ValueError, EOFError):
            print("ERROR: invalid line number", file=sys.stderr)
            return 1

    if not (1 <= line_num <= len(crontab_lines)):
        print(f"ERROR: line number out of range (1-{len(crontab_lines)})", file=sys.stderr)
        return 1

    line_index, old_line = crontab_lines[line_num - 1]

    parsed = _parse_job_line(old_line, crontab_spec)
    if parsed is None:
        print(f"ERROR: cannot parse crontab line: {old_line}", file=sys.stderr)
        return 1
    schedule, command = parsed

    if args.uninstall:
        return _run_uninstall(
            monitor_url=monitor_url,
            token=token,
            host=host,
            crontab_spec=crontab_spec,
            crontab_file=crontab_file,
            crontab_content=crontab_content,
            line_index=line_index,
            old_line=old_line,
            schedule=schedule,
            command=command,
            confirm=args.confirm,
        )

    # Compute fingerprint and fetch wrapper template
    fingerprint = compute_fingerprint(host, crontab_spec, schedule, command)
    install_date = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d")
    try:
        template_text = fetch_wrapper_template(monitor_url, token)
    except Exception as exc:
        print(f"ERROR: failed to fetch wrapper template: {exc}", file=sys.stderr)
        return 1
    # Substitute the 4 placeholders. Order + values must match
    # kernel/cron/install.py:_build_wrapper_content() so the produced
    # wrapper is byte-identical to the server-side installer's output.
    wrapper_content = (
        template_text.replace("{{FINGERPRINT}}", fingerprint)
        .replace("{{HEARTBEAT_URL_BASE}}", monitor_url)
        .replace("{{TOKEN_FILE_PATH}}", TOKEN_FILE_PATH)
        .replace("{{INSTALL_DATE}}", install_date)
    )
    # Splice the wrapper prefix in at the LAST occurrence of `command`, mirroring
    # install.py:_rewrite_line (rfind) and hm-cron-apply.sh:replace_last. A
    # command can contain its own schedule-like substrings, so first-occurrence
    # replace would rewrite the wrong span.
    _idx = old_line.rfind(command)
    if _idx < 0:
        print(f"ERROR: command not found in crontab line: {old_line}", file=sys.stderr)
        return 1
    new_line = old_line[:_idx] + WRAPPER_INVOCATION_PREFIX + command

    reg_payload: dict[str, object] = {
        "host": host,
        "source_path": crontab_spec,
        "schedule": schedule,
        "command": command,
        "wrapper": True,
    }

    # Dry-run: print preview
    if not args.confirm:
        print("=== Wrapper script ===")
        print(wrapper_content)
        print("\n=== Crontab diff ===")
        print(f"File: {crontab_spec}")
        print(f"- {old_line}")
        print(f"+ {new_line}")
        print("\n=== Registration payload ===")
        print(json.dumps(reg_payload, indent=2))
        return 0

    return _write_files_and_register(
        crontab_file=crontab_file,
        crontab_content=crontab_content,
        line_index=line_index,
        new_line=new_line,
        wrapper_content=wrapper_content,
        monitor_url=monitor_url,
        fingerprint=fingerprint,
        token=token,
        reg_payload=reg_payload,
    )


if __name__ == "__main__":
    sys.exit(main())
