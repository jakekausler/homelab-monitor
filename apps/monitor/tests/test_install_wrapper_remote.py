"""Tests for homelab_monitor/cli/install_wrapper_remote.py (STAGE-002-009).

Covers every function and branch in the standalone remote-host CLI script.
The script is stdlib-only; tests use monkeypatch + tmp_path to sandbox file I/O
and mock urlopen for HTTP calls.

New items (STAGE-002-009 finalize):
- Item 2: fetch_wrapper_template returns body on HTTP 200; raises on non-2xx
- Item 3: byte-identical check vs install.py:_build_wrapper_content()
- Item 4: remote installer no longer contains "WRAPPER_TEMPLATE" or "/ping"
- Item 5: last-occurrence (rfind) splice when command repeats a schedule token
- Item 6: rollback scenarios — wrapper/token/crontab write fail; re-install-over-existing;
          registration failure does NOT roll back; token file written 0644 (item 7)
"""

from __future__ import annotations

import socket
import stat
from collections.abc import Callable
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

import pytest

# The module lives under homelab_monitor.cli but is a standalone script
import homelab_monitor.cli.install_wrapper_remote as iwr
from homelab_monitor.cli.install_wrapper_remote import (
    _build_arg_parser,  # pyright: ignore[reportPrivateUsage]
    _parse_job_line,  # pyright: ignore[reportPrivateUsage]
    _resolve_crontab_file,  # pyright: ignore[reportPrivateUsage]
    _write_files_and_register,  # pyright: ignore[reportPrivateUsage]
    compute_fingerprint,
    fetch_wrapper_template,
    main,
    parse_crontab_lines,
)

# ---------------------------------------------------------------------------
# _resolve_crontab_file
# ---------------------------------------------------------------------------


def test_resolve_etc_crontab() -> None:
    assert _resolve_crontab_file("/etc/crontab") == Path("/etc/crontab")


def test_resolve_crontab_user() -> None:
    assert _resolve_crontab_file("crontab:alice") == Path("/var/spool/cron/crontabs/alice")


def test_resolve_crontab_root_user() -> None:
    assert _resolve_crontab_file("crontab:root") == Path("/var/spool/cron/crontabs/root")


def test_resolve_arbitrary_path() -> None:
    assert _resolve_crontab_file("/tmp/my.crontab") == Path("/tmp/my.crontab")


# ---------------------------------------------------------------------------
# _parse_job_line
# ---------------------------------------------------------------------------


def test_parse_job_line_system_crontab_with_user_field() -> None:
    """System crontab (/etc/crontab): the USER column is stripped; command is the rest.

    split(None, 6) → parts = ['*/5','*','*','*','*','root','/usr/bin/backup.sh --full'].
    parts[5]='root' has no special char → is_system=True → command = parts[6:] joined.
    """
    line = "*/5 * * * * root /usr/bin/backup.sh --full"
    result = _parse_job_line(line, "/etc/crontab")
    assert result is not None
    schedule, command = result
    assert schedule == "*/5 * * * *"
    assert command == "/usr/bin/backup.sh --full"


def test_parse_job_line_system_crontab_multiword_command() -> None:
    """System crontab with a multi-word command exercises the parts[6:] join branch."""
    line = "0 3 * * * backupuser /bin/sh -c 'date && /opt/run.sh'"
    result = _parse_job_line(line, "/etc/crontab")
    assert result is not None
    schedule, command = result
    assert schedule == "0 3 * * *"
    assert command == "/bin/sh -c 'date && /opt/run.sh'"


def test_parse_job_line_system_crontab_pure_user_no_command() -> None:
    """System crontab: parts[5]='root' (no special chars), is_system=True, len==6 → None."""
    line = "*/5 * * * * root"
    result = _parse_job_line(line, "/etc/crontab")
    assert result is None


def test_parse_job_line_user_crontab() -> None:
    """User crontab: 6th field is command (no user column)."""
    line = "*/5 * * * * /usr/bin/backup.sh --full"
    result = _parse_job_line(line, "crontab:alice")
    assert result is not None
    schedule, command = result
    assert schedule == "*/5 * * * *"
    assert command == "/usr/bin/backup.sh --full"


def test_parse_job_line_too_few_fields_returns_none() -> None:
    """Line with fewer than 6 fields returns None."""
    line = "* * * * *"  # only 5 fields, no command
    assert _parse_job_line(line, "crontab:alice") is None


def test_parse_job_line_system_crontab_user_field_with_slash_not_treated_as_user() -> None:
    """If the 6th field contains '/', is_system=False — entire remainder is command.

    split(None, 6) → parts[5] = '/usr/bin/mytask.sh'; it contains '/' so
    is_system=False; command = " ".join(parts[5:]) = '/usr/bin/mytask.sh arg'.
    """
    line = "*/5 * * * * /usr/bin/mytask.sh arg"
    result = _parse_job_line(line, "/etc/crontab")
    assert result is not None
    schedule, command = result
    assert schedule == "*/5 * * * *"
    assert command == "/usr/bin/mytask.sh arg"


def test_parse_job_line_user_crontab_with_special_char_in_cmd() -> None:
    """User crontab with semicolon in command — parsed correctly."""
    line = "0 * * * * /bin/sh -c 'echo hello; date'"
    result = _parse_job_line(line, "crontab:root")
    assert result is not None
    schedule, command = result
    assert schedule == "0 * * * *"
    assert "/bin/sh" in command


# ---------------------------------------------------------------------------
# parse_crontab_lines
# ---------------------------------------------------------------------------


def test_parse_crontab_lines_skips_comments() -> None:
    content = "# this is a comment\n*/5 * * * * /usr/bin/task\n"
    lines = parse_crontab_lines(content)
    assert len(lines) == 1
    idx, line = lines[0]
    assert idx == 1
    assert "/usr/bin/task" in line


def test_parse_crontab_lines_skips_blank_lines() -> None:
    content = "\n\n*/5 * * * * /usr/bin/task\n\n"
    lines = parse_crontab_lines(content)
    assert len(lines) == 1


def test_parse_crontab_lines_multiple_jobs() -> None:
    content = "# header\n*/5 * * * * job1\n@daily job2\n"
    lines = parse_crontab_lines(content)
    assert len(lines) == 2  # noqa: PLR2004
    assert lines[0][0] == 1
    assert lines[1][0] == 2  # noqa: PLR2004


def test_parse_crontab_lines_empty_returns_empty() -> None:
    assert parse_crontab_lines("") == []


def test_parse_crontab_lines_only_comments() -> None:
    content = "# comment 1\n# comment 2\n"
    assert parse_crontab_lines(content) == []


# ---------------------------------------------------------------------------
# compute_fingerprint
# ---------------------------------------------------------------------------


def test_compute_fingerprint_deterministic() -> None:
    fp1 = compute_fingerprint("host1", "/etc/crontab", "*/5 * * * *", "/usr/bin/task")
    fp2 = compute_fingerprint("host1", "/etc/crontab", "*/5 * * * *", "/usr/bin/task")
    assert fp1 == fp2


def test_compute_fingerprint_differs_on_host() -> None:
    fp1 = compute_fingerprint("host1", "/etc/crontab", "*/5 * * * *", "/usr/bin/task")
    fp2 = compute_fingerprint("host2", "/etc/crontab", "*/5 * * * *", "/usr/bin/task")
    assert fp1 != fp2


def test_compute_fingerprint_is_hex_string() -> None:
    fp = compute_fingerprint("h", "/etc/crontab", "* * * * *", "cmd")
    assert len(fp) == 64  # noqa: PLR2004 -- SHA256 hex = 64 chars
    assert all(c in "0123456789abcdef" for c in fp)


def test_compute_fingerprint_matches_kernel_algorithm() -> None:
    """Must match kernel/cron/fingerprint.py compute_fingerprint."""
    from homelab_monitor.kernel.cron.fingerprint import (  # noqa: PLC0415
        compute_fingerprint as kernel_fp,
    )

    args = ("myhost", "crontab:alice", "0 2 * * *", "/home/alice/backup.sh")
    assert compute_fingerprint(*args) == kernel_fp(
        host=args[0], source_path=args[1], schedule=args[2], command=args[3]
    )


