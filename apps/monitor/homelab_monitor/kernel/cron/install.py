"""Server-side installer for the heartbeat wrapper on a local cron (STAGE-002-009).

Uniform routing: the monitor container writes ONLY a request file to the IPC dir.
The host-side executor applies ALL writes atomically (wrapper script, token file,
crontab rewrite) with full rollback on failure. Pure-ish content building
(`build_install_kit`) is separated from IPC-driving I/O (`install_wrapper_local`)
so tests can target each independently.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

from structlog.stdlib import BoundLogger

from homelab_monitor.kernel.auth.repository import AuthRepository
from homelab_monitor.kernel.cron.cron_apply_ipc import (
    CronApplyRejectedError,
    UnwrapCrontabOp,
    WrapCrontabOp,
    WriteTokenOp,
    WriteWrapperEnvOp,
    WriteWrapperScriptOp,
    submit_and_wait,
)
from homelab_monitor.kernel.cron.cron_apply_ipc import (
    CronApplyUnavailableError as _IpcCronApplyUnavailableError,
)
from homelab_monitor.kernel.cron.discovery_types import CronSourceKind
from homelab_monitor.kernel.cron.fingerprint import compute_fingerprint
from homelab_monitor.kernel.cron.heartbeat_wrapper_token import (
    ensure_heartbeat_wrapper_token,
)
from homelab_monitor.kernel.cron.repository import CronRecord, CronRepo
from homelab_monitor.kernel.cron.wrapper_constants import (
    TOKEN_FILE_PATH,
    WRAPPER_ENV_PATH,
    WRAPPER_FORMAT_VERSION,
    WRAPPER_PATH,
    WRAPPER_SEPARATOR,
    build_invocation_prefix,
    is_legacy_wrapped,
    is_wrapped,
    unwrap_command,
)
from homelab_monitor.kernel.db.time import utc_now_iso
from homelab_monitor.kernel.secrets.repository import AsyncSecretsRepository
from homelab_monitor.plugins.discoverers.cron_discoverer import resolve_snapshot_dir
from homelab_monitor.plugins.discoverers.cron_parser import parse_one_line

# ---------- Typed errors ----------


class WrapperInstallError(Exception):
    """Base class for install failures (caller maps to a 4xx/5xx)."""


class RemoteHostError(WrapperInstallError):
    """The cron's host is not the monitor's own host (router → 400)."""


class CronLineNotFoundError(WrapperInstallError):
    """No crontab line in source_path fingerprint-matches the cron (→ 409)."""


class AlreadyWrappedError(WrapperInstallError):
    """The matched crontab line is already wrapper-invoked (→ 409)."""


class NotWrappedError(WrapperInstallError):
    """The matched crontab line is NOT wrapper-invoked — nothing to remove (→ 409).

    The exact mirror of AlreadyWrappedError. Uninstall gates on the discovered
    crontab line being currently wrapped (the crontab is ground truth, NOT
    wrapper_last_seen_at).
    """


class CrontabWriteError(WrapperInstallError):
    """A filesystem error during wrapper/token/crontab write; rollback ran (→ 500)."""


class CronApplyUnavailableError(WrapperInstallError):
    """The host-side cron-apply executor is not installed / not responding (→ 503)."""


# ---------- Result dataclasses ----------


@dataclass(frozen=True, slots=True)
class CrontabDiff:
    """The single crontab line change the install will make."""

    source_path: str  # e.g. "crontab:alice" or "/etc/crontab"
    container_file: str  # resolved /host/... path the installer reads from
    old_line: str  # the exact original line (byte-exact)
    new_line: str  # the rewritten line
    line_index: int  # 0-based index in the file's splitlines()
    inner_command: str  # the unwrapped command substring (for the executor request)


@dataclass(frozen=True, slots=True)
class WrapperInstallKit:
    """Everything the install would do — returned by dry-run and apply."""

    fingerprint: str
    wrapper_path: str  # WRAPPER_PATH — the HOST path (contract: crontab line)
    wrapper_content: str  # fully-substituted wrapper script text
    token_file_path: str  # TOKEN_FILE_PATH — the HOST path (baked into wrapper)
    crontab_diff: CrontabDiff


@dataclass(frozen=True, slots=True)
class WrapperUninstallKit:
    """Everything the uninstall would do — returned by dry-run and apply.

    Uninstall is a pure crontab-line edit (D1: never delete the wrapper script;
    D2: never touch the token). So this kit carries ONLY the crontab diff.
    """

    fingerprint: str
    crontab_diff: CrontabDiff


# ---------- Host path resolution ----------


def _resolve_container_path(source_path: str, host_root: Path) -> Path:
    """Return the container-side path the installer reads the source crontab from.

    System crontabs (/etc/crontab, /etc/cron.d/*) are read from the read-only
    /etc bind mount under host_root. User crontabs (crontab:<user>) are read
    from the root-generated crontab-snapshot directory (HM_CRON_SNAPSHOT_DIR) —
    the SAME source the cron-discoverer reads. The container has NO mount on
    /var/spool/cron/crontabs (STAGE-002-009 Option B): the 0600 spool files are
    unreadable by the non-root container, so the install dry-run reads the
    world-readable snapshot copy. The host-side executor still verifies and
    rewrites the REAL spool file. No write capability here.
    """
    if source_path == "/etc/crontab":
        return host_root / "etc" / "crontab"
    if source_path.startswith("/etc/cron.d/"):
        name = source_path[len("/etc/cron.d/") :]
        return host_root / "etc" / "cron.d" / name
    if source_path.startswith("crontab:"):
        user = source_path[len("crontab:") :]
        return resolve_snapshot_dir() / user
    raise WrapperInstallError(f"unrecognized source_path: {source_path!r}")


# ---------- Crontab line matching ----------


def _source_kind_from_path(source_path: str) -> CronSourceKind:
    """Infer the source kind from the source path."""
    if source_path.startswith("crontab:"):
        return CronSourceKind.USER_CRONTAB
    return CronSourceKind.SYSTEM_WITH_USER_FIELD


def _find_matching_line(
    *,
    content: str,
    host: str,
    source_path: str,
    fingerprint: str,
) -> tuple[int, str, str, str, bool, str]:
    """Return (line_index, raw_line, schedule, inner_command, line_is_wrapped, raw_command) for
    the line whose fingerprint matches. Raises CronLineNotFoundError if none match.

    inner_command is the UNWRAPPED command — so fingerprint matching works
    whether the file currently shows the wrapped or unwrapped form. Matching
    uses compute_fingerprint(host, source_path, schedule, unwrap_command(cmd)).

    line_is_wrapped is True iff the COMMAND PORTION of the matched line is a
    wrapper invocation. It is computed via ``is_wrapped`` on the genuine
    PRE-unwrap command (parsed with ``unwrap=False``) — NOT on the raw line,
    which would always be False because ``is_wrapped`` is ``^``-anchored and the
    wrapper path is never at the start of a crontab line (schedule/user precede it).

    raw_command is the genuine PRE-unwrap command from the crontab line (the command
    portion exactly as it appears on disk, before any unwrapping).
    """
    source_kind = _source_kind_from_path(source_path)

    for line_index, raw_line in enumerate(content.splitlines(keepends=False)):
        parsed = parse_one_line(line=raw_line, source_kind=source_kind, unwrap=False)
        if parsed is None:
            continue

        schedule, raw_command = parsed
        line_is_wrapped = is_wrapped(raw_command)
        inner_command = unwrap_command(raw_command)
        fp = compute_fingerprint(
            host=host,
            source_path=source_path,
            schedule=schedule,
            command=inner_command,
        )

        if fp == fingerprint:
            return line_index, raw_line, schedule, inner_command, line_is_wrapped, raw_command

    raise CronLineNotFoundError(f"no crontab line matches fingerprint {fingerprint}")


# ---------- Line rewrite ----------


def _rewrite_line(
    raw_line: str,
    inner_command: str,
    fingerprint: str,
) -> str:
    """Produce the wrapped replacement line.

    Handles TWO cases:
    1. UNWRAPPED source line: find inner_command at the END (rfind) and prepend
       the wrapper prefix.
    2. LEGACY-wrapped source line: the line contains the segment
       `<WRAPPER_PATH> -- <inner_command>`; this legacy segment must be
       REPLACED (not just prepended) with the NEW format.

    Preserves leading whitespace, the schedule field and (for system crontabs)
    the USER column byte-exact — only the command tail is rewritten.
    """
    legacy_segment = f"{WRAPPER_PATH} {WRAPPER_SEPARATOR} {inner_command}"
    new_segment = build_invocation_prefix(fingerprint) + inner_command

    # Try replacing the legacy segment (for format-migration re-wraps).
    idx = raw_line.rfind(legacy_segment)
    if idx >= 0:
        return raw_line[:idx] + new_segment + raw_line[idx + len(legacy_segment) :]

    # Fall back to the plain unwrapped case (rfind inner_command + prepend).
    idx = raw_line.rfind(inner_command)
    if idx < 0:  # defensive — parser extracted it from this line
        raise WrapperInstallError("internal: command not found in raw line")
    return raw_line[:idx] + build_invocation_prefix(fingerprint) + inner_command


# ---------- Wrapper content building ----------


def _build_wrapper_content() -> str:
    """Build the GENERIC wrapper script. Byte-identical for every cron — the
    only substitutions are fixed constants (single-sourced from
    wrapper_constants.py), so the rendered file is identical for all installs."""
    template_text = (
        files("homelab_monitor")
        .joinpath("data", "cron-with-heartbeat.sh.tmpl")
        .read_text(encoding="utf-8")
    )
    return (
        template_text.replace("{{TOKEN_FILE_PATH}}", TOKEN_FILE_PATH)
        .replace("{{WRAPPER_ENV_PATH}}", WRAPPER_ENV_PATH)
        .replace("{{WRAPPER_FORMAT_VERSION}}", WRAPPER_FORMAT_VERSION)
    )


# ---------- Public functions ----------


async def build_install_kit(
    cron: CronRecord,
    *,
    host_root: Path,
    public_url: str,
) -> WrapperInstallKit:
    """Pure-ish: resolve host paths, read the source crontab, find + match the
    line, build wrapper content + crontab diff. NO writes. Raises
    CronLineNotFoundError / AlreadyWrappedError / WrapperInstallError.
    Used by both dry-run and apply.
    """
    source_path = cron.source_path
    if source_path is None:
        raise WrapperInstallError(
            f"cron {cron.fingerprint} has no source_path (remote-only); "
            "wrapper install requires a local crontab file"
        )

    container_path = _resolve_container_path(source_path, host_root)

    if not container_path.exists():
        raise CronLineNotFoundError(f"crontab file not found: {container_path}")

    crontab_content = container_path.read_text(encoding="utf-8", errors="replace")

    (
        line_index,
        raw_line,
        _schedule,
        inner_command,
        line_is_wrapped,
        raw_command,
    ) = _find_matching_line(
        content=crontab_content,
        host=cron.host,
        source_path=source_path,
        fingerprint=cron.fingerprint,
    )

    # A CURRENT-format wrapped line is rejected (genuinely already installed).
    # A LEGACY-format wrapped line is allowed through (format-migration re-wrap).
    if line_is_wrapped and not is_legacy_wrapped(raw_command):
        raise AlreadyWrappedError(f"crontab line is already wrapped for {cron.fingerprint}")

    new_line = _rewrite_line(raw_line, inner_command, cron.fingerprint)

    wrapper_content = _build_wrapper_content()

    return WrapperInstallKit(
        fingerprint=cron.fingerprint,
        wrapper_path=WRAPPER_PATH,
        wrapper_content=wrapper_content,
        token_file_path=TOKEN_FILE_PATH,
        crontab_diff=CrontabDiff(
            source_path=source_path,
            container_file=str(container_path),
            old_line=raw_line,
            new_line=new_line,
            line_index=line_index,
            inner_command=inner_command,
        ),
    )


async def build_uninstall_kit(
    cron: CronRecord,
    *,
    host_root: Path,
) -> WrapperUninstallKit:
    """Pure-ish: resolve host paths, read the source crontab, find + match the
    line, build the unwrap crontab diff. NO writes. Raises
    CronLineNotFoundError / NotWrappedError / WrapperInstallError.
    Used by both dry-run and apply.

    No public_url / install_date params — uninstall builds no wrapper content.
    """
    source_path = cron.source_path
    if source_path is None:
        raise WrapperInstallError(
            f"cron {cron.fingerprint} has no source_path (remote-only); "
            "wrapper uninstall requires a local crontab file"
        )

    container_path = _resolve_container_path(source_path, host_root)

    if not container_path.exists():
        raise CronLineNotFoundError(f"crontab file not found: {container_path}")

    crontab_content = container_path.read_text(encoding="utf-8", errors="replace")

    (
        line_index,
        raw_line,
        _schedule,
        inner_command,
        line_is_wrapped,
        _raw_command,
    ) = _find_matching_line(
        content=crontab_content,
        host=cron.host,
        source_path=source_path,
        fingerprint=cron.fingerprint,
    )

    # D6: gate on the crontab line itself — the crontab is ground truth.
    if not line_is_wrapped:
        raise NotWrappedError(
            f"crontab line is not wrapped for {cron.fingerprint}; nothing to remove"
        )

    # Re-derive the unwrapped line: strip the wrapper prefix in-place by finding
    # the prefix for this specific fingerprint and removing it.
    prefix = build_invocation_prefix(cron.fingerprint)
    idx = raw_line.find(prefix)
    if idx < 0:
        # The line is wrapped but with a DIFFERENT fingerprint argument than this
        # cron's — should not happen (fingerprint match already passed). Defensive.
        raise WrapperInstallError("internal: wrapper prefix not found in raw line")
    new_line = raw_line[:idx] + raw_line[idx + len(prefix) :]

    return WrapperUninstallKit(
        fingerprint=cron.fingerprint,
        crontab_diff=CrontabDiff(
            source_path=source_path,
            container_file=str(container_path),
            old_line=raw_line,
            new_line=new_line,
            line_index=line_index,
            inner_command=inner_command,
        ),
    )


async def install_wrapper_local(  # noqa: PLR0913 -- explicit DI (repos/log)
    fingerprint: str,
    *,
    cron_repo: CronRepo,
    auth_repo: AuthRepository,
    secrets_repo: AsyncSecretsRepository,
    host_root: Path,
    public_url: str,
    local_hostname: str,
    who: str,
    ip: str | None,
    log: BoundLogger,
    ipc_dir: Path | None = None,
) -> CronRecord:
    """Uniform routing install via host-side executor.

    Steps:
    1. Fetch cron
    2. Check host
    3. Build install kit (reads crontab, finds line, builds wrapper content)
    4. Ensure token exists
    5. Build 4-operation request (wrapper-script, token, wrapper-env, wrap-crontab)
    6. Submit to executor and wait for result
    7. Translate executor errors to typed errors
    8. Set wrapper_format_version
    9. Upsert discovered (so row reflects wrapped line)
    10. Audit record
    11. Return refreshed cron

    No host-side file I/O — all writes performed by the executor atomically
    with rollback. Errors before the IPC call (RemoteHostError,
    CronLineNotFoundError, AlreadyWrappedError) are re-raised unchanged.
    Executor errors are translated to typed errors.
    """
    # Step 1: Fetch cron
    cron = await cron_repo.get_cron(fingerprint, include_hidden=True)
    if cron is None:
        raise CronLineNotFoundError(f"cron not found: {fingerprint}")

    # Step 2: Check host
    if cron.host != local_hostname:
        raise RemoteHostError(f"cron is on host {cron.host!r}, not local {local_hostname!r}")

    # Step 3: Build install kit
    kit = await build_install_kit(cron, host_root=host_root, public_url=public_url)

    # Step 4: Ensure token exists
    try:
        plaintext_token = await ensure_heartbeat_wrapper_token(auth_repo, secrets_repo, log=log)
    except Exception as exc:
        raise CrontabWriteError(f"failed to ensure token: {exc}") from exc

    # Step 5: Build operation list (order matters: wrapper + token before wrap-crontab)
    if "\n" in public_url or "\r" in public_url:
        raise WrapperInstallError("public_url contains a newline — refusing to write wrapper.env")
    operations = [
        WriteWrapperScriptOp(content=kit.wrapper_content),
        WriteTokenOp(content=plaintext_token),
        WriteWrapperEnvOp(content=f"HEARTBEAT_URL_BASE={public_url}\n"),
        WrapCrontabOp(
            target_crontab=kit.crontab_diff.source_path,
            old_line=kit.crontab_diff.old_line,
            command=kit.crontab_diff.inner_command,
            new_line=kit.crontab_diff.new_line,
        ),
    ]

    # Step 6: IPC request to executor
    try:
        await submit_and_wait(
            operations=operations,
            log=log,
            ipc_dir=ipc_dir,
        )
    except _IpcCronApplyUnavailableError as exc:
        # Executor not installed / timed out
        raise CronApplyUnavailableError(str(exc)) from exc
    except CronApplyRejectedError as exc:
        # Executor rejected the request — translate error_code.
        if exc.error_code == "already_wrapped":
            raise AlreadyWrappedError(str(exc)) from exc
        # write_failed / crontab_missing are SERVER-side failures (filesystem /
        # sandbox), not client mistakes — surface as 500 via CrontabWriteError.
        if exc.error_code in ("write_failed", "crontab_missing"):
            raise CrontabWriteError(str(exc)) from exc
        # bad_path, line_not_found, bad_request → 409 (client/request problem).
        raise CronLineNotFoundError(str(exc)) from exc
    except Exception as exc:
        # Malformed result / other IPC error
        raise CrontabWriteError(f"cron-apply IPC error: {exc}") from exc

    # Step 7: (No-op — executor already succeeded and rolled back on any failure)

    # Step 8: Record the installed wrapper's format version (D-FORMATVER).
    await cron_repo.set_wrapper_format_version(fingerprint, WRAPPER_FORMAT_VERSION)

    # Step 9: Bump last_discovered_at so the registry's freshness reflects this
    # operator-initiated touch; discovery re-converges on its next tick.
    await cron_repo.upsert_discovered(
        host=cron.host,
        source_path=kit.crontab_diff.source_path,
        schedule=cron.schedule,
        command=cron.command,
        is_wrapped=True,
        now=utc_now_iso(),
    )

    # Step 10: Audit verb
    await cron_repo.record_wrapper_installed(fingerprint, who=who, ip=ip)

    # Step 11: Return refreshed cron
    updated_cron = await cron_repo.get_cron(fingerprint, include_hidden=True)
    if updated_cron is None:
        raise CrontabWriteError("cron disappeared after install")

    log.info("wrapper_installed.success", fingerprint=fingerprint)
    return updated_cron


async def uninstall_wrapper_local(  # noqa: PLR0913 -- explicit DI (repos/log)
    fingerprint: str,
    *,
    cron_repo: CronRepo,
    host_root: Path,
    local_hostname: str,
    who: str,
    ip: str | None,
    log: BoundLogger,
    ipc_dir: Path | None = None,
) -> CronRecord:
    """Uniform routing uninstall via host-side executor.

    Steps:
    1. Fetch cron
    2. Check host
    3. Build uninstall kit (reads crontab, finds line, builds unwrap diff)
    4. Build 1-operation request (unwrap-crontab) — NO token, NO wrapper-script
    5. Submit to executor and wait for result
    6. Translate executor errors to typed errors
    7. Bump last_discovered_at (registry freshness)
    8. record_wrapper_uninstalled — clears wrapper_last_seen_at + audit row
    9. Return refreshed cron

    No host-side file I/O — the executor performs the single crontab rewrite
    atomically with rollback. D1: the shared wrapper script is NEVER removed.
    D2: the shared token file is NEVER touched.
    """
    # Step 1: Fetch cron
    cron = await cron_repo.get_cron(fingerprint, include_hidden=True)
    if cron is None:
        raise CronLineNotFoundError(f"cron not found: {fingerprint}")

    # Step 2: Check host
    if cron.host != local_hostname:
        raise RemoteHostError(f"cron is on host {cron.host!r}, not local {local_hostname!r}")

    # Step 3: Build uninstall kit
    kit = await build_uninstall_kit(cron, host_root=host_root)

    # Step 4: Build the single-operation request
    operations = [
        UnwrapCrontabOp(
            target_crontab=kit.crontab_diff.source_path,
            old_line=kit.crontab_diff.old_line,
            new_line=kit.crontab_diff.new_line,
        ),
    ]

    # Step 5: IPC request to executor
    try:
        await submit_and_wait(
            operations=operations,
            log=log,
            ipc_dir=ipc_dir,
        )
    except _IpcCronApplyUnavailableError as exc:
        raise CronApplyUnavailableError(str(exc)) from exc
    except CronApplyRejectedError as exc:
        # Executor rejected — translate error_code.
        if exc.error_code == "not_wrapped":
            raise NotWrappedError(str(exc)) from exc
        if exc.error_code in ("write_failed", "crontab_missing"):
            raise CrontabWriteError(str(exc)) from exc
        # bad_path, line_not_found, bad_request → 409.
        raise CronLineNotFoundError(str(exc)) from exc
    except Exception as exc:
        raise CrontabWriteError(f"cron-apply IPC error: {exc}") from exc

    # Step 7: Bump last_discovered_at so registry freshness reflects this touch.
    await cron_repo.upsert_discovered(
        host=cron.host,
        source_path=kit.crontab_diff.source_path,
        schedule=cron.schedule,
        command=cron.command,
        is_wrapped=False,
        now=utc_now_iso(),
    )

    # Step 8: Audit verb + clear wrapper_last_seen_at (D3).
    await cron_repo.record_wrapper_uninstalled(fingerprint, who=who, ip=ip)

    # Step 9: Return refreshed cron
    updated_cron = await cron_repo.get_cron(fingerprint, include_hidden=True)
    if updated_cron is None:
        raise CrontabWriteError("cron disappeared after uninstall")

    log.info("wrapper_uninstalled.success", fingerprint=fingerprint)
    return updated_cron


__all__ = [
    "AlreadyWrappedError",
    "CronApplyUnavailableError",
    "CronLineNotFoundError",
    "CrontabDiff",
    "CrontabWriteError",
    "NotWrappedError",
    "RemoteHostError",
    "WrapperInstallError",
    "WrapperInstallKit",
    "WrapperUninstallKit",
    "build_install_kit",
    "build_uninstall_kit",
    "install_wrapper_local",
    "uninstall_wrapper_local",
]
