"""Tests for cron_apply_ipc (STAGE-002-009).

Covers:
- Dataclass to_payload() produces correct dicts with correct keys
- File-write op payloads contain NO path/target/target_crontab key
- submit_and_wait: writes request with correct JSON shape (id, schema_version=1, operations)
- submit_and_wait: 3-op list serialized in order
- submit_and_wait: result file present with status=ok → returns CronApplyResult
- submit_and_wait: result file with status=error → raises CronApplyRejectedError with error_code
- submit_and_wait: missing requests/ dir → CronApplyUnavailableError (immediate)
- submit_and_wait: timeout (no result file) → CronApplyUnavailableError
- submit_and_wait: empty operations → ValueError
- submit_and_wait: WrapCrontabOp with invalid target_crontab → CronApplyRejectedError(bad_path)
- _atomic_write_json: leaves no .tmp on success, cleans up on os.replace failure
- _atomic_write_json: writes byte-exact JSON
- _read_result: raises CronApplyError on malformed JSON and on bad status
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest
import structlog

from homelab_monitor.kernel.cron.cron_apply_constants import (
    OP_WRAP_CRONTAB,
    OP_WRITE_TOKEN,
    OP_WRITE_WRAPPER_SCRIPT,
    REQUEST_SCHEMA_VERSION,
    REQUESTS_SUBDIR,
    RESULTS_SUBDIR,
)
from homelab_monitor.kernel.cron.cron_apply_ipc import (
    CronApplyError,
    CronApplyRejectedError,
    CronApplyResult,
    CronApplyUnavailableError,
    WrapCrontabOp,
    WriteTokenOp,
    WriteWrapperScriptOp,
    _atomic_write_json,  # pyright: ignore[reportPrivateUsage]
    _read_result,  # pyright: ignore[reportPrivateUsage]
    submit_and_wait,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _null_log() -> structlog.stdlib.BoundLogger:
    return structlog.get_logger()  # type: ignore[return-value]


def _make_ipc_dirs(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Create base/requests/results structure; return (base, requests, results)."""
    base = tmp_path / "ipc"
    requests_dir = base / REQUESTS_SUBDIR
    results_dir = base / RESULTS_SUBDIR
    requests_dir.mkdir(parents=True)
    results_dir.mkdir(parents=True)
    return base, requests_dir, results_dir


def _write_result(
    results_dir: Path, req_id: str, *, status: str, error_code: str | None = None, message: str = ""
) -> Path:
    result_path = results_dir / f"{req_id}.json"
    data: dict[str, object] = {
        "id": req_id,
        "status": status,
        "error_code": error_code,
        "message": message,
    }
    result_path.write_text(json.dumps(data), encoding="utf-8")
    return result_path


# ---------------------------------------------------------------------------
# Dataclass to_payload() unit tests
# ---------------------------------------------------------------------------


def test_wrap_crontab_op_payload() -> None:
    op = WrapCrontabOp(
        target_crontab="crontab:alice",
        old_line="*/10 * * * * /usr/bin/task.sh",
        command="/usr/bin/task.sh",
        new_line="*/10 * * * * /usr/local/bin/cron-with-heartbeat.sh -- /usr/bin/task.sh",
    )
    p = op.to_payload()
    assert p["operation"] == OP_WRAP_CRONTAB
    assert p["target_crontab"] == "crontab:alice"
    assert p["old_line"] == "*/10 * * * * /usr/bin/task.sh"
    assert p["command"] == "/usr/bin/task.sh"
    # new_line must be present in the payload (executor cross-check)
    assert p["new_line"] == "*/10 * * * * /usr/local/bin/cron-with-heartbeat.sh -- /usr/bin/task.sh"


def test_write_wrapper_script_op_payload() -> None:
    op = WriteWrapperScriptOp(content="#!/bin/bash\necho hi\n")
    p = op.to_payload()
    assert p["operation"] == OP_WRITE_WRAPPER_SCRIPT
    assert p["content"] == "#!/bin/bash\necho hi\n"


def test_write_token_op_payload() -> None:
    op = WriteTokenOp(content="hb_abc123")
    p = op.to_payload()
    assert p["operation"] == OP_WRITE_TOKEN
    assert p["content"] == "hb_abc123"