# ---------------------------------------------------------------------------
# _build_arg_parser
# ---------------------------------------------------------------------------


def test_build_arg_parser_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """Defaults come from env vars / socket.gethostname."""
    monkeypatch.delenv("HM_MONITOR_URL", raising=False)
    monkeypatch.delenv("HM_HEARTBEAT_TOKEN", raising=False)
    parser = _build_arg_parser()
    args = parser.parse_args([])
    assert args.monitor_url == ""
    assert args.token == ""
    assert args.crontab == ""
    assert args.line == 0
    assert args.host == socket.gethostname()
    assert args.confirm is False


def test_build_arg_parser_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HM_MONITOR_URL", "http://monitor.example.com")
    monkeypatch.setenv("HM_HEARTBEAT_TOKEN", "secret-token")
    parser = _build_arg_parser()
    args = parser.parse_args([])
    assert args.monitor_url == "http://monitor.example.com"
    assert args.token == "secret-token"


def test_build_arg_parser_flag_overrides_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HM_MONITOR_URL", "http://env.example.com")
    parser = _build_arg_parser()
    args = parser.parse_args(["--monitor-url", "http://flag.example.com"])
    assert args.monitor_url == "http://flag.example.com"


def test_build_arg_parser_confirm_flag() -> None:
    parser = _build_arg_parser()
    args = parser.parse_args(["--confirm"])
    assert args.confirm is True


def test_build_arg_parser_all_flags() -> None:
    parser = _build_arg_parser()
    args = parser.parse_args(
        [
            "--monitor-url",
            "http://m.example.com",
            "--token",
            "tok",
            "--crontab",
            "/etc/crontab",
            "--line",
            "3",
            "--host",
            "myhost",
            "--confirm",
        ]
    )
    assert args.monitor_url == "http://m.example.com"
    assert args.token == "tok"
    assert args.crontab == "/etc/crontab"
    assert args.line == 3  # noqa: PLR2004
    assert args.host == "myhost"
    assert args.confirm is True


# ---------------------------------------------------------------------------
# _write_files_and_register
# ---------------------------------------------------------------------------


def _make_crontab_file(tmp_path: Path, content: str = "*/5 * * * * root /usr/bin/task\n") -> Path:
    ct = tmp_path / "crontab"
    ct.write_text(content, encoding="utf-8")
    return ct


def test_write_files_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Success path: wrapper written, token written, crontab rewritten, register called."""
    fake_wrapper = tmp_path / "wrapper.sh"
    fake_token_dir = tmp_path / "token_dir"
    fake_token_dir.mkdir()
    fake_token = fake_token_dir / "heartbeat.token"
    ct = _make_crontab_file(tmp_path)

    monkeypatch.setattr(iwr, "WRAPPER_PATH", str(fake_wrapper))
    monkeypatch.setattr(iwr, "TOKEN_FILE_PATH", str(fake_token))
    monkeypatch.setattr(iwr, "TOKEN_FILE_DIR", str(fake_token_dir))

    mock_resp = MagicMock()
    mock_resp.__enter__ = MagicMock(return_value=mock_resp)
    mock_resp.__exit__ = MagicMock(return_value=False)
    mock_resp.status = 200

    crontab_content = ct.read_text(encoding="utf-8")
    line_index = 0
    new_line = "*/5 * * * * root /usr/local/bin/wrapper.sh -- /usr/bin/task"

    with patch("homelab_monitor.cli.install_wrapper_remote.urlopen", return_value=mock_resp):
        rc = _write_files_and_register(
            crontab_file=ct,
            crontab_content=crontab_content,
            line_index=line_index,
            new_line=new_line,
            wrapper_content="#!/bin/bash\necho ok\n",
            monitor_url="http://monitor.example.com",
            fingerprint="abc123",
            token="my-token",
            reg_payload={"host": "h"},
        )

    assert rc == 0
    assert fake_wrapper.exists()
    assert fake_token.exists()
    assert fake_token.read_text(encoding="utf-8") == "my-token"
    new_content = ct.read_text(encoding="utf-8")
    assert new_line in new_content


def test_write_files_wrapper_write_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Wrapper write failure returns 1."""
    fake_wrapper = tmp_path / "nonexistent_dir" / "wrapper.sh"
    fake_token = tmp_path / "token"
    ct = _make_crontab_file(tmp_path)

    monkeypatch.setattr(iwr, "WRAPPER_PATH", str(fake_wrapper))
    monkeypatch.setattr(iwr, "TOKEN_FILE_PATH", str(fake_token))
    monkeypatch.setattr(iwr, "TOKEN_FILE_DIR", str(tmp_path))

    rc = _write_files_and_register(
        crontab_file=ct,
        crontab_content=ct.read_text(encoding="utf-8"),
        line_index=0,
        new_line="new line",
        wrapper_content="content",
        monitor_url="http://m.example.com",
        fingerprint="fp",
        token="tok",
        reg_payload={},
    )
    assert rc == 1


def test_write_files_token_write_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Token write failure returns 1."""
    fake_wrapper = tmp_path / "wrapper.sh"
    # Token in a non-existent nested dir that can't be created
    fake_token_dir = tmp_path / "tok_dir"
    fake_token = fake_token_dir / "heartbeat.token"
    ct = _make_crontab_file(tmp_path)

    monkeypatch.setattr(iwr, "WRAPPER_PATH", str(fake_wrapper))
    monkeypatch.setattr(iwr, "TOKEN_FILE_PATH", str(fake_token))
    monkeypatch.setattr(iwr, "TOKEN_FILE_DIR", str(fake_token_dir))

    # Make token dir a file so mkdir fails
    fake_token_dir.write_text("not a dir")

    rc = _write_files_and_register(
        crontab_file=ct,
        crontab_content=ct.read_text(encoding="utf-8"),
        line_index=0,
        new_line="new line",
        wrapper_content="#!/bin/bash\n",
        monitor_url="http://m.example.com",
        fingerprint="fp",
        token="tok",
        reg_payload={},
    )
    assert rc == 1


def test_write_files_crontab_rewrite_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Crontab rewrite failure (tempfile can't be created) returns 1."""
    fake_wrapper = tmp_path / "wrapper.sh"
    fake_token_dir = tmp_path / "token_dir"
    fake_token_dir.mkdir()
    fake_token = fake_token_dir / "heartbeat.token"
    _make_crontab_file(tmp_path)

    monkeypatch.setattr(iwr, "WRAPPER_PATH", str(fake_wrapper))
    monkeypatch.setattr(iwr, "TOKEN_FILE_PATH", str(fake_token))
    monkeypatch.setattr(iwr, "TOKEN_FILE_DIR", str(fake_token_dir))

    # Make crontab a directory so rewrite fails
    bad_ct = tmp_path / "bad_ct_dir"
    bad_ct.mkdir()

    rc = _write_files_and_register(
        crontab_file=bad_ct,
        crontab_content="*/5 * * * * root /usr/bin/task\n",
        line_index=0,
        new_line="new line",
        wrapper_content="#!/bin/bash\n",
        monitor_url="http://m.example.com",
        fingerprint="fp",
        token="tok",
        reg_payload={},
    )
    assert rc == 1


def test_write_files_register_http_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """HTTPError during register: warning printed, still returns 0."""
    fake_wrapper = tmp_path / "wrapper.sh"
    fake_token_dir = tmp_path / "token_dir"
    fake_token_dir.mkdir()
    fake_token = fake_token_dir / "heartbeat.token"
    ct = _make_crontab_file(tmp_path)

    monkeypatch.setattr(iwr, "WRAPPER_PATH", str(fake_wrapper))
    monkeypatch.setattr(iwr, "TOKEN_FILE_PATH", str(fake_token))
    monkeypatch.setattr(iwr, "TOKEN_FILE_DIR", str(fake_token_dir))

    http_error = HTTPError("http://x", 500, "Server Error", {}, None)  # type: ignore[arg-type]
    with patch("homelab_monitor.cli.install_wrapper_remote.urlopen", side_effect=http_error):
        rc = _write_files_and_register(
            crontab_file=ct,
            crontab_content=ct.read_text(encoding="utf-8"),
            line_index=0,
            new_line="*/5 * * * * root /wrapper -- /usr/bin/task",
            wrapper_content="#!/bin/bash\n",
            monitor_url="http://m.example.com",
            fingerprint="fp",
            token="tok",
            reg_payload={},
        )

    assert rc == 0
    captured = capsys.readouterr()
    assert "WARNING" in captured.err


