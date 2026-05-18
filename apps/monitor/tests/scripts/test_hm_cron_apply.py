"""Tests for scripts/hm-cron-apply.sh (STAGE-002-009).

Each test builds a temporary IPC dir + fake filesystem root, writes a
request JSON into requests/, runs the script via subprocess (with
HM_CRON_APPLY_IPC_DIR + HM_CRON_APPLY_ROOT pointed at tmp dirs), then
asserts the result file and any on-disk state. Mirrors the pattern from
tests/test_cron_wrapper_script.py.

NOT counted toward kernel coverage (this is a bash-script harness).
"""

from __future__ import annotations

import json
import os
import subprocess
import uuid
from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SCRIPT = Path(__file__).parents[4] / "scripts" / "hm-cron-apply.sh"
assert _SCRIPT.exists(), f"apply script not found at {_SCRIPT}"

# The apply script hard-codes this prefix for wrap-crontab re-derivation.
_WRAPPER_PREFIX = "/usr/local/bin/cron-with-heartbeat.sh -- "


def _run_script(
    ipc_dir: Path,
    apply_root: Path,
    *,
    timeout: int = 15,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["HM_CRON_APPLY_IPC_DIR"] = str(ipc_dir)
    env["HM_CRON_APPLY_ROOT"] = str(apply_root)
    return subprocess.run(
        ["bash", str(_SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _make_ipc(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Create ipc/requests + ipc/results under tmp_path."""
    ipc = tmp_path / "ipc"
    req = ipc / "requests"
    res = ipc / "results"
    req.mkdir(parents=True)
    res.mkdir(parents=True)
    return ipc, req, res


def _make_root(tmp_path: Path) -> Path:
    """Create a minimal fake filesystem root under tmp_path/root."""
    root = tmp_path / "root"
    (root / "var" / "spool" / "cron" / "crontabs").mkdir(parents=True)
    (root / "etc").mkdir(parents=True)
    (root / "usr" / "local" / "bin").mkdir(parents=True)
    return root


def _write_request(req_dir: Path, operations: list[dict[str, Any]]) -> str:
    req_id = str(uuid.uuid4())
    payload = {
        "id": req_id,
        "schema_version": 1,
        "operations": operations,
    }
    (req_dir / f"{req_id}.json").write_text(json.dumps(payload), encoding="utf-8")
    return req_id


def _read_result(res_dir: Path, req_id: str) -> dict[str, Any]:
    path = res_dir / f"{req_id}.json"
    assert path.exists(), f"result file not found: {path}"
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# wrap-crontab operation
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_wrap_user_crontab(tmp_path: Path) -> None:
    """Single wrap-crontab op on a user crontab → status=ok, line wrapped."""
    ipc, req, res = _make_ipc(tmp_path)
    root = _make_root(tmp_path)

    spool = root / "var" / "spool" / "cron" / "crontabs"
    ct = spool / "alice"
    old_line = "*/10 * * * * /usr/bin/task.sh --arg"
    ct.write_text(old_line + "\n", encoding="utf-8")
    ct.chmod(0o600)

    req_id = _write_request(
        req,
        [
            {
                "operation": "wrap-crontab",
                "target_crontab": "crontab:alice",
                "old_line": old_line,
                "command": "/usr/bin/task.sh --arg",
            }
        ],
    )

    _run_script(ipc, root)

    result = _read_result(res, req_id)
    assert result["status"] == "ok", result
    new_content = ct.read_text(encoding="utf-8")
    assert _WRAPPER_PREFIX + "/usr/bin/task.sh --arg" in new_content
    assert oct(ct.stat().st_mode & 0o777) == oct(0o600)


@pytest.mark.slow
def test_wrap_system_crontab(tmp_path: Path) -> None:
    """wrap-crontab on /etc/crontab preserves USER field, wraps command only."""
    ipc, req, res = _make_ipc(tmp_path)
    root = _make_root(tmp_path)

    etc_ct = root / "etc" / "crontab"
    old_line = "*/5 * * * * root /usr/bin/backup.sh --full"
    etc_ct.write_text("# header\n" + old_line + "\n", encoding="utf-8")

    req_id = _write_request(
        req,
        [
            {
                "operation": "wrap-crontab",
                "target_crontab": "/etc/crontab",
                "old_line": old_line,
                "command": "/usr/bin/backup.sh --full",
            }
        ],
    )

    _run_script(ipc, root)

    result = _read_result(res, req_id)
    assert result["status"] == "ok", result
    new_content = etc_ct.read_text(encoding="utf-8")
    assert "root" in new_content  # USER field preserved
    assert "*/5 * * * *" in new_content  # schedule preserved
    assert _WRAPPER_PREFIX + "/usr/bin/backup.sh --full" in new_content


@pytest.mark.slow
def test_wrap_cron_d(tmp_path: Path) -> None:
    """wrap-crontab on /etc/cron.d/foo → status=ok."""
    ipc, req, res = _make_ipc(tmp_path)
    root = _make_root(tmp_path)

    cron_d = root / "etc" / "cron.d"
    cron_d.mkdir(parents=True, exist_ok=True)
    cron_file = cron_d / "foo"
    old_line = "0 3 * * * root /usr/sbin/cleanup"
    cron_file.write_text(old_line + "\n", encoding="utf-8")

    req_id = _write_request(
        req,
        [
            {
                "operation": "wrap-crontab",
                "target_crontab": "/etc/cron.d/foo",
                "old_line": old_line,
                "command": "/usr/sbin/cleanup",
            }
        ],
    )

    _run_script(ipc, root)

    result = _read_result(res, req_id)
    assert result["status"] == "ok", result
    assert _WRAPPER_PREFIX + "/usr/sbin/cleanup" in cron_file.read_text(encoding="utf-8")


@pytest.mark.slow
def test_reject_bad_path(tmp_path: Path) -> None:
    """target_crontab=/etc/passwd → error bad_path, no file touched."""
    ipc, req, res = _make_ipc(tmp_path)
    root = _make_root(tmp_path)

    req_id = _write_request(
        req,
        [
            {
                "operation": "wrap-crontab",
                "target_crontab": "/etc/passwd",
                "old_line": "any",
                "command": "any",
            }
        ],
    )

    _run_script(ipc, root)

    result = _read_result(res, req_id)
    assert result["status"] == "error"
    assert result["error_code"] == "bad_path"


@pytest.mark.slow
def test_reject_path_traversal(tmp_path: Path) -> None:
    """/etc/cron.d/../shadow → bad_path."""
    ipc, req, res = _make_ipc(tmp_path)
    root = _make_root(tmp_path)

    req_id = _write_request(
        req,
        [
            {
                "operation": "wrap-crontab",
                "target_crontab": "/etc/cron.d/../shadow",
                "old_line": "any",
                "command": "any",
            }
        ],
    )

    _run_script(ipc, root)

    result = _read_result(res, req_id)
    assert result["status"] == "error"
    assert result["error_code"] == "bad_path"


@pytest.mark.slow
def test_reject_user_traversal(tmp_path: Path) -> None:
    """crontab:../../etc/passwd → bad_path."""
    ipc, req, res = _make_ipc(tmp_path)
    root = _make_root(tmp_path)

    req_id = _write_request(
        req,
        [
            {
                "operation": "wrap-crontab",
                "target_crontab": "crontab:../../etc/passwd",
                "old_line": "any",
                "command": "any",
            }
        ],
    )

    _run_script(ipc, root)

    result = _read_result(res, req_id)
    assert result["status"] == "error"
    assert result["error_code"] == "bad_path"


@pytest.mark.slow
def test_reject_line_not_present(tmp_path: Path) -> None:
    """old_line not in crontab → line_not_found, crontab unchanged."""
    ipc, req, res = _make_ipc(tmp_path)
    root = _make_root(tmp_path)

    etc_ct = root / "etc" / "crontab"
    etc_ct.write_text("# nothing matching\n", encoding="utf-8")
    original = etc_ct.read_text(encoding="utf-8")

    req_id = _write_request(
        req,
        [
            {
                "operation": "wrap-crontab",
                "target_crontab": "/etc/crontab",
                "old_line": "*/5 * * * * root /usr/bin/missing.sh",
                "command": "/usr/bin/missing.sh",
            }
        ],
    )

    _run_script(ipc, root)

    result = _read_result(res, req_id)
    assert result["status"] == "error"
    assert result["error_code"] == "line_not_found"
    assert etc_ct.read_text(encoding="utf-8") == original


@pytest.mark.slow
def test_reject_already_wrapped(tmp_path: Path) -> None:
    """Line already contains the wrapper prefix → already_wrapped."""
    ipc, req, res = _make_ipc(tmp_path)
    root = _make_root(tmp_path)

    etc_ct = root / "etc" / "crontab"
    old_line = f"*/5 * * * * root {_WRAPPER_PREFIX}/usr/bin/task.sh"
    etc_ct.write_text(old_line + "\n", encoding="utf-8")

    req_id = _write_request(
        req,
        [
            {
                "operation": "wrap-crontab",
                "target_crontab": "/etc/crontab",
                "old_line": old_line,
                "command": "/usr/bin/task.sh",
            }
        ],
    )

    _run_script(ipc, root)

    result = _read_result(res, req_id)
    assert result["status"] == "error"
    assert result["error_code"] == "already_wrapped"


@pytest.mark.slow
def test_command_not_in_old_line(tmp_path: Path) -> None:
    """command field not a substring of old_line → bad_request."""
    ipc, req, res = _make_ipc(tmp_path)
    root = _make_root(tmp_path)

    etc_ct = root / "etc" / "crontab"
    old_line = "*/5 * * * * root /usr/bin/task.sh"
    etc_ct.write_text(old_line + "\n", encoding="utf-8")

    req_id = _write_request(
        req,
        [
            {
                "operation": "wrap-crontab",
                "target_crontab": "/etc/crontab",
                "old_line": old_line,
                "command": "/usr/bin/other.sh",  # NOT in old_line
            }
        ],
    )

    _run_script(ipc, root)

    result = _read_result(res, req_id)
    assert result["status"] == "error"
    assert result["error_code"] == "bad_request"


@pytest.mark.slow
def test_only_target_line_changed(tmp_path: Path) -> None:
    """When wrapping line 2 of 3, lines 1 and 3 are byte-identical."""
    ipc, req, res = _make_ipc(tmp_path)
    root = _make_root(tmp_path)

    etc_ct = root / "etc" / "crontab"
    line1 = "0 1 * * * root /usr/bin/job1.sh"
    line2 = "0 2 * * * root /usr/bin/job2.sh"
    line3 = "0 3 * * * root /usr/bin/job3.sh"
    etc_ct.write_text("\n".join([line1, line2, line3, ""]), encoding="utf-8")

    req_id = _write_request(
        req,
        [
            {
                "operation": "wrap-crontab",
                "target_crontab": "/etc/crontab",
                "old_line": line2,
                "command": "/usr/bin/job2.sh",
            }
        ],
    )

    _run_script(ipc, root)

    result = _read_result(res, req_id)
    assert result["status"] == "ok", result
    lines = etc_ct.read_text(encoding="utf-8").splitlines()
    assert lines[0] == line1
    assert _WRAPPER_PREFIX + "/usr/bin/job2.sh" in lines[1]
    assert lines[2] == line3


# ---------------------------------------------------------------------------
# write-wrapper-script operation
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_write_wrapper_script(tmp_path: Path) -> None:
    """write-wrapper-script writes content to the fixed path with mode 0755."""
    ipc, req, res = _make_ipc(tmp_path)
    root = _make_root(tmp_path)

    content = "#!/bin/bash\necho wrapper\n"
    req_id = _write_request(req, [{"operation": "write-wrapper-script", "content": content}])

    _run_script(ipc, root)

    result = _read_result(res, req_id)
    assert result["status"] == "ok", result
    dest = root / "usr" / "local" / "bin" / "cron-with-heartbeat.sh"
    assert dest.exists()
    # printf '%s' in the apply script strips the trailing newline from content
    assert dest.read_text(encoding="utf-8") == content.rstrip("\n")
    assert oct(dest.stat().st_mode & 0o777) == oct(0o755)


@pytest.mark.slow
def test_write_wrapper_script_creates_parent(tmp_path: Path) -> None:
    """write-wrapper-script creates parent dir if absent."""
    ipc, req, res = _make_ipc(tmp_path)
    root = tmp_path / "bare-root"
    root.mkdir()
    # Do NOT create usr/local/bin

    content = "#!/bin/bash\n"
    req_id = _write_request(req, [{"operation": "write-wrapper-script", "content": content}])

    _run_script(ipc, root)

    result = _read_result(res, req_id)
    assert result["status"] == "ok", result
    dest = root / "usr" / "local" / "bin" / "cron-with-heartbeat.sh"
    assert dest.exists()


@pytest.mark.slow
def test_reject_wrapper_op_with_path_field(tmp_path: Path) -> None:
    """write-wrapper-script op carrying a 'path' field → bad_request."""
    ipc, req, res = _make_ipc(tmp_path)
    root = _make_root(tmp_path)

    req_id = _write_request(
        req,
        [
            {
                "operation": "write-wrapper-script",
                "content": "#!/bin/bash\n",
                "path": "/etc/evil",
            }
        ],
    )

    _run_script(ipc, root)

    result = _read_result(res, req_id)
    assert result["status"] == "error"
    assert result["error_code"] == "bad_request"


@pytest.mark.slow
def test_reject_wrapper_op_missing_content(tmp_path: Path) -> None:
    """write-wrapper-script op with no content → bad_request."""
    ipc, req, res = _make_ipc(tmp_path)
    root = _make_root(tmp_path)

    req_id = _write_request(req, [{"operation": "write-wrapper-script"}])

    _run_script(ipc, root)

    result = _read_result(res, req_id)
    assert result["status"] == "error"
    assert result["error_code"] == "bad_request"


# ---------------------------------------------------------------------------
# write-token operation
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_write_token(tmp_path: Path) -> None:
    """write-token writes content to fixed path with mode 0644."""
    ipc, req, res = _make_ipc(tmp_path)
    root = _make_root(tmp_path)
    (root / "etc" / "homelab-monitor").mkdir(parents=True, exist_ok=True)

    token = "hb_abc123xyz"
    req_id = _write_request(req, [{"operation": "write-token", "content": token}])

    _run_script(ipc, root)

    result = _read_result(res, req_id)
    assert result["status"] == "ok", result
    dest = root / "etc" / "homelab-monitor" / "heartbeat.token"
    assert dest.exists()
    assert dest.read_text(encoding="utf-8") == token
    assert oct(dest.stat().st_mode & 0o777) == oct(0o644)


@pytest.mark.slow
def test_write_token_creates_etc_homelab_monitor(tmp_path: Path) -> None:
    """write-token creates /etc/homelab-monitor dir if absent."""
    ipc, req, res = _make_ipc(tmp_path)
    root = tmp_path / "bare-root"
    root.mkdir()
    # Do NOT create etc/homelab-monitor

    req_id = _write_request(req, [{"operation": "write-token", "content": "tok"}])

    _run_script(ipc, root)

    result = _read_result(res, req_id)
    assert result["status"] == "ok", result
    dest = root / "etc" / "homelab-monitor" / "heartbeat.token"
    assert dest.exists()


@pytest.mark.slow
def test_reject_token_op_with_path_field(tmp_path: Path) -> None:
    """write-token op carrying a 'target' field → bad_request."""
    ipc, req, res = _make_ipc(tmp_path)
    root = _make_root(tmp_path)

    req_id = _write_request(
        req,
        [
            {
                "operation": "write-token",
                "content": "tok",
                "target": "/etc/evil",
            }
        ],
    )

    _run_script(ipc, root)

    result = _read_result(res, req_id)
    assert result["status"] == "error"
    assert result["error_code"] == "bad_request"


@pytest.mark.slow
def test_reject_token_op_missing_content(tmp_path: Path) -> None:
    """write-token op with no content → bad_request."""
    ipc, req, res = _make_ipc(tmp_path)
    root = _make_root(tmp_path)

    req_id = _write_request(req, [{"operation": "write-token"}])

    _run_script(ipc, root)

    result = _read_result(res, req_id)
    assert result["status"] == "error"
    assert result["error_code"] == "bad_request"


# ---------------------------------------------------------------------------
# Multi-operation + atomic rollback
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_full_install_three_ops(tmp_path: Path) -> None:
    """Full 3-op list → status=ok; all three files in expected state."""
    ipc, req, res = _make_ipc(tmp_path)
    root = _make_root(tmp_path)

    etc_ct = root / "etc" / "crontab"
    old_line = "*/5 * * * * root /usr/bin/backup.sh"
    etc_ct.write_text(old_line + "\n", encoding="utf-8")

    wrapper_content = "#!/bin/bash\necho wrapper\n"
    token_content = "hb_mytoken"

    req_id = _write_request(
        req,
        [
            {"operation": "write-wrapper-script", "content": wrapper_content},
            {"operation": "write-token", "content": token_content},
            {
                "operation": "wrap-crontab",
                "target_crontab": "/etc/crontab",
                "old_line": old_line,
                "command": "/usr/bin/backup.sh",
            },
        ],
    )

    _run_script(ipc, root)

    result = _read_result(res, req_id)
    assert result["status"] == "ok", result

    wrapper_dest = root / "usr" / "local" / "bin" / "cron-with-heartbeat.sh"
    token_dest = root / "etc" / "homelab-monitor" / "heartbeat.token"
    # printf '%s' strips trailing newline from content
    assert wrapper_dest.read_text(encoding="utf-8") == wrapper_content.rstrip("\n")
    assert token_dest.read_text(encoding="utf-8") == token_content
    assert _WRAPPER_PREFIX + "/usr/bin/backup.sh" in etc_ct.read_text(encoding="utf-8")


@pytest.mark.slow
def test_rollback_when_third_op_fails(tmp_path: Path) -> None:
    """3-op list where wrap-crontab fails → status=error; wrapper + token rolled back."""
    ipc, req, res = _make_ipc(tmp_path)
    root = _make_root(tmp_path)

    etc_ct = root / "etc" / "crontab"
    etc_ct.write_text("# empty\n", encoding="utf-8")  # no matching old_line

    req_id = _write_request(
        req,
        [
            {"operation": "write-wrapper-script", "content": "#!/bin/bash\n"},
            {"operation": "write-token", "content": "hb_tok"},
            {
                "operation": "wrap-crontab",
                "target_crontab": "/etc/crontab",
                "old_line": "*/5 * * * * root /usr/bin/nothere.sh",
                "command": "/usr/bin/nothere.sh",
            },
        ],
    )

    _run_script(ipc, root)

    result = _read_result(res, req_id)
    assert result["status"] == "error"
    assert result["error_code"] == "line_not_found"

    # Rollback: wrapper + token should not exist (they were freshly created)
    wrapper_dest = root / "usr" / "local" / "bin" / "cron-with-heartbeat.sh"
    token_dest = root / "etc" / "homelab-monitor" / "heartbeat.token"
    assert not wrapper_dest.exists(), "wrapper not rolled back"
    assert not token_dest.exists(), "token not rolled back"
    # Crontab unchanged
    assert etc_ct.read_text(encoding="utf-8") == "# empty\n"


@pytest.mark.slow
def test_rollback_restores_preexisting_token(tmp_path: Path) -> None:
    """Pre-existing token is restored (not deleted) on rollback."""
    ipc, req, res = _make_ipc(tmp_path)
    root = _make_root(tmp_path)

    # Pre-create token with known content
    token_dir = root / "etc" / "homelab-monitor"
    token_dir.mkdir(parents=True, exist_ok=True)
    token_file = token_dir / "heartbeat.token"
    token_file.write_text("OLD_TOKEN", encoding="utf-8")

    etc_ct = root / "etc" / "crontab"
    etc_ct.write_text("# empty\n", encoding="utf-8")

    req_id = _write_request(
        req,
        [
            {"operation": "write-wrapper-script", "content": "#!/bin/bash\n"},
            {"operation": "write-token", "content": "NEW_TOKEN"},
            {
                "operation": "wrap-crontab",
                "target_crontab": "/etc/crontab",
                "old_line": "nothere",
                "command": "nothere",
            },
        ],
    )

    _run_script(ipc, root)

    result = _read_result(res, req_id)
    assert result["status"] == "error"
    # Token restored to OLD value
    assert token_file.read_text(encoding="utf-8") == "OLD_TOKEN"


@pytest.mark.slow
def test_rollback_deletes_fresh_wrapper(tmp_path: Path) -> None:
    """Wrapper did not pre-exist; on rollback it is deleted."""
    ipc, req, res = _make_ipc(tmp_path)
    root = _make_root(tmp_path)

    etc_ct = root / "etc" / "crontab"
    etc_ct.write_text("# empty\n", encoding="utf-8")

    wrapper_dest = root / "usr" / "local" / "bin" / "cron-with-heartbeat.sh"
    assert not wrapper_dest.exists()

    req_id = _write_request(
        req,
        [
            {"operation": "write-wrapper-script", "content": "#!/bin/bash\n"},
            {"operation": "write-token", "content": "tok"},
            {
                "operation": "wrap-crontab",
                "target_crontab": "/etc/crontab",
                "old_line": "nothere",
                "command": "nothere",
            },
        ],
    )

    _run_script(ipc, root)

    _read_result(res, req_id)
    assert not wrapper_dest.exists(), "freshly-created wrapper should be deleted on rollback"


@pytest.mark.slow
def test_ok_result_message_names_op_count(tmp_path: Path) -> None:
    """Successful 3-op run → result message mentions operation count."""
    ipc, req, res = _make_ipc(tmp_path)
    root = _make_root(tmp_path)

    etc_ct = root / "etc" / "crontab"
    old_line = "0 1 * * * root /usr/bin/job.sh"
    etc_ct.write_text(old_line + "\n", encoding="utf-8")

    req_id = _write_request(
        req,
        [
            {"operation": "write-wrapper-script", "content": "#!/bin/bash\n"},
            {"operation": "write-token", "content": "tok"},
            {
                "operation": "wrap-crontab",
                "target_crontab": "/etc/crontab",
                "old_line": old_line,
                "command": "/usr/bin/job.sh",
            },
        ],
    )

    _run_script(ipc, root)

    result = _read_result(res, req_id)
    assert result["status"] == "ok", result
    assert "3" in result["message"]


# ---------------------------------------------------------------------------
# Request envelope / lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_reject_malformed_json(tmp_path: Path) -> None:
    """Request file with invalid JSON → bad_request result."""
    ipc, req, res = _make_ipc(tmp_path)
    root = _make_root(tmp_path)

    req_id = str(uuid.uuid4())
    (req / f"{req_id}.json").write_text("{not valid json{{", encoding="utf-8")

    _run_script(ipc, root)

    result = _read_result(res, req_id)
    assert result["status"] == "error"
    assert result["error_code"] == "bad_request"


@pytest.mark.slow
def test_reject_bad_schema_version(tmp_path: Path) -> None:
    """schema_version=999 → bad_request."""
    ipc, req, res = _make_ipc(tmp_path)
    root = _make_root(tmp_path)

    req_id = str(uuid.uuid4())
    payload = {
        "id": req_id,
        "schema_version": 999,
        "operations": [{"operation": "write-token", "content": "x"}],
    }
    (req / f"{req_id}.json").write_text(json.dumps(payload), encoding="utf-8")

    _run_script(ipc, root)

    result = _read_result(res, req_id)
    assert result["status"] == "error"
    assert result["error_code"] == "bad_request"


@pytest.mark.slow
def test_reject_empty_operations(tmp_path: Path) -> None:
    """operations: [] → bad_request."""
    ipc, req, res = _make_ipc(tmp_path)
    root = _make_root(tmp_path)

    req_id = str(uuid.uuid4())
    payload: dict[str, object] = {"id": req_id, "schema_version": 1, "operations": []}
    (req / f"{req_id}.json").write_text(json.dumps(payload), encoding="utf-8")

    _run_script(ipc, root)

    result = _read_result(res, req_id)
    assert result["status"] == "error"
    assert result["error_code"] == "bad_request"


@pytest.mark.slow
def test_reject_unknown_operation(tmp_path: Path) -> None:
    """operation='delete' → bad_request."""
    ipc, req, res = _make_ipc(tmp_path)
    root = _make_root(tmp_path)

    req_id = _write_request(req, [{"operation": "delete", "target": "/etc/crontab"}])

    _run_script(ipc, root)

    result = _read_result(res, req_id)
    assert result["status"] == "error"
    assert result["error_code"] == "bad_request"


@pytest.mark.slow
def test_request_file_deleted_after_processing(tmp_path: Path) -> None:
    """After processing, requests/<id>.json deleted; results/<id>.json present."""
    ipc, req, res = _make_ipc(tmp_path)
    root = _make_root(tmp_path)

    req_id = _write_request(req, [{"operation": "write-token", "content": "tok"}])
    req_file = req / f"{req_id}.json"

    _run_script(ipc, root)

    assert not req_file.exists(), "request file should be deleted after processing"
    assert (res / f"{req_id}.json").exists(), "result file should exist"


@pytest.mark.slow
def test_idempotent_when_result_exists(tmp_path: Path) -> None:
    """Pre-existing result file → request skipped (no re-apply), request deleted."""
    ipc, req, res = _make_ipc(tmp_path)
    root = _make_root(tmp_path)

    etc_ct = root / "etc" / "crontab"
    etc_ct.write_text("# original\n", encoding="utf-8")
    original = etc_ct.read_text(encoding="utf-8")

    req_id = str(uuid.uuid4())
    # Pre-write a result claiming ok
    (res / f"{req_id}.json").write_text(
        json.dumps({"id": req_id, "status": "ok", "error_code": None, "message": "pre-existing"}),
        encoding="utf-8",
    )
    # Write a request that would fail if processed
    payload = {
        "id": req_id,
        "schema_version": 1,
        "operations": [
            {
                "operation": "wrap-crontab",
                "target_crontab": "/etc/crontab",
                "old_line": "nonexistent line",
                "command": "nonexistent",
            }
        ],
    }
    req_file = req / f"{req_id}.json"
    req_file.write_text(json.dumps(payload), encoding="utf-8")

    _run_script(ipc, root)

    # Request file deleted (idempotency)
    assert not req_file.exists()
    # Crontab untouched (request was skipped)
    assert etc_ct.read_text(encoding="utf-8") == original


# ---------------------------------------------------------------------------
# Item 13: correct/wrong/absent new_line cross-check
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_wrap_crontab_correct_new_line_accepted(tmp_path: Path) -> None:
    """wrap-crontab with correct supplied new_line → ok, crontab rewritten."""
    ipc, req, res = _make_ipc(tmp_path)
    root = _make_root(tmp_path)

    spool = root / "var" / "spool" / "cron" / "crontabs"
    ct = spool / "alice"
    old_line = "*/10 * * * * /usr/bin/backup.sh"
    ct.write_text(old_line + "\n", encoding="utf-8")

    expected_new_line = "*/10 * * * * " + _WRAPPER_PREFIX + "/usr/bin/backup.sh"
    req_id = _write_request(
        req,
        [
            {
                "operation": "wrap-crontab",
                "target_crontab": "crontab:alice",
                "old_line": old_line,
                "command": "/usr/bin/backup.sh",
                "new_line": expected_new_line,
            }
        ],
    )

    _run_script(ipc, root)

    result = _read_result(res, req_id)
    assert result["status"] == "ok", result
    assert expected_new_line in ct.read_text(encoding="utf-8")


@pytest.mark.slow
def test_wrap_crontab_wrong_new_line_returns_bad_request(tmp_path: Path) -> None:
    """wrap-crontab with disagreeing supplied new_line → bad_request, crontab unchanged."""
    ipc, req, res = _make_ipc(tmp_path)
    root = _make_root(tmp_path)

    spool = root / "var" / "spool" / "cron" / "crontabs"
    ct = spool / "bob"
    old_line = "*/10 * * * * /usr/bin/task.sh"
    ct.write_text(old_line + "\n", encoding="utf-8")
    original_content = ct.read_text(encoding="utf-8")

    wrong_new_line = "*/10 * * * * /WRONG/wrapper -- /usr/bin/task.sh"
    req_id = _write_request(
        req,
        [
            {
                "operation": "wrap-crontab",
                "target_crontab": "crontab:bob",
                "old_line": old_line,
                "command": "/usr/bin/task.sh",
                "new_line": wrong_new_line,
            }
        ],
    )

    _run_script(ipc, root)

    result = _read_result(res, req_id)
    assert result["status"] == "error", result
    assert result["error_code"] == "bad_request"
    # Crontab must be unchanged
    assert ct.read_text(encoding="utf-8") == original_content


@pytest.mark.slow
def test_wrap_crontab_absent_new_line_still_ok(tmp_path: Path) -> None:
    """wrap-crontab without new_line field → ok (backward-compat, no cross-check)."""
    ipc, req, res = _make_ipc(tmp_path)
    root = _make_root(tmp_path)

    spool = root / "var" / "spool" / "cron" / "crontabs"
    ct = spool / "carol"
    old_line = "0 3 * * * /usr/bin/cleanup.sh"
    ct.write_text(old_line + "\n", encoding="utf-8")

    # new_line key is deliberately absent from the request
    req_id = _write_request(
        req,
        [
            {
                "operation": "wrap-crontab",
                "target_crontab": "crontab:carol",
                "old_line": old_line,
                "command": "/usr/bin/cleanup.sh",
                # no "new_line" key
            }
        ],
    )

    _run_script(ipc, root)

    result = _read_result(res, req_id)
    assert result["status"] == "ok", result
    assert _WRAPPER_PREFIX + "/usr/bin/cleanup.sh" in ct.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Item 15: temp sweep on no-request pass
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_temp_sweep_removes_stale_snap_files(tmp_path: Path) -> None:
    """A pre-seeded stale .snap.* temp file in results/ is removed on a no-request pass.

    Item 15 (M2 temp sweep): the executor sweeps leftover .snap.* / *.hmtmp.*
    files from a previous interrupted run when no pending request is present.
    """
    ipc, _req, res = _make_ipc(tmp_path)
    root = _make_root(tmp_path)

    # Seed a stale snap file in results/ (mimics an interrupted previous run)
    stale_snap = res / ".snap.stale_test_file"
    stale_snap.write_text("leftover", encoding="utf-8")

    # Run the script with NO request files — just the sweep pass
    _run_script(ipc, root)

    # The stale snap file must be gone
    assert not stale_snap.exists(), f"stale snap file was not cleaned up: {stale_snap}"


@pytest.mark.slow
def test_temp_sweep_removes_stale_hmtmp_files(tmp_path: Path) -> None:
    """A pre-seeded *.hmtmp.* file beside the fixed wrapper path is removed.

    Item 15 (M2 temp sweep): the executor sweeps leftover *.hmtmp.* files
    beside the FIXED wrapper-script + token paths (where apply_write_file's
    mktemp creates them), not the requests/ dir.
    """
    ipc, _req, _res = _make_ipc(tmp_path)
    root = _make_root(tmp_path)

    # Seed a stale hmtmp file beside the fixed wrapper-script path — the
    # location the executor's mktemp uses and the M2 sweep targets.
    stale_hmtmp = root / "usr" / "local" / "bin" / "cron-with-heartbeat.sh.hmtmp.abc123"
    stale_hmtmp.write_text("leftover", encoding="utf-8")

    # Run with NO request JSON files
    _run_script(ipc, root)

    # The stale hmtmp file must be gone
    assert not stale_hmtmp.exists(), f"stale hmtmp file was not cleaned up: {stale_hmtmp}"