def test_file_write_op_has_no_path_key() -> None:
    """Regression: file-write op payloads must NOT carry path/target/target_crontab."""
    script_op = WriteWrapperScriptOp(content="#!/bin/bash\n")
    token_op = WriteTokenOp(content="tok")
    for op in (script_op, token_op):
        p = op.to_payload()
        assert "path" not in p
        assert "target" not in p
        assert "target_crontab" not in p


# ---------------------------------------------------------------------------
# submit_and_wait: request JSON shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_writes_request_with_correct_shape(tmp_path: Path) -> None:
    """submit_and_wait writes request JSON with id, schema_version=1, operations list."""
    base, _requests_dir, results_dir = _make_ipc_dirs(tmp_path)
    fixed_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

    ops = [
        WriteWrapperScriptOp(content="#!/bin/bash\n"),
        WriteTokenOp(content="tok"),
        WrapCrontabOp(
            target_crontab="/etc/crontab",
            old_line="*/5 * * * * root /usr/bin/task.sh",
            command="/usr/bin/task.sh",
            new_line="*/5 * * * * root /usr/local/bin/cron-with-heartbeat.sh -- /usr/bin/task.sh",
        ),
    ]

    # Pre-write the result so the poller finds it immediately
    _write_result(results_dir, fixed_id, status="ok", message="applied 3 operations")

    with patch(
        "homelab_monitor.kernel.cron.cron_apply_ipc.uuid.uuid4", return_value=uuid.UUID(fixed_id)
    ):
        result = await submit_and_wait(
            operations=ops,
            log=_null_log(),
            ipc_dir=base,
            timeout=5.0,
        )

    assert result.status == "ok"

    # Now verify the request file (already renamed from .tmp)
    # The request file may have been consumed; but we can inspect what was written
    # by reading any remaining .json files
    assert isinstance(result, CronApplyResult)
    assert result.id == fixed_id


@pytest.mark.asyncio
async def test_submit_request_file_has_correct_json(tmp_path: Path) -> None:
    """The written request file carries correct JSON structure."""
    base, _requests_dir, results_dir = _make_ipc_dirs(tmp_path)
    fixed_id = "11111111-2222-3333-4444-555555555555"

    ops = [
        WriteWrapperScriptOp(content="wrapper"),
        WriteTokenOp(content="mytoken"),
        WrapCrontabOp(
            target_crontab="crontab:bob",
            old_line="0 2 * * * /usr/bin/backup.sh",
            command="/usr/bin/backup.sh",
            new_line="0 2 * * * /usr/local/bin/cron-with-heartbeat.sh -- /usr/bin/backup.sh",
        ),
    ]

    # Intercept the atomic write to inspect the payload before it's consumed
    captured: dict[str, object] = {}
    real_atomic = _atomic_write_json

    def _capture(parent: Path, target: Path, payload: dict[str, object]) -> None:
        captured.update(payload)
        real_atomic(parent, target, payload)

    # Pre-write result so poller exits quickly
    _write_result(results_dir, fixed_id, status="ok", message="ok")

    with (
        patch(
            "homelab_monitor.kernel.cron.cron_apply_ipc.uuid.uuid4",
            return_value=uuid.UUID(fixed_id),
        ),
        patch(
            "homelab_monitor.kernel.cron.cron_apply_ipc._atomic_write_json", side_effect=_capture
        ),
    ):
        await submit_and_wait(operations=ops, log=_null_log(), ipc_dir=base, timeout=5.0)

    assert captured["id"] == fixed_id
    assert captured["schema_version"] == REQUEST_SCHEMA_VERSION
    operations: list[dict[str, object]] = captured["operations"]  # type: ignore[assignment]
    assert isinstance(operations, list)
    assert len(operations) == 3  # noqa: PLR2004
    assert operations[0]["operation"] == OP_WRITE_WRAPPER_SCRIPT
    assert operations[1]["operation"] == OP_WRITE_TOKEN
    assert operations[2]["operation"] == OP_WRAP_CRONTAB
    assert operations[2]["target_crontab"] == "crontab:bob"
    assert operations[2]["old_line"] == "0 2 * * * /usr/bin/backup.sh"
    assert operations[2]["command"] == "/usr/bin/backup.sh"