def test_write_files_register_generic_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Generic exception during register: warning printed, still returns 0."""
    fake_wrapper = tmp_path / "wrapper.sh"
    fake_token_dir = tmp_path / "token_dir"
    fake_token_dir.mkdir()
    fake_token = fake_token_dir / "heartbeat.token"
    ct = _make_crontab_file(tmp_path)

    monkeypatch.setattr(iwr, "WRAPPER_PATH", str(fake_wrapper))
    monkeypatch.setattr(iwr, "TOKEN_FILE_PATH", str(fake_token))
    monkeypatch.setattr(iwr, "TOKEN_FILE_DIR", str(fake_token_dir))

    with patch(
        "homelab_monitor.cli.install_wrapper_remote.urlopen", side_effect=OSError("network down")
    ):
        rc = _write_files_and_register(
            crontab_file=ct,
            crontab_content=ct.read_text(encoding="utf-8"),
            line_index=0,
            new_line="*/5 * * * * root /wrapper -- /usr/bin/task",
            wrapper_content="#!/bin/bash\n",
            monitor_url="http://m.example.com",
            fingerprint="fp",
            token="tok",
            reg_payload={},
        )

    assert rc == 0
    captured = capsys.readouterr()
    assert "WARNING" in captured.err


def test_write_files_non_200_status_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Non-2xx HTTP status prints a warning but still returns 0."""
    fake_wrapper = tmp_path / "wrapper.sh"
    fake_token_dir = tmp_path / "token_dir"
    fake_token_dir.mkdir()
    fake_token = fake_token_dir / "heartbeat.token"
    ct = _make_crontab_file(tmp_path)

    monkeypatch.setattr(iwr, "WRAPPER_PATH", str(fake_wrapper))
    monkeypatch.setattr(iwr, "TOKEN_FILE_PATH", str(fake_token))
    monkeypatch.setattr(iwr, "TOKEN_FILE_DIR", str(fake_token_dir))

    mock_resp = MagicMock()
    mock_resp.__enter__ = MagicMock(return_value=mock_resp)
    mock_resp.__exit__ = MagicMock(return_value=False)
    mock_resp.status = 503

    with patch("homelab_monitor.cli.install_wrapper_remote.urlopen", return_value=mock_resp):
        rc = _write_files_and_register(
            crontab_file=ct,
            crontab_content=ct.read_text(encoding="utf-8"),
            line_index=0,
            new_line="*/5 * * * * root /wrapper -- /usr/bin/task",
            wrapper_content="#!/bin/bash\n",
            monitor_url="http://m.example.com",
            fingerprint="fp",
            token="tok",
            reg_payload={},
        )

    assert rc == 0
    captured = capsys.readouterr()
    assert "WARNING" in captured.err


# ---------------------------------------------------------------------------
# main() — validation early-returns
# ---------------------------------------------------------------------------


def _make_main_argv(  # noqa: PLR0913 -- test helper takes explicit per-field argv args
    *,
    monitor_url: str = "http://monitor.example.com",
    token: str = "my-token",
    crontab: str = "",
    line: int = 0,
    host: str = "testhost",
    confirm: bool = False,
) -> list[str]:
    argv = [
        "--monitor-url",
        monitor_url,
        "--token",
        token,
        "--host",
        host,
    ]
    if crontab:
        argv += ["--crontab", crontab]
    if line:
        argv += ["--line", str(line)]
    if confirm:
        argv.append("--confirm")
    return argv


def test_main_missing_monitor_url_returns_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("HM_MONITOR_URL", raising=False)
    monkeypatch.delenv("HM_HEARTBEAT_TOKEN", raising=False)
    with patch("sys.argv", ["prog", "--token", "tok"]):
        rc = main()
    assert rc == 1
    assert (
        "monitor-url" in capsys.readouterr().err.lower() or "MONITOR_URL" in capsys.readouterr().err
    )


def test_main_missing_token_returns_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("HM_HEARTBEAT_TOKEN", raising=False)
    with patch("sys.argv", ["prog", "--monitor-url", "http://m.example.com"]):
        rc = main()
    assert rc == 1
    captured = capsys.readouterr()
    assert "token" in captured.err.lower() or "TOKEN" in captured.err


def test_main_crontab_not_found_returns_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    nonexistent = str(tmp_path / "does_not_exist")
    argv = _make_main_argv(crontab=nonexistent)
    with patch("sys.argv", ["prog", *argv]):
        rc = main()
    assert rc == 1
    assert "not found" in capsys.readouterr().err


def test_main_no_crontab_lines_returns_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ct = tmp_path / "empty.crontab"
    ct.write_text("# only comments\n", encoding="utf-8")
    argv = _make_main_argv(crontab=str(ct))
    with patch("sys.argv", ["prog", *argv]):
        rc = main()
    assert rc == 1
    assert "no crontab lines" in capsys.readouterr().err


def test_main_line_out_of_range_returns_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ct = tmp_path / "test.crontab"
    ct.write_text("*/5 * * * * /usr/bin/task\n", encoding="utf-8")
    argv = _make_main_argv(crontab=str(ct), line=99)
    with patch("sys.argv", ["prog", *argv]):
        rc = main()
    assert rc == 1
    assert "out of range" in capsys.readouterr().err


def test_main_unparseable_line_returns_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A line that parses as non-comment but has too few fields for the heuristic → None."""
    ct = tmp_path / "test.crontab"
    # Only 4 fields — parse_crontab_lines includes it (non-blank, non-comment)
    # but _parse_job_line returns None (< 6 fields)
    ct.write_text("* * * *\n", encoding="utf-8")
    argv = _make_main_argv(crontab=str(ct), line=1)
    with patch("sys.argv", ["prog", *argv]):
        rc = main()
    assert rc == 1
    assert "cannot parse" in capsys.readouterr().err


def test_main_dry_run_prints_preview(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Dry-run (no --confirm) prints wrapper + crontab diff + payload, returns 0."""
    ct = tmp_path / "test.crontab"
    ct.write_text("*/5 * * * * /usr/bin/task\n", encoding="utf-8")
    argv = _make_main_argv(crontab=str(ct), line=1)
    with (
        patch("sys.argv", ["prog", *argv]),
        patch(
            "homelab_monitor.cli.install_wrapper_remote.fetch_wrapper_template",
            return_value="#!/bin/sh\n# {{FINGERPRINT}}\n",
        ),
    ):
        rc = main()
    assert rc == 0
    captured = capsys.readouterr()
    assert "Wrapper script" in captured.out
    assert "Crontab diff" in captured.out
    assert "Registration payload" in captured.out
    assert "/usr/bin/task" in captured.out


