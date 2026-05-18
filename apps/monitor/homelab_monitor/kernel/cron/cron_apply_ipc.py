"""Request/result IPC client for the host-side cron-apply executor (STAGE-002-009).

The monitor container has zero host-write capability. To apply a wrapper
install it writes a request JSON — carrying a LIST of operations — into
<IPC>/requests/<id>.json (atomic temp+rename) and polls <IPC>/results/<id>.json
for the executor's verdict. The executor applies the operation list atomically
(all-or-nothing with rollback).
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from structlog.stdlib import BoundLogger

from homelab_monitor.kernel.cron.cron_apply_constants import (
    OP_WRAP_CRONTAB,
    OP_WRITE_TOKEN,
    OP_WRITE_WRAPPER_SCRIPT,
    REQUEST_SCHEMA_VERSION,
    REQUESTS_SUBDIR,
    RESULT_POLL_INTERVAL_SECONDS,
    RESULT_POLL_TIMEOUT_SECONDS,
    RESULTS_SUBDIR,
    get_ipc_dir,
    is_valid_target_crontab,
)

#: Max byte length of any single op's `content` field. The real wrapper
#: script is ~1.5 KB and a token ~64 bytes; 64 KB is a generous ceiling that
#: still rejects a pathological / malicious oversized payload.
_MAX_OP_CONTENT_BYTES: int = 64 * 1024


@dataclass(frozen=True, slots=True)
class WrapCrontabOp:
    """A wrap-crontab operation: rewrite an already-present crontab line.

    `new_line` is the installer's intended rewritten line. The executor
    RE-DERIVES new_line itself from old_line + command (the security
    property); the supplied value is only a cross-check — the executor
    rejects the request (bad_request) if its re-derived line disagrees.
    """

    target_crontab: str
    old_line: str
    command: str
    new_line: str

    def to_payload(self) -> dict[str, object]:
        return {
            "operation": OP_WRAP_CRONTAB,
            "target_crontab": self.target_crontab,
            "old_line": self.old_line,
            "command": self.command,
            "new_line": self.new_line,
        }


@dataclass(frozen=True, slots=True)
class WriteWrapperScriptOp:
    """A write-wrapper-script operation: the executor writes a FIXED path."""

    content: str

    def to_payload(self) -> dict[str, object]:
        return {"operation": OP_WRITE_WRAPPER_SCRIPT, "content": self.content}


@dataclass(frozen=True, slots=True)
class WriteTokenOp:
    """A write-token operation: the executor writes a FIXED path."""

    content: str

    def to_payload(self) -> dict[str, object]:
        return {"operation": OP_WRITE_TOKEN, "content": self.content}


CronApplyOp = WrapCrontabOp | WriteWrapperScriptOp | WriteTokenOp


class CronApplyError(Exception):
    """Base — a cron-apply IPC failure (router maps to 5xx)."""


class CronApplyUnavailableError(CronApplyError):
    """The IPC dir / executor is missing or did not respond in time (→ 503)."""


class CronApplyRejectedError(CronApplyError):
    """The executor rejected the request (bad path / line / request) (→ 409)."""

    def __init__(self, message: str, *, error_code: str) -> None:
        super().__init__(message)
        self.error_code = error_code


@dataclass(frozen=True, slots=True)
class CronApplyResult:
    """Parsed result file."""

    id: str
    status: Literal["ok", "error"]
    error_code: str | None
    message: str


async def submit_and_wait(
    *,
    operations: Sequence[CronApplyOp],
    log: BoundLogger,
    ipc_dir: Path | None = None,
    timeout: float = RESULT_POLL_TIMEOUT_SECONDS,
) -> CronApplyResult:
    """Write a multi-operation request file, poll for the result, return it.

    The executor applies `operations` in order, atomically (all-or-nothing
    with rollback). A `status="ok"` result means every operation succeeded.

    Raises:
        CronApplyUnavailableError: IPC dir missing, or timeout (executor not
            installed / not running).
        CronApplyRejectedError: executor returned status=error (the whole
            request was rolled back).
        ValueError: `operations` is empty.
    """
    if not operations:
        raise ValueError("operations must be non-empty")

    base = ipc_dir if ipc_dir is not None else get_ipc_dir()
    requests_dir = base / REQUESTS_SUBDIR
    results_dir = base / RESULTS_SUBDIR

    if not requests_dir.is_dir():
        raise CronApplyUnavailableError(
            f"cron-apply IPC requests dir not found: {requests_dir}; "
            "run scripts/host-setup.sh on the host"
        )

    # Defensive pre-check: never submit a syntactically invalid crontab target.
    for op in operations:
        if isinstance(op, WrapCrontabOp) and not is_valid_target_crontab(op.target_crontab):
            raise CronApplyRejectedError(
                f"invalid target_crontab: {op.target_crontab!r}",
                error_code="bad_path",
            )
        if isinstance(op, (WriteWrapperScriptOp, WriteTokenOp)):
            content_bytes = len(op.content.encode("utf-8"))
            if content_bytes > _MAX_OP_CONTENT_BYTES:
                raise CronApplyRejectedError(
                    f"op content too large: {content_bytes} bytes (max {_MAX_OP_CONTENT_BYTES})",
                    error_code="bad_request",
                )

    request_id = str(uuid.uuid4())
    payload: dict[str, object] = {
        "id": request_id,
        "schema_version": REQUEST_SCHEMA_VERSION,
        "operations": [op.to_payload() for op in operations],
    }
    result_path = results_dir / f"{request_id}.json"
    request_path = requests_dir / f"{request_id}.json"

    _atomic_write_json(requests_dir, request_path, payload)
    log.info(
        "cron_apply.request_submitted",
        request_id=request_id,
        operations=[op.to_payload()["operation"] for op in operations],
    )

    _loop = asyncio.get_running_loop()
    deadline = _loop.time() + timeout
    while _loop.time() < deadline:
        if result_path.exists():
            result = _read_result(result_path)
            log.info(
                "cron_apply.result_received",
                request_id=request_id,
                status=result.status,
                error_code=result.error_code,
            )
            if result.status == "error":
                raise CronApplyRejectedError(
                    result.message, error_code=result.error_code or "unknown"
                )
            return result
        await asyncio.sleep(RESULT_POLL_INTERVAL_SECONDS)

    log.error("cron_apply.timeout", request_id=request_id)
    # Best-effort: remove the orphan request file so a slow executor does not
    # later apply a request the caller has already abandoned. missing_ok=True
    # tolerates the race where the executor picked it up between poll + unlink.
    request_path.unlink(missing_ok=True)
    raise CronApplyUnavailableError(
        f"cron-apply executor did not respond within {timeout:.0f}s; "
        "ensure homelab-monitor-cron-apply.path is enabled (run host-setup.sh)"
    )


def _atomic_write_json(parent: Path, target: Path, payload: dict[str, object]) -> None:
    """Write payload as JSON atomically: temp file in `parent` + os.replace.

    The .path watcher must never observe a partial file — hence temp+rename.
    """
    text = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    fd, tmp = tempfile.mkstemp(dir=parent, prefix=".", suffix=".json.tmp", text=True)
    try:
        # The request JSON transits the plaintext heartbeat token (write-token
        # op content). Force 0600 explicitly so the permission is independent
        # of the process umask.
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, target)
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise


def _read_result(path: Path) -> CronApplyResult:
    """Parse a results/<id>.json file. Raises CronApplyError on malformed JSON."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CronApplyError(f"unreadable cron-apply result {path}: {exc}") from exc
    status = data.get("status")
    if status not in ("ok", "error"):
        raise CronApplyError(f"bad status in cron-apply result {path}: {status!r}")
    return CronApplyResult(
        id=str(data.get("id", "")),
        status=status,
        error_code=data.get("error_code"),
        message=str(data.get("message", "")),
    )


__all__ = [
    "CronApplyError",
    "CronApplyOp",
    "CronApplyRejectedError",
    "CronApplyResult",
    "CronApplyUnavailableError",
    "WrapCrontabOp",
    "WriteTokenOp",
    "WriteWrapperScriptOp",
    "submit_and_wait",
]