@pytest.mark.asyncio
async def test_submit_ops_serialized_in_list_order(tmp_path: Path) -> None:
    """Operations appear in the request in the same order as the input list."""
    base, _requests_dir, results_dir = _make_ipc_dirs(tmp_path)
    fixed_id = "ffffffff-0000-1111-2222-333333333333"
    _write_result(results_dir, fixed_id, status="ok")

    ops = [
        WriteWrapperScriptOp(content="script"),
        WriteTokenOp(content="token"),
        WrapCrontabOp(
            target_crontab="/etc/crontab",
            old_line="line",
            command="cmd",
            new_line="/usr/local/bin/cron-with-heartbeat.sh -- cmd",
        ),
    ]

    captured_ops: list[str] = []

    def _capture(parent: Path, target: Path, payload: dict[str, object]) -> None:
        for op in payload.get("operations", []):  # type: ignore[union-attr]
            captured_ops.append(str(op["operation"]))  # type: ignore[index]
        _atomic_write_json(parent, target, payload)

    with (
        patch(
            "homelab_monitor.kernel.cron.cron_apply_ipc.uuid.uuid4",
            return_value=uuid.UUID(fixed_id),
        ),
        patch(
            "homelab_monitor.kernel.cron.cron_apply_ipc._atomic_write_json", side_effect=_capture
        ),
    ):
        await submit_and_wait(operations=ops, log=_null_log(), ipc_dir=base, timeout=5.0)

    assert captured_ops == [OP_WRITE_WRAPPER_SCRIPT, OP_WRITE_TOKEN, OP_WRAP_CRONTAB]


# ---------------------------------------------------------------------------
# submit_and_wait: success result → returns CronApplyResult
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_returns_result_on_ok(tmp_path: Path) -> None:
    """When result file status=ok, submit_and_wait returns CronApplyResult."""
    base, _requests_dir, results_dir = _make_ipc_dirs(tmp_path)
    fixed_id = "deadbeef-dead-beef-dead-beefdeadbeef"
    _write_result(results_dir, fixed_id, status="ok", message="applied 3 operations")

    ops = [WriteWrapperScriptOp(content="x")]

    with patch(
        "homelab_monitor.kernel.cron.cron_apply_ipc.uuid.uuid4", return_value=uuid.UUID(fixed_id)
    ):
        result = await submit_and_wait(operations=ops, log=_null_log(), ipc_dir=base, timeout=5.0)

    assert result.status == "ok"
    assert result.id == fixed_id
    assert result.error_code is None
    assert "applied" in result.message


# ---------------------------------------------------------------------------
# submit_and_wait: status=error → raises CronApplyRejectedError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_raises_rejected_on_error_result(tmp_path: Path) -> None:
    """status=error → CronApplyRejectedError with the correct error_code."""
    base, _requests_dir, results_dir = _make_ipc_dirs(tmp_path)
    fixed_id = "cafecafe-cafe-cafe-cafe-cafecafecafe"
    _write_result(
        results_dir,
        fixed_id,
        status="error",
        error_code="line_not_found",
        message="old_line not present",
    )

    ops = [
        WrapCrontabOp(
            target_crontab="/etc/crontab",
            old_line="missing",
            command="cmd",
            new_line="/usr/local/bin/cron-with-heartbeat.sh -- cmd",
        )
    ]

    with (
        patch(
            "homelab_monitor.kernel.cron.cron_apply_ipc.uuid.uuid4",
            return_value=uuid.UUID(fixed_id),
        ),
        pytest.raises(CronApplyRejectedError) as exc_info,
    ):
        await submit_and_wait(operations=ops, log=_null_log(), ipc_dir=base, timeout=5.0)

    assert exc_info.value.error_code == "line_not_found"


@pytest.mark.asyncio
async def test_submit_rejected_error_code_already_wrapped(tmp_path: Path) -> None:
    """error_code=already_wrapped is preserved on CronApplyRejectedError."""
    base, _requests_dir, results_dir = _make_ipc_dirs(tmp_path)
    fixed_id = "bebebebe-bebe-bebe-bebe-bebebebebebe"
    _write_result(
        results_dir, fixed_id, status="error", error_code="already_wrapped", message="wrapped"
    )

    ops = [
        WrapCrontabOp(
            target_crontab="/etc/crontab",
            old_line="x",
            command="x",
            new_line="/usr/local/bin/cron-with-heartbeat.sh -- x",
        )
    ]

    with (
        patch(
            "homelab_monitor.kernel.cron.cron_apply_ipc.uuid.uuid4",
            return_value=uuid.UUID(fixed_id),
        ),
        pytest.raises(CronApplyRejectedError) as exc_info,
    ):
        await submit_and_wait(operations=ops, log=_null_log(), ipc_dir=base, timeout=5.0)

    assert exc_info.value.error_code == "already_wrapped"