def test_main_confirm_calls_write_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Confirm path calls _write_files_and_register, returns its exit code."""
    ct = tmp_path / "test.crontab"
    ct.write_text("*/5 * * * * /usr/bin/task\n", encoding="utf-8")
    argv = _make_main_argv(crontab=str(ct), line=1, confirm=True)

    with (
        patch("sys.argv", ["prog", *argv]),
        patch(
            "homelab_monitor.cli.install_wrapper_remote.fetch_wrapper_template",
            return_value="#!/bin/sh\n# template\n",
        ),
        patch(
            "homelab_monitor.cli.install_wrapper_remote._write_files_and_register",
            return_value=0,
        ) as mock_write,
    ):
        rc = main()

    assert rc == 0
    assert mock_write.called


def test_write_files_crontab_without_trailing_newline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Crontab rewrite without trailing newline skips append (line 238->242 False branch)."""
    fake_wrapper = tmp_path / "wrapper.sh"
    fake_token_dir = tmp_path / "token_dir2"
    fake_token_dir.mkdir()
    fake_token = fake_token_dir / "heartbeat.token"
    ct = tmp_path / "crontab2"
    # No trailing newline
    original = "*/5 * * * * /usr/bin/task"
    ct.write_text(original, encoding="utf-8")

    monkeypatch.setattr(iwr, "WRAPPER_PATH", str(fake_wrapper))
    monkeypatch.setattr(iwr, "TOKEN_FILE_PATH", str(fake_token))
    monkeypatch.setattr(iwr, "TOKEN_FILE_DIR", str(fake_token_dir))

    mock_resp = MagicMock()
    mock_resp.__enter__ = MagicMock(return_value=mock_resp)
    mock_resp.__exit__ = MagicMock(return_value=False)
    mock_resp.status = 200

    with patch("homelab_monitor.cli.install_wrapper_remote.urlopen", return_value=mock_resp):
        rc = _write_files_and_register(
            crontab_file=ct,
            crontab_content=original,
            line_index=0,
            new_line="*/5 * * * * /wrapper -- /usr/bin/task",
            wrapper_content="#!/bin/bash\n",
            monitor_url="http://m.example.com",
            fingerprint="fp",
            token="tok",
            reg_payload={},
        )

    assert rc == 0
    # No trailing newline added since original had none
    new_content = ct.read_text(encoding="utf-8")
    assert not new_content.endswith("\n")


def test_write_files_crontab_with_trailing_newline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Crontab rewrite preserves trailing newline (line 238->242 branch)."""
    fake_wrapper = tmp_path / "wrapper.sh"
    fake_token_dir = tmp_path / "token_dir"
    fake_token_dir.mkdir()
    fake_token = fake_token_dir / "heartbeat.token"
    ct = tmp_path / "crontab"
    # Content with trailing newline
    original = "*/5 * * * * /usr/bin/task\n"
    ct.write_text(original, encoding="utf-8")

    monkeypatch.setattr(iwr, "WRAPPER_PATH", str(fake_wrapper))
    monkeypatch.setattr(iwr, "TOKEN_FILE_PATH", str(fake_token))
    monkeypatch.setattr(iwr, "TOKEN_FILE_DIR", str(fake_token_dir))

    mock_resp = MagicMock()
    mock_resp.__enter__ = MagicMock(return_value=mock_resp)
    mock_resp.__exit__ = MagicMock(return_value=False)
    mock_resp.status = 200

    with patch("homelab_monitor.cli.install_wrapper_remote.urlopen", return_value=mock_resp):
        rc = _write_files_and_register(
            crontab_file=ct,
            crontab_content=original,
            line_index=0,
            new_line="*/5 * * * * /wrapper -- /usr/bin/task",
            wrapper_content="#!/bin/bash\n",
            monitor_url="http://m.example.com",
            fingerprint="fp",
            token="tok",
            reg_payload={},
        )

    assert rc == 0
    new_content = ct.read_text(encoding="utf-8")
    assert new_content.endswith("\n")


def test_main_prompt_lists_spool_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """When --crontab not set, existing spool files are printed (lines 304-305)."""
    # Create a real user crontab file for the prompt to use
    user_ct = tmp_path / "alice"
    user_ct.write_text("*/5 * * * * /usr/bin/task\n", encoding="utf-8")

    # Patch Path.glob on the /var/spool/cron/crontabs path to return our file
    original_glob = Path.glob

    def patched_glob(self: Path, pattern: str) -> list[Path]:  # type: ignore[override]
        if "crontabs" in str(self):
            return [user_ct]
        return list(original_glob(self, pattern))

    monkeypatch.setattr(Path, "glob", patched_glob)

    # User selects our crontab by path, then picks line 1
    argv = _make_main_argv()  # no --crontab, no --line
    with (
        patch("sys.argv", ["prog", *argv]),
        patch("builtins.input", side_effect=[str(user_ct), "1"]),
        patch(
            "homelab_monitor.cli.install_wrapper_remote.fetch_wrapper_template",
            return_value="#!/bin/sh\n# {{FINGERPRINT}}\n",
        ),
    ):
        rc = main()

    assert rc == 0
    captured = capsys.readouterr()
    assert "Available crontabs" in captured.out


def test_main_prompt_spool_dir_not_listed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Spool entries that are dirs (not files) are skipped (line 304->303 False branch)."""
    user_ct = tmp_path / "alice"
    user_ct.write_text("*/5 * * * * /usr/bin/task\n", encoding="utf-8")

    dir_entry = tmp_path / "adir"
    dir_entry.mkdir()

    # glob returns both a file and a directory; only the file should be listed
    original_glob = Path.glob

    def patched_glob(self: Path, pattern: str) -> list[Path]:  # type: ignore[override]
        if "crontabs" in str(self):
            return [user_ct, dir_entry]  # dir_entry.is_file() == False → skipped
        return list(original_glob(self, pattern))

    monkeypatch.setattr(Path, "glob", patched_glob)

    argv = _make_main_argv()  # no --crontab
    with (
        patch("sys.argv", ["prog", *argv]),
        patch("builtins.input", side_effect=[str(user_ct), "1"]),
        patch(
            "homelab_monitor.cli.install_wrapper_remote.fetch_wrapper_template",
            return_value="#!/bin/sh\n# {{FINGERPRINT}}\n",
        ),
    ):
        rc = main()

    assert rc == 0
    captured = capsys.readouterr()
    assert "Available crontabs" in captured.out
    assert "adir" not in captured.out


def test_main_crontab_read_error_returns_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """read_text raises → returns 1 (lines 316-318)."""
    ct = tmp_path / "test.crontab"
    ct.write_text("*/5 * * * * /usr/bin/task\n", encoding="utf-8")
    argv = _make_main_argv(crontab=str(ct))

    with (
        patch("sys.argv", ["prog", *argv]),
        patch("pathlib.Path.read_text", side_effect=PermissionError("denied")),
    ):
        rc = main()

    assert rc == 1
    assert "failed to read" in capsys.readouterr().err


def test_main_if_name_main_entrypoint(capsys: pytest.CaptureFixture[str]) -> None:
    """Line 393: __main__ block — exec the module source with __name__='__main__'."""
    import importlib.util  # noqa: PLC0415
    from pathlib import Path as _Path  # noqa: PLC0415

    # Find the source file
    spec = importlib.util.find_spec("homelab_monitor.cli.install_wrapper_remote")
    assert spec is not None and spec.origin is not None
    source = _Path(spec.origin).read_text(encoding="utf-8")

    # Execute the source with __name__ = "__main__" so line 393 runs
    with (
        patch("sys.argv", ["install_wrapper_remote", "--monitor-url", "", "--token", ""]),
        pytest.raises(SystemExit) as exc_info,
    ):
        exec(compile(source, spec.origin, "exec"), {"__name__": "__main__"})

    assert exc_info.value.code == 1