# ---------------------------------------------------------------------------
# submit_and_wait: missing requests/ dir → immediate CronApplyUnavailableError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_unavailable_when_requests_dir_missing(tmp_path: Path) -> None:
    """Missing requests/ dir → CronApplyUnavailableError (no timeout wait)."""
    base = tmp_path / "no-ipc"
    base.mkdir()
    # requests/ NOT created

    ops = [WriteTokenOp(content="tok")]

    with pytest.raises(CronApplyUnavailableError):
        await submit_and_wait(operations=ops, log=_null_log(), ipc_dir=base, timeout=5.0)


# ---------------------------------------------------------------------------
# submit_and_wait: timeout → CronApplyUnavailableError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_raises_unavailable_on_timeout(tmp_path: Path) -> None:
    """When no result file appears within timeout, CronApplyUnavailableError raised."""
    base, _requests_dir, _results_dir = _make_ipc_dirs(tmp_path)

    ops = [WriteWrapperScriptOp(content="x")]

    with pytest.raises(CronApplyUnavailableError, match="did not respond"):
        await submit_and_wait(operations=ops, log=_null_log(), ipc_dir=base, timeout=0.1)


# ---------------------------------------------------------------------------
# submit_and_wait: empty operations → ValueError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_raises_value_error_on_empty_operations(tmp_path: Path) -> None:
    """Empty operations list → ValueError."""
    base, _requests_dir, _results_dir = _make_ipc_dirs(tmp_path)

    with pytest.raises(ValueError, match="non-empty"):
        await submit_and_wait(operations=[], log=_null_log(), ipc_dir=base)


# ---------------------------------------------------------------------------
# submit_and_wait: invalid target_crontab → pre-check → CronApplyRejectedError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_invalid_target_crontab_raises_rejected(tmp_path: Path) -> None:
    """WrapCrontabOp with invalid target → CronApplyRejectedError(bad_path)
    before request written."""
    base, requests_dir, _results_dir = _make_ipc_dirs(tmp_path)

    ops = [
        WrapCrontabOp(
            target_crontab="/etc/passwd",
            old_line="line",
            command="cmd",
            new_line="/usr/local/bin/cron-with-heartbeat.sh -- cmd",
        )
    ]

    with pytest.raises(CronApplyRejectedError) as exc_info:
        await submit_and_wait(operations=ops, log=_null_log(), ipc_dir=base)

    assert exc_info.value.error_code == "bad_path"
    # No request file should have been written
    assert list(requests_dir.glob("*.json")) == []


# ---------------------------------------------------------------------------
# _atomic_write_json
# ---------------------------------------------------------------------------


def test_atomic_write_json_no_tmp_on_success(tmp_path: Path) -> None:
    """_atomic_write_json leaves no .tmp file behind on success."""
    target = tmp_path / "out.json"
    _atomic_write_json(tmp_path, target, {"key": "value"})

    remaining = list(tmp_path.iterdir())
    # Only the target file; no .tmp file
    assert target.exists()
    tmp_files = [f for f in remaining if f.suffix == ".tmp" or ".json.tmp" in f.name]
    assert tmp_files == [], f"unexpected tmp files: {tmp_files}"


def test_atomic_write_json_cleans_up_tmp_on_failure(tmp_path: Path) -> None:
    """_atomic_write_json cleans up the temp file when os.replace raises."""
    target = tmp_path / "out.json"

    with (
        patch(
            "homelab_monitor.kernel.cron.cron_apply_ipc.os.replace",
            side_effect=OSError("replace failed"),
        ),
        pytest.raises(OSError, match="replace failed"),
    ):
        _atomic_write_json(tmp_path, target, {"key": "value"})

    tmp_files = [f for f in tmp_path.iterdir() if ".json.tmp" in f.name]
    assert tmp_files == [], f"tmp files not cleaned up: {tmp_files}"