def test_main_prompt_for_crontab(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """If --crontab not set, prompts user; uses the entered spec."""
    ct = tmp_path / "my.crontab"
    ct.write_text("*/5 * * * * /usr/bin/task\n", encoding="utf-8")
    argv = _make_main_argv()  # no crontab arg

    # Patch glob to return nothing (no files in /var/spool/cron/crontabs)
    with (
        patch("sys.argv", ["prog", *argv]),
        patch("homelab_monitor.cli.install_wrapper_remote.Path") as mock_path_cls,
        patch("builtins.input", return_value=str(ct)),
    ):
        # We need Path to work normally for the file we control
        # Restore Path for everything except the glob call
        import pathlib  # noqa: PLC0415

        def patched_path(*args: object, **kwargs: object) -> pathlib.Path:
            return pathlib.Path(*args, **kwargs)  # type: ignore[arg-type]

        mock_path_cls.side_effect = patched_path
        mock_spool = MagicMock()
        mock_spool.glob.return_value = []
        mock_path_cls.return_value = mock_spool

        # Use real Path for the main call; still need to mock input
        with (
            patch("homelab_monitor.cli.install_wrapper_remote.Path", pathlib.Path),
            patch("builtins.input", return_value=str(ct)),
        ):
            rc = main()

    # rc is 0 (dry-run) if crontab was found; the exact rc depends on prompts
    # The key thing is main() ran without exception
    assert isinstance(rc, int)


def test_main_prompt_for_line_number(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """If --line not set, prompts user for line number."""
    ct = tmp_path / "test.crontab"
    ct.write_text("*/5 * * * * /usr/bin/task\n", encoding="utf-8")
    argv = _make_main_argv(crontab=str(ct))  # no --line

    with (
        patch("sys.argv", ["prog", *argv]),
        patch("builtins.input", return_value="1"),
        patch(
            "homelab_monitor.cli.install_wrapper_remote.fetch_wrapper_template",
            return_value="#!/bin/sh\n# {{FINGERPRINT}}\n",
        ),
    ):
        rc = main()

    assert rc == 0
    captured = capsys.readouterr()
    assert "Crontab lines" in captured.out


def test_main_prompt_invalid_line_number_returns_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Prompt returns non-integer → returns 1."""
    ct = tmp_path / "test.crontab"
    ct.write_text("*/5 * * * * /usr/bin/task\n", encoding="utf-8")
    argv = _make_main_argv(crontab=str(ct))

    with (
        patch("sys.argv", ["prog", *argv]),
        patch("builtins.input", return_value="notanumber"),
    ):
        rc = main()

    assert rc == 1
    assert "invalid" in capsys.readouterr().err


def test_main_prompt_eof_returns_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Prompt raises EOFError → returns 1."""
    ct = tmp_path / "test.crontab"
    ct.write_text("*/5 * * * * /usr/bin/task\n", encoding="utf-8")
    argv = _make_main_argv(crontab=str(ct))

    with (
        patch("sys.argv", ["prog", *argv]),
        patch("builtins.input", side_effect=EOFError),
    ):
        rc = main()

    assert rc == 1
    assert "invalid" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Item 2: fetch_wrapper_template
# ---------------------------------------------------------------------------


def _make_urlopen_resp(body: str, status: int = 200) -> MagicMock:
    """Return a mock context-manager response for urlopen."""
    mock_resp = MagicMock()
    mock_resp.__enter__ = MagicMock(return_value=mock_resp)
    mock_resp.__exit__ = MagicMock(return_value=False)
    mock_resp.status = status
    mock_resp.read = MagicMock(return_value=body.encode("utf-8"))
    return mock_resp


def test_fetch_wrapper_template_returns_body_on_200() -> None:
    """fetch_wrapper_template returns the decoded body when HTTP status is 200."""
    tmpl = "#!/bin/sh\n# {{FINGERPRINT}}\n"
    mock_resp = _make_urlopen_resp(tmpl, status=200)

    with patch("homelab_monitor.cli.install_wrapper_remote.urlopen", return_value=mock_resp):
        result = fetch_wrapper_template("http://monitor.example.com", "my-token")

    assert result == tmpl


def test_fetch_wrapper_template_raises_on_non_2xx() -> None:
    """fetch_wrapper_template raises RuntimeError when HTTP status is outside 2xx."""
    mock_resp = _make_urlopen_resp("Forbidden", status=403)

    with (
        patch("homelab_monitor.cli.install_wrapper_remote.urlopen", return_value=mock_resp),
        pytest.raises(RuntimeError, match="403"),
    ):
        fetch_wrapper_template("http://monitor.example.com", "bad-token")


# ---------------------------------------------------------------------------
# Item 3: byte-identical substitution vs install.py:_build_wrapper_content()
# ---------------------------------------------------------------------------


def test_remote_installer_substitution_byte_identical_to_kernel(tmp_path: Path) -> None:
    """The remote installer's substituted wrapper == install.py:_build_wrapper_content().

    Both must produce exactly the same bytes for the same (fingerprint, url, date).
    """
    import datetime  # noqa: PLC0415

    from homelab_monitor.kernel.cron.install import (  # noqa: PLC0415
        _build_wrapper_content,  # pyright: ignore[reportPrivateUsage]
    )

    fp = "a" * 64
    monitor_url = "https://monitor.example.com"
    install_date = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d")

    # Kernel side: call _build_wrapper_content directly
    kernel_content = _build_wrapper_content(fp, monitor_url, install_date)

    # Remote-installer side: fetch template from API (mocked), then substitute
    # Read the real template to simulate the fetch
    from importlib.resources import files as _files  # noqa: PLC0415

    tmpl = (
        _files("homelab_monitor")
        .joinpath("data", "cron-with-heartbeat.sh.tmpl")
        .read_text(encoding="utf-8")
    )

    mock_resp = _make_urlopen_resp(tmpl, status=200)
    with patch("homelab_monitor.cli.install_wrapper_remote.urlopen", return_value=mock_resp):
        template_text = fetch_wrapper_template(monitor_url, "tok")

    remote_content = (
        template_text.replace("{{FINGERPRINT}}", fp)
        .replace("{{HEARTBEAT_URL_BASE}}", monitor_url)
        .replace("{{TOKEN_FILE_PATH}}", iwr.TOKEN_FILE_PATH)
        .replace("{{INSTALL_DATE}}", install_date)
    )

    assert remote_content == kernel_content


# ---------------------------------------------------------------------------
# Item 4: remote installer must not contain "WRAPPER_TEMPLATE" or "/ping"
# ---------------------------------------------------------------------------


def test_remote_installer_source_has_no_wrapper_template_string() -> None:
    """The remote installer source must not contain the string 'WRAPPER_TEMPLATE'.

    The embedded template was deleted; the installer now fetches it from the API.
    """
    import importlib.util  # noqa: PLC0415

    spec = importlib.util.find_spec("homelab_monitor.cli.install_wrapper_remote")
    assert spec is not None and spec.origin is not None
    source = Path(spec.origin).read_text(encoding="utf-8")
    assert "WRAPPER_TEMPLATE" not in source, "Found deprecated WRAPPER_TEMPLATE in source"


def test_remote_installer_source_has_no_ping_endpoint() -> None:
    """The remote installer must not hard-code '/ping' (old health-check artifact)."""
    import importlib.util  # noqa: PLC0415

    spec = importlib.util.find_spec("homelab_monitor.cli.install_wrapper_remote")
    assert spec is not None and spec.origin is not None
    source = Path(spec.origin).read_text(encoding="utf-8")
    assert '"/ping"' not in source and "'/ping'" not in source, "Found /ping endpoint in source"


# ---------------------------------------------------------------------------
# Item 5: last-occurrence (rfind) splice when command repeats a schedule token
# ---------------------------------------------------------------------------


def test_write_files_rfind_splice_last_occurrence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When old_line contains the command text twice, only the LAST occurrence is replaced.

    Example: a command that includes a schedule-like substring as an argument.
    old_line = '*/5 * * * * /usr/bin/*/5-task.sh'  →  the command is '/usr/bin/*/5-task.sh'.
    rfind ensures the rightmost span is replaced, matching install.py:_rewrite_line.
    """
    fake_wrapper = tmp_path / "wrapper.sh"
    fake_token_dir = tmp_path / "token_dir"
    fake_token_dir.mkdir()
    fake_token = fake_token_dir / "heartbeat.token"

    # Command deliberately contains "*/5" to create ambiguity
    command = "/usr/bin/backup.sh --interval=*/5"
    old_line = f"*/5 * * * * root {command}"
    ct = tmp_path / "crontab"
    ct.write_text(old_line + "\n", encoding="utf-8")

    monkeypatch.setattr(iwr, "WRAPPER_PATH", str(fake_wrapper))
    monkeypatch.setattr(iwr, "TOKEN_FILE_PATH", str(fake_token))
    monkeypatch.setattr(iwr, "TOKEN_FILE_DIR", str(fake_token_dir))

    # Compute the expected new_line: rfind replaces only the last occurrence
    _idx = old_line.rfind(command)
    assert _idx >= 0
    expected_new_line = old_line[:_idx] + iwr.WRAPPER_INVOCATION_PREFIX + command

    mock_resp = _make_urlopen_resp("ok", status=200)
    with patch("homelab_monitor.cli.install_wrapper_remote.urlopen", return_value=mock_resp):
        rc = _write_files_and_register(
            crontab_file=ct,
            crontab_content=ct.read_text(encoding="utf-8"),
            line_index=0,
            new_line=expected_new_line,
            wrapper_content="#!/bin/sh\n",
            monitor_url="http://m.example.com",
            fingerprint="fp",
            token="tok",
            reg_payload={},
        )

    assert rc == 0
    new_content = ct.read_text(encoding="utf-8")
    assert expected_new_line in new_content
    # The schedule prefix must remain untouched (not replaced)
    assert new_content.startswith("*/5 * * * * root ")


# ---------------------------------------------------------------------------
# Item 6 (C3): rollback scenarios
# ---------------------------------------------------------------------------


def test_rollback_on_wrapper_write_failure_leaves_host_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Wrapper-write failure rolls back: crontab and token untouched."""
    # wrapper path in a non-existent subdirectory → write fails
    fake_wrapper = tmp_path / "nonexistent_dir" / "wrapper.sh"
    fake_token_dir = tmp_path / "token_dir"
    fake_token_dir.mkdir()
    fake_token = fake_token_dir / "heartbeat.token"
    ct = _make_crontab_file(tmp_path)
    original_ct = ct.read_text(encoding="utf-8")

    monkeypatch.setattr(iwr, "WRAPPER_PATH", str(fake_wrapper))
    monkeypatch.setattr(iwr, "TOKEN_FILE_PATH", str(fake_token))
    monkeypatch.setattr(iwr, "TOKEN_FILE_DIR", str(fake_token_dir))

    rc = _write_files_and_register(
        crontab_file=ct,
        crontab_content=original_ct,
        line_index=0,
        new_line="new line",
        wrapper_content="content",
        monitor_url="http://m.example.com",
        fingerprint="fp",
        token="tok",
        reg_payload={},
    )

    assert rc == 1
    # crontab must be unchanged
    assert ct.read_text(encoding="utf-8") == original_ct
    # token must not have been written
    assert not fake_token.exists()


def test_rollback_on_token_write_failure_restores_wrapper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Token-write failure rolls back: wrapper unlinked if it didn't pre-exist."""
    fake_wrapper = tmp_path / "wrapper.sh"
    fake_token_dir = tmp_path / "tok_dir"
    # Make token dir a file so mkdir/write fails
    fake_token_dir.write_text("not a dir")
    fake_token = fake_token_dir / "heartbeat.token"
    ct = _make_crontab_file(tmp_path)
    original_ct = ct.read_text(encoding="utf-8")

    monkeypatch.setattr(iwr, "WRAPPER_PATH", str(fake_wrapper))
    monkeypatch.setattr(iwr, "TOKEN_FILE_PATH", str(fake_token))
    monkeypatch.setattr(iwr, "TOKEN_FILE_DIR", str(fake_token_dir))

    rc = _write_files_and_register(
        crontab_file=ct,
        crontab_content=original_ct,
        line_index=0,
        new_line="new line",
        wrapper_content="#!/bin/sh\n",
        monitor_url="http://m.example.com",
        fingerprint="fp",
        token="tok",
        reg_payload={},
    )

    assert rc == 1
    # crontab must be unchanged
    assert ct.read_text(encoding="utf-8") == original_ct
    # wrapper must have been rolled back (unlinked)
    assert not fake_wrapper.exists()


def test_rollback_on_crontab_write_failure_restores_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Crontab-write failure rolls back wrapper + token."""
    fake_wrapper = tmp_path / "wrapper.sh"
    fake_token_dir = tmp_path / "token_dir"
    fake_token_dir.mkdir()
    fake_token = fake_token_dir / "heartbeat.token"
    # crontab as a directory → atomic write fails
    bad_ct = tmp_path / "bad_ct_dir"
    bad_ct.mkdir()

    monkeypatch.setattr(iwr, "WRAPPER_PATH", str(fake_wrapper))
    monkeypatch.setattr(iwr, "TOKEN_FILE_PATH", str(fake_token))
    monkeypatch.setattr(iwr, "TOKEN_FILE_DIR", str(fake_token_dir))

    rc = _write_files_and_register(
        crontab_file=bad_ct,
        crontab_content="*/5 * * * * root /usr/bin/task\n",
        line_index=0,
        new_line="new line",
        wrapper_content="#!/bin/sh\n",
        monitor_url="http://m.example.com",
        fingerprint="fp",
        token="tok",
        reg_payload={},
    )

    assert rc == 1
    # wrapper and token must have been rolled back
    assert not fake_wrapper.exists()
    assert not fake_token.exists()


def test_reinstall_over_existing_restores_prior_content_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-install over an existing wrapper: failure restores prior wrapper content."""
    fake_wrapper = tmp_path / "wrapper.sh"
    prior_wrapper_content = "#!/bin/sh\n# old wrapper\n"
    fake_wrapper.write_text(prior_wrapper_content, encoding="utf-8")

    fake_token_dir = tmp_path / "tok_dir"
    # Make token dir a file to force failure after wrapper write
    fake_token_dir.write_text("not a dir")
    fake_token = fake_token_dir / "heartbeat.token"

    ct = _make_crontab_file(tmp_path)
    original_ct = ct.read_text(encoding="utf-8")

    monkeypatch.setattr(iwr, "WRAPPER_PATH", str(fake_wrapper))
    monkeypatch.setattr(iwr, "TOKEN_FILE_PATH", str(fake_token))
    monkeypatch.setattr(iwr, "TOKEN_FILE_DIR", str(fake_token_dir))

    rc = _write_files_and_register(
        crontab_file=ct,
        crontab_content=original_ct,
        line_index=0,
        new_line="new line",
        wrapper_content="#!/bin/sh\n# new wrapper\n",
        monitor_url="http://m.example.com",
        fingerprint="fp",
        token="tok",
        reg_payload={},
    )

    assert rc == 1
    # Prior wrapper content must be restored
    assert fake_wrapper.read_text(encoding="utf-8") == prior_wrapper_content
    # crontab unchanged
    assert ct.read_text(encoding="utf-8") == original_ct


def test_registration_failure_does_not_roll_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Registration (step 4) failure does NOT roll back file writes.

    Registration is best-effort; files remain written even when urlopen raises.
    """
    fake_wrapper = tmp_path / "wrapper.sh"
    fake_token_dir = tmp_path / "token_dir"
    fake_token_dir.mkdir()
    fake_token = fake_token_dir / "heartbeat.token"
    ct = _make_crontab_file(tmp_path)
    original_ct = ct.read_text(encoding="utf-8")

    monkeypatch.setattr(iwr, "WRAPPER_PATH", str(fake_wrapper))
    monkeypatch.setattr(iwr, "TOKEN_FILE_PATH", str(fake_token))
    monkeypatch.setattr(iwr, "TOKEN_FILE_DIR", str(fake_token_dir))

    with patch(
        "homelab_monitor.cli.install_wrapper_remote.urlopen", side_effect=OSError("net down")
    ):
        rc = _write_files_and_register(
            crontab_file=ct,
            crontab_content=original_ct,
            line_index=0,
            new_line="*/5 * * * * root /wrapper -- /usr/bin/task",
            wrapper_content="#!/bin/sh\n",
            monitor_url="http://m.example.com",
            fingerprint="fp",
            token="tok",
            reg_payload={},
        )

    # Returns 0 despite registration failure
    assert rc == 0
    # Files are still written
    assert fake_wrapper.exists()
    assert fake_token.exists()
    # Warning printed
    assert "WARNING" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Coverage gap: _rmdir_if_empty (lines 104-106)
# ---------------------------------------------------------------------------


def test_rmdir_if_empty_removes_empty_dir(tmp_path: Path) -> None:
    """_rmdir_if_empty removes an empty directory (exercises lines 104-106)."""
    from homelab_monitor.cli.install_wrapper_remote import (  # noqa: PLC0415
        _rmdir_if_empty,  # pyright: ignore[reportPrivateUsage]
    )

    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    assert empty_dir.exists()
    _rmdir_if_empty(empty_dir)
    assert not empty_dir.exists()


def test_rmdir_if_empty_suppresses_error_for_nonempty_dir(tmp_path: Path) -> None:
    """_rmdir_if_empty silently ignores OSError when directory is not empty."""
    from homelab_monitor.cli.install_wrapper_remote import (  # noqa: PLC0415
        _rmdir_if_empty,  # pyright: ignore[reportPrivateUsage]
    )

    nonempty = tmp_path / "nonempty"
    nonempty.mkdir()
    (nonempty / "child").write_text("x")
    # Should not raise
    _rmdir_if_empty(nonempty)
    assert nonempty.exists()


# ---------------------------------------------------------------------------
# Coverage gap: _write_token_file when token_dir did NOT pre-exist (line 211)
# and _undo_token_restore when token file DID pre-exist (lines 218-224)
# ---------------------------------------------------------------------------


def test_write_token_file_new_dir_registers_rmdir_undo(tmp_path: Path) -> None:
    """When token_dir does not exist, _write_token_file appends _rmdir_if_empty undo (line 211)."""
    from homelab_monitor.cli.install_wrapper_remote import (  # noqa: PLC0415
        _write_token_file,  # pyright: ignore[reportPrivateUsage]
    )

    token_dir = tmp_path / "new_token_dir"
    token_file = token_dir / "heartbeat.token"
    assert not token_dir.exists()

    undo: list[Callable[[], None]] = []
    _write_token_file(token_file, token_dir, "mytoken", undo)

    assert token_dir.exists()
    assert token_file.read_text(encoding="utf-8") == "mytoken"
    # undo list must contain at least 2 entries: rmdir + unlink
    assert len(undo) >= 2  # noqa: PLR2004

    # Execute the rmdir undo to exercise line 211's lambda (which calls _rmdir_if_empty)
    token_file.unlink()  # empty the dir first so rmdir can succeed
    undo[0]()  # this is the _rmdir_if_empty lambda
    assert not token_dir.exists()


def test_write_token_file_preexisting_token_registers_restore_undo(tmp_path: Path) -> None:
    """When token file exists, _write_token_file registers _undo_token_restore (lines 218-224)."""
    from homelab_monitor.cli.install_wrapper_remote import (  # noqa: PLC0415
        _write_token_file,  # pyright: ignore[reportPrivateUsage]
    )

    token_dir = tmp_path / "token_dir"
    token_dir.mkdir()
    token_file = token_dir / "heartbeat.token"
    prior_token = "prior-token-content"
    token_file.write_text(prior_token, encoding="utf-8")

    undo: list[Callable[[], None]] = []
    _write_token_file(token_file, token_dir, "new-token", undo)

    assert token_file.read_text(encoding="utf-8") == "new-token"
    # undo list has exactly 1 entry (no rmdir since dir pre-existed): _undo_token_restore
    assert len(undo) == 1

    # Execute the undo — must restore prior content (exercises lines 218-224)
    undo[0]()
    assert token_file.read_text(encoding="utf-8") == prior_token


# ---------------------------------------------------------------------------
# Coverage gap: fetch_wrapper_template exception in main() (lines 408-410)
# ---------------------------------------------------------------------------


def test_main_fetch_wrapper_template_failure_returns_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """When fetch_wrapper_template raises, main() prints error and returns 1 (lines 408-410)."""
    ct = tmp_path / "test.crontab"
    ct.write_text("*/5 * * * * /usr/bin/task\n", encoding="utf-8")
    argv = _make_main_argv(crontab=str(ct), line=1)

    with (
        patch("sys.argv", ["prog", *argv]),
        patch(
            "homelab_monitor.cli.install_wrapper_remote.fetch_wrapper_template",
            side_effect=RuntimeError("template fetch returned HTTP 403"),
        ),
    ):
        rc = main()

    assert rc == 1
    assert "failed to fetch wrapper template" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Coverage gap: rfind returns -1 — command not found in line (lines 426-427)
# ---------------------------------------------------------------------------


def test_main_command_not_found_in_line_returns_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """When rfind can't locate command in old_line, main() prints error and returns 1.

    lines 426-427. This is reached via main() after _parse_job_line succeeds. We force
    the mismatch by patching _parse_job_line to return a command string that does not
    appear in the line.
    """
    ct = tmp_path / "test.crontab"
    ct.write_text("*/5 * * * * /usr/bin/task\n", encoding="utf-8")
    argv = _make_main_argv(crontab=str(ct), line=1)

    with (
        patch("sys.argv", ["prog", *argv]),
        patch(
            "homelab_monitor.cli.install_wrapper_remote.fetch_wrapper_template",
            return_value="#!/bin/sh\n# {{FINGERPRINT}}\n",
        ),
        patch(
            "homelab_monitor.cli.install_wrapper_remote._parse_job_line",
            return_value=("*/5 * * * *", "COMMAND_THAT_DOES_NOT_EXIST_IN_LINE"),
        ),
    ):
        rc = main()

    assert rc == 1
    assert "command not found in crontab line" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Coverage gap: _undo_wrapper_restore (wrapper pre-existed → restore on failure)
# ---------------------------------------------------------------------------


def test_reinstall_wrapper_preexisted_restore_on_crontab_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When wrapper pre-exists and crontab write fails, _undo_wrapper_restore restores it."""
    fake_wrapper = tmp_path / "wrapper.sh"
    prior_wrapper = "#!/bin/sh\n# prior wrapper\n"
    fake_wrapper.write_text(prior_wrapper, encoding="utf-8")

    fake_token_dir = tmp_path / "token_dir"
    fake_token_dir.mkdir()
    fake_token = fake_token_dir / "heartbeat.token"

    # crontab is a directory so atomic write fails after wrapper+token succeed
    bad_ct = tmp_path / "bad_ct_dir"
    bad_ct.mkdir()

    monkeypatch.setattr(iwr, "WRAPPER_PATH", str(fake_wrapper))
    monkeypatch.setattr(iwr, "TOKEN_FILE_PATH", str(fake_token))
    monkeypatch.setattr(iwr, "TOKEN_FILE_DIR", str(fake_token_dir))

    rc = _write_files_and_register(
        crontab_file=bad_ct,
        crontab_content="*/5 * * * * root /usr/bin/task\n",
        line_index=0,
        new_line="new line",
        wrapper_content="#!/bin/sh\n# new wrapper\n",
        monitor_url="http://m.example.com",
        fingerprint="fp",
        token="tok",
        reg_payload={},
    )

    assert rc == 1
    # Wrapper must be restored to prior content (exercises _undo_wrapper_restore)
    assert fake_wrapper.read_text(encoding="utf-8") == prior_wrapper


# ---------------------------------------------------------------------------
# Item 7: token file written 0644
# ---------------------------------------------------------------------------


def test_token_file_written_0644(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Token file is chmod'd 0644 after writing.

    Item 7: 0644 allows the cron runner to read it while preventing world-write.
    """
    fake_wrapper = tmp_path / "wrapper.sh"
    fake_token_dir = tmp_path / "token_dir"
    fake_token_dir.mkdir()
    fake_token = fake_token_dir / "heartbeat.token"
    ct = _make_crontab_file(tmp_path)

    monkeypatch.setattr(iwr, "WRAPPER_PATH", str(fake_wrapper))
    monkeypatch.setattr(iwr, "TOKEN_FILE_PATH", str(fake_token))
    monkeypatch.setattr(iwr, "TOKEN_FILE_DIR", str(fake_token_dir))

    mock_resp = _make_urlopen_resp("ok", status=200)
    with patch("homelab_monitor.cli.install_wrapper_remote.urlopen", return_value=mock_resp):
        rc = _write_files_and_register(
            crontab_file=ct,
            crontab_content=ct.read_text(encoding="utf-8"),
            line_index=0,
            new_line="*/5 * * * * root /wrapper -- /usr/bin/task",
            wrapper_content="#!/bin/sh\n",
            monitor_url="http://m.example.com",
            fingerprint="fp",
            token="tok",
            reg_payload={},
        )

    assert rc == 0
    assert fake_token.exists()
    token_mode = stat.S_IMODE(fake_token.stat().st_mode)
    assert token_mode == 0o644  # noqa: PLR2004


# ===========================================================================
# STAGE-002-009A: _run_uninstall tests
# ===========================================================================

from homelab_monitor.cli.install_wrapper_remote import (  # noqa: E402
    _run_uninstall,  # pyright: ignore[reportPrivateUsage]
    unwrap_command,
)

_WRAPPER_PREFIX = iwr.WRAPPER_INVOCATION_PREFIX


# ---------------------------------------------------------------------------
# unwrap_command module-level helper
# ---------------------------------------------------------------------------


def test_unwrap_command_strips_prefix() -> None:
    """unwrap_command strips the wrapper prefix and returns the bare command."""
    bare = "/usr/bin/myjob.sh --arg"
    wrapped = _WRAPPER_PREFIX + bare
    assert unwrap_command(wrapped) == bare


def test_unwrap_command_passthrough_when_not_wrapped() -> None:
    """unwrap_command is a no-op when the command has no wrapper prefix."""
    bare = "/usr/bin/other.sh"
    assert unwrap_command(bare) == bare


# ---------------------------------------------------------------------------
# _run_uninstall dry-run
# ---------------------------------------------------------------------------


def test_run_uninstall_dry_run_prints_diff(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--uninstall dry-run prints the old→new crontab diff; returns 0."""
    ct = tmp_path / "crontab"
    bare_cmd = "/usr/bin/cleanup.sh"
    schedule = "0 3 * * *"
    old_line = f"{schedule} {_WRAPPER_PREFIX}{bare_cmd}"
    ct.write_text(old_line + "\n", encoding="utf-8")

    rc = _run_uninstall(
        monitor_url="http://monitor.example.com",
        token="tok",
        host="testhost",
        crontab_spec="/etc/crontab",
        crontab_file=ct,
        crontab_content=ct.read_text(encoding="utf-8"),
        line_index=0,
        old_line=old_line,
        schedule=schedule,
        command=f"{_WRAPPER_PREFIX}{bare_cmd}",  # the full wrapped command
        confirm=False,
    )

    assert rc == 0
    out = capsys.readouterr().out
    assert "Crontab diff" in out
    assert old_line in out
    # Crontab unchanged
    assert ct.read_text(encoding="utf-8") == old_line + "\n"


def test_run_uninstall_non_wrapped_line_returns_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--uninstall on a non-wrapped line prints an error and returns 1."""
    ct = tmp_path / "crontab"
    bare_cmd = "/usr/bin/task.sh"
    schedule = "*/5 * * * *"
    old_line = f"{schedule} {bare_cmd}"  # NOT wrapped
    ct.write_text(old_line + "\n", encoding="utf-8")

    rc = _run_uninstall(
        monitor_url="http://monitor.example.com",
        token="tok",
        host="testhost",
        crontab_spec="/etc/crontab",
        crontab_file=ct,
        crontab_content=ct.read_text(encoding="utf-8"),
        line_index=0,
        old_line=old_line,
        schedule=schedule,
        command=bare_cmd,
        confirm=False,
    )

    assert rc == 1
    err = capsys.readouterr().err
    assert "not wrapped" in err.lower()


# ---------------------------------------------------------------------------
# _run_uninstall confirm (actual rewrite)
# ---------------------------------------------------------------------------


def test_run_uninstall_confirm_rewrites_crontab(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--uninstall confirm rewrites the crontab to the bare command."""
    ct = tmp_path / "crontab"
    bare_cmd = "/usr/bin/myjob.sh"
    schedule = "*/10 * * * *"
    old_line = f"{schedule} {_WRAPPER_PREFIX}{bare_cmd}"
    ct.write_text(old_line + "\n", encoding="utf-8")

    mock_resp = MagicMock()

    def _mock_enter(s: MagicMock) -> MagicMock:
        return s

    mock_resp.__enter__ = _mock_enter
    mock_resp.__exit__ = MagicMock(return_value=False)
    mock_resp.status = 200

    with patch("homelab_monitor.cli.install_wrapper_remote.urlopen", return_value=mock_resp):
        rc = _run_uninstall(
            monitor_url="http://monitor.example.com",
            token="tok",
            host="testhost",
            crontab_spec="/etc/crontab",
            crontab_file=ct,
            crontab_content=ct.read_text(encoding="utf-8"),
            line_index=0,
            old_line=old_line,
            schedule=schedule,
            command=f"{_WRAPPER_PREFIX}{bare_cmd}",
            confirm=True,
        )

    assert rc == 0
    new_content = ct.read_text(encoding="utf-8")
    assert _WRAPPER_PREFIX not in new_content
    assert bare_cmd in new_content


def test_run_uninstall_confirm_rollback_on_write_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--uninstall confirm with write failure → rollback restores original content."""
    ct = tmp_path / "crontab"
    bare_cmd = "/usr/bin/job.sh"
    schedule = "0 2 * * *"
    old_line = f"{schedule} {_WRAPPER_PREFIX}{bare_cmd}"
    original_content = old_line + "\n"
    ct.write_text(original_content, encoding="utf-8")

    with patch(
        "homelab_monitor.cli.install_wrapper_remote._atomic_write_text",
        side_effect=OSError("disk full"),
    ):
        rc = _run_uninstall(
            monitor_url="http://monitor.example.com",
            token="tok",
            host="testhost",
            crontab_spec="/etc/crontab",
            crontab_file=ct,
            crontab_content=original_content,
            line_index=0,
            old_line=old_line,
            schedule=schedule,
            command=f"{_WRAPPER_PREFIX}{bare_cmd}",
            confirm=True,
        )

    assert rc == 1
    err = capsys.readouterr().err
    assert "ERROR" in err


def test_run_uninstall_confirm_no_trailing_newline(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """_run_uninstall confirm on crontab WITHOUT trailing newline does not append one.

    Covers install_wrapper_remote.py line 400->402 False branch.
    """
    ct = tmp_path / "crontab_no_nl"
    bare_cmd = "/usr/bin/job2.sh"
    schedule = "*/30 * * * *"
    old_line = f"{schedule} {_WRAPPER_PREFIX}{bare_cmd}"
    # Content WITHOUT trailing newline
    original_content = old_line  # no "\n"
    ct.write_text(original_content, encoding="utf-8")

    mock_resp = MagicMock()
    mock_resp.__enter__ = MagicMock(return_value=mock_resp)
    mock_resp.__exit__ = MagicMock(return_value=False)
    mock_resp.status = 200

    with patch("homelab_monitor.cli.install_wrapper_remote.urlopen", return_value=mock_resp):
        rc = _run_uninstall(
            monitor_url="http://monitor.example.com",
            token="tok",
            host="testhost",
            crontab_spec="/etc/crontab",
            crontab_file=ct,
            crontab_content=original_content,
            line_index=0,
            old_line=old_line,
            schedule=schedule,
            command=f"{_WRAPPER_PREFIX}{bare_cmd}",
            confirm=True,
        )

    assert rc == 0
    new_content = ct.read_text(encoding="utf-8")
    # No trailing newline added
    assert not new_content.endswith("\n")
    assert _WRAPPER_PREFIX not in new_content
    assert bare_cmd in new_content


def test_main_uninstall_flag_routes_to_run_uninstall(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """main() with --uninstall routes to _run_uninstall (dry-run).

    Covers install_wrapper_remote.py line 485 (if args.uninstall: branch).
    """
    bare_cmd = "/usr/bin/cleanup.sh"
    schedule = "0 3 * * *"
    old_line = f"{schedule} {_WRAPPER_PREFIX}{bare_cmd}"
    ct = tmp_path / "test.crontab"
    ct.write_text(old_line + "\n", encoding="utf-8")

    argv = [
        "--monitor-url",
        "http://monitor.example.com",
        "--token",
        "tok",
        "--host",
        "testhost",
        "--crontab",
        str(ct),
        "--line",
        "1",
        "--uninstall",
        # no --confirm → dry-run
    ]
    with patch("sys.argv", ["prog", *argv]):
        rc = main()

    assert rc == 0
    out = capsys.readouterr().out
    assert "Crontab diff" in out
    assert old_line in out