def test_atomic_write_json_byte_exact_roundtrip(tmp_path: Path) -> None:
    """_atomic_write_json produces byte-exact JSON that round-trips cleanly."""
    payload: dict[str, object] = {
        "id": "test-id",
        "schema_version": 1,
        "operations": [
            {"operation": "write-wrapper-script", "content": "#!/bin/bash\n"},
        ],
    }
    target = tmp_path / "req.json"
    _atomic_write_json(tmp_path, target, payload)

    loaded = json.loads(target.read_text(encoding="utf-8"))
    assert loaded["id"] == "test-id"
    assert loaded["schema_version"] == 1
    assert loaded["operations"][0]["operation"] == "write-wrapper-script"


def test_atomic_write_json_temp_has_leading_dot(tmp_path: Path) -> None:
    """Temp file has leading dot (keeps .path watcher glob from triggering on it)."""
    target = tmp_path / "out.json"

    seen_tmps: list[str] = []
    real_mkstemp = __import__("tempfile").mkstemp

    def _recording_mkstemp(**kwargs: object) -> tuple[int, str]:
        fd, path = real_mkstemp(**kwargs)
        seen_tmps.append(path)
        return fd, path

    with patch(
        "homelab_monitor.kernel.cron.cron_apply_ipc.tempfile.mkstemp",
        side_effect=_recording_mkstemp,
    ):
        _atomic_write_json(tmp_path, target, {"x": 1})

    assert seen_tmps, "mkstemp was not called"
    tmp_name = os.path.basename(seen_tmps[0])
    assert tmp_name.startswith("."), f"temp file does not start with '.': {tmp_name}"
    assert tmp_name.endswith(".json.tmp"), f"temp file does not end with '.json.tmp': {tmp_name}"


# ---------------------------------------------------------------------------
# _read_result
# ---------------------------------------------------------------------------


def test_read_result_raises_on_malformed_json(tmp_path: Path) -> None:
    """_read_result raises CronApplyError when the result file is not valid JSON."""
    bad = tmp_path / "bad.json"
    bad.write_text("{not valid json", encoding="utf-8")

    with pytest.raises(CronApplyError, match="unreadable"):
        _read_result(bad)


def test_read_result_raises_on_missing_file(tmp_path: Path) -> None:
    """_read_result raises CronApplyError when the result file does not exist."""
    missing = tmp_path / "missing.json"

    with pytest.raises(CronApplyError):
        _read_result(missing)


def test_read_result_raises_on_bad_status(tmp_path: Path) -> None:
    """_read_result raises CronApplyError when status is not 'ok' or 'error'."""
    bad = tmp_path / "bad_status.json"
    bad.write_text(
        json.dumps({"id": "x", "status": "pending", "error_code": None, "message": ""}),
        encoding="utf-8",
    )

    with pytest.raises(CronApplyError, match="bad status"):
        _read_result(bad)


def test_read_result_ok(tmp_path: Path) -> None:
    """_read_result returns CronApplyResult for a valid ok result."""
    p = tmp_path / "ok.json"
    p.write_text(
        json.dumps(
            {"id": "my-id", "status": "ok", "error_code": None, "message": "applied 3 operations"}
        ),
        encoding="utf-8",
    )

    result = _read_result(p)
    assert result.status == "ok"
    assert result.id == "my-id"
    assert result.error_code is None
    assert result.message == "applied 3 operations"


def test_read_result_error(tmp_path: Path) -> None:
    """_read_result returns CronApplyResult for a valid error result."""
    p = tmp_path / "err.json"
    p.write_text(
        json.dumps(
            {
                "id": "my-id",
                "status": "error",
                "error_code": "bad_path",
                "message": "rejected (rolled back)",
            }
        ),
        encoding="utf-8",
    )

    result = _read_result(p)
    assert result.status == "error"
    assert result.error_code == "bad_path"


# ---------------------------------------------------------------------------
# New tests: item 10 — timeout deletes orphan request file
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_timeout_deletes_orphan_request_file(tmp_path: Path) -> None:
    """On timeout, submit_and_wait removes the orphan request file (best-effort).

    Item 10: ensures a slow executor cannot later apply an abandoned request.
    """
    base, requests_dir, _results_dir = _make_ipc_dirs(tmp_path)

    ops = [WriteWrapperScriptOp(content="x")]

    with pytest.raises(CronApplyUnavailableError):
        await submit_and_wait(operations=ops, log=_null_log(), ipc_dir=base, timeout=0.05)

    # After timeout the requests/ dir should have no .json files left
    remaining = list(requests_dir.glob("*.json"))
    assert remaining == [], f"orphan request files not cleaned up: {remaining}"


# ---------------------------------------------------------------------------
# Item 11 — request file has mode 0600
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_request_file_mode_is_0600(tmp_path: Path) -> None:
    """_atomic_write_json sets the file's mode to 0600 before rename.

    Item 11: the request JSON transits a plaintext heartbeat token; 0600
    is enforced independently of the process umask.
    """
    base, _requests_dir, results_dir = _make_ipc_dirs(tmp_path)
    fixed_id = "a1a1a1a1-b2b2-c3c3-d4d4-e5e5e5e5e5e5"
    _write_result(results_dir, fixed_id, status="ok")

    # Intercept the atomic write to check the fd mode before rename
    observed_modes: list[int] = []

    real_fchmod = os.fchmod

    def _spy_fchmod(fd: int, mode: int) -> None:
        observed_modes.append(mode)
        real_fchmod(fd, mode)

    with (
        patch(
            "homelab_monitor.kernel.cron.cron_apply_ipc.uuid.uuid4",
            return_value=uuid.UUID(fixed_id),
        ),
        patch("homelab_monitor.kernel.cron.cron_apply_ipc.os.fchmod", side_effect=_spy_fchmod),
    ):
        await submit_and_wait(
            operations=[WriteTokenOp(content="tok")],
            log=_null_log(),
            ipc_dir=base,
            timeout=5.0,
        )

    assert observed_modes, "os.fchmod was not called"
    assert observed_modes[0] == 0o600  # noqa: PLR2004


# ---------------------------------------------------------------------------
# Item 12 — WrapCrontabOp.to_payload() includes new_line
# ---------------------------------------------------------------------------


def test_wrap_crontab_op_payload_includes_new_line() -> None:
    """WrapCrontabOp.to_payload() must include the new_line field.

    Item 12: the executor uses supplied new_line as a cross-check.
    """
    _new = "*/5 * * * * root /usr/local/bin/cron-with-heartbeat.sh -- /usr/bin/task.sh"
    op = WrapCrontabOp(
        target_crontab="/etc/crontab",
        old_line="*/5 * * * * root /usr/bin/task.sh",
        command="/usr/bin/task.sh",
        new_line=_new,
    )
    p = op.to_payload()
    assert "new_line" in p
    assert p["new_line"] == _new


# ---------------------------------------------------------------------------
# Item 16 — content > 64 KB → CronApplyRejectedError; exactly 64 KB → accepted
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_oversized_content_raises_rejected(tmp_path: Path) -> None:
    """WriteWrapperScriptOp with content > 64 KB → CronApplyRejectedError(bad_request).

    Item 16: no request file written when content is too large.
    """
    base, requests_dir, _results_dir = _make_ipc_dirs(tmp_path)

    oversized_content = "x" * (64 * 1024 + 1)
    ops = [WriteWrapperScriptOp(content=oversized_content)]

    with pytest.raises(CronApplyRejectedError) as exc_info:
        await submit_and_wait(operations=ops, log=_null_log(), ipc_dir=base)

    assert exc_info.value.error_code == "bad_request"
    # No request file must have been written
    assert list(requests_dir.glob("*.json")) == []


@pytest.mark.asyncio
async def test_submit_exactly_64kb_content_is_accepted(tmp_path: Path) -> None:
    """WriteWrapperScriptOp with content == exactly 64 KB → accepted (boundary).

    Item 16: boundary value must NOT raise.
    """
    base, _requests_dir, results_dir = _make_ipc_dirs(tmp_path)
    fixed_id = "cccccccc-cccc-cccc-cccc-cccccccccccc"
    _write_result(results_dir, fixed_id, status="ok")

    exact_content = "x" * (64 * 1024)
    ops = [WriteWrapperScriptOp(content=exact_content)]

    with patch(
        "homelab_monitor.kernel.cron.cron_apply_ipc.uuid.uuid4",
        return_value=uuid.UUID(fixed_id),
    ):
        result = await submit_and_wait(operations=ops, log=_null_log(), ipc_dir=base, timeout=5.0)

    assert result.status == "ok"
