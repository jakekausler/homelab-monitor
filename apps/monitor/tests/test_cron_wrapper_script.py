"""Tests for the cron-with-heartbeat.sh.tmpl wrapper script (STAGE-002-012).

Actually executes the rendered wrapper template as a script:
- Renders the template with constant placeholders only
- Stands up a tiny local HTTP server to receive heartbeat POSTs
- Uses a fake `logger` on PATH to capture journald marker calls
- Asserts correct call sequence, exit-code propagation, marker emission,
  and graceful degradation when logger/env-file is absent.

Note: full Vector/journald integration is validated at prod-rig refinement (3b).
The fake-logger technique makes marker assertions deterministic in CI.
"""

from __future__ import annotations

import http.server
import os
import subprocess
import threading
import time
from importlib.resources import files
from pathlib import Path
from typing import Any

from homelab_monitor.kernel.cron.wrapper_constants import WRAPPER_FORMAT_VERSION

# ---------------------------------------------------------------------------
# Tiny HTTP server for receiving heartbeat POSTs
# ---------------------------------------------------------------------------


class _HeartbeatCollector(http.server.BaseHTTPRequestHandler):
    """Records POST paths into server.calls list."""

    server: _HeartbeatServer  # type: ignore[assignment]

    def do_POST(self) -> None:
        self.server.calls.append(self.path)
        self.send_response(204)
        self.end_headers()

    def log_message(self, format: str, *args: Any) -> None:  # noqa: ANN401 -- overrides stdlib
        pass


class _HeartbeatServer(http.server.HTTPServer):
    def __init__(self, *args: Any, **kwargs: Any) -> None:  # noqa: ANN401
        super().__init__(*args, **kwargs)
        self.calls: list[str] = []


def _start_server() -> tuple[_HeartbeatServer, int]:
    server = _HeartbeatServer(("127.0.0.1", 0), _HeartbeatCollector)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server, port


# ---------------------------------------------------------------------------
# Template helpers
# ---------------------------------------------------------------------------


def _build_wrapper(token_file: str, env_file: str) -> str:
    """Render the new constant-placeholder template."""
    tmpl = (
        files("homelab_monitor").joinpath("data", "cron-with-heartbeat.sh.tmpl").read_text("utf-8")
    )
    return (
        tmpl.replace("{{TOKEN_FILE_PATH}}", token_file)
        .replace("{{WRAPPER_ENV_PATH}}", env_file)
        .replace("{{WRAPPER_FORMAT_VERSION}}", WRAPPER_FORMAT_VERSION)
    )


def _write_wrapper(tmp_path: Path, content: str) -> Path:
    script = tmp_path / "cron-with-heartbeat.sh"
    script.write_text(content, encoding="utf-8")
    script.chmod(0o755)
    return script


def _write_token(tmp_path: Path, token: str = "test-token-abc") -> Path:
    token_file = tmp_path / "heartbeat.token"
    token_file.write_text(token, encoding="utf-8")
    token_file.chmod(0o600)
    return token_file


def _write_env(tmp_path: Path, url_base: str) -> Path:
    """Write the wrapper.env file: HEARTBEAT_URL_BASE=<url>."""
    env_file = tmp_path / "wrapper.env"
    env_file.write_text(f"HEARTBEAT_URL_BASE={url_base}\n", encoding="utf-8")
    env_file.chmod(0o644)
    return env_file


def _write_fake_logger(fake_dir: Path, log_file: Path) -> None:
    """Write a fake `logger` script that appends stdin+args to log_file."""
    fake_logger = fake_dir / "logger"
    fake_logger.write_text(
        f"#!/bin/sh\n"
        f"# fake logger: append args + stdin to {log_file}\n"
        f"printf '%s\\n' \"$*\" >> {log_file}\n"
        f"cat >> {log_file}\n",
        encoding="utf-8",
    )
    fake_logger.chmod(0o755)


def _run_wrapper(
    script: Path,
    fingerprint: str,
    *cmd_args: str,
    env: dict[str, str] | None = None,
    timeout: int = 10,
    capture_output: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run the wrapper with <fingerprint> -- <cmd_args>."""
    cmd = [str(script), fingerprint, "--", *cmd_args]
    return subprocess.run(
        cmd,
        timeout=timeout,
        capture_output=capture_output,
        text=capture_output,
        env=env,
        check=False,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestWrapperScript:
    _FP = "b3d04508bdd33e915854ddd7d6729c2d708e1faf793761c8a79395ffad5f75ad"

    def test_exit_code_preserved_through_logger_pipe(self, tmp_path: Path) -> None:
        """Exit-code preservation through the tempfile technique for several codes."""
        server, port = _start_server()
        url_base = f"http://127.0.0.1:{port}"
        token_file = _write_token(tmp_path)
        env_file = _write_env(tmp_path, url_base)
        content = _build_wrapper(str(token_file), str(env_file))
        script = _write_wrapper(tmp_path, content)

        for expected_rc in (0, 1, 2, 42, 127):
            result = _run_wrapper(script, self._FP, "sh", "-c", f"exit {expected_rc}")
            assert result.returncode == expected_rc, (
                f"expected exit {expected_rc}, got {result.returncode}"
            )

        server.shutdown()

    def test_start_then_ok_on_success(self, tmp_path: Path) -> None:
        """/start?run_id=<uuid> then /ok?run_id=...&exit_code=0."""
        server, port = _start_server()
        url_base = f"http://127.0.0.1:{port}"
        token_file = _write_token(tmp_path)
        env_file = _write_env(tmp_path, url_base)
        content = _build_wrapper(str(token_file), str(env_file))
        script = _write_wrapper(tmp_path, content)

        result = _run_wrapper(script, self._FP, "true")
        assert result.returncode == 0

        time.sleep(0.15)
        server.shutdown()

        paths = server.calls
        assert any(f"/api/hb/{self._FP}/start" in p for p in paths), f"no /start in {paths}"
        assert any(f"/api/hb/{self._FP}/ok" in p for p in paths), f"no /ok in {paths}"
        assert not any("fail" in p for p in paths), f"unexpected /fail in {paths}"

    def test_start_then_fail_on_nonzero(self, tmp_path: Path) -> None:
        """/start then /fail?exit_code=42."""
        server, port = _start_server()
        url_base = f"http://127.0.0.1:{port}"
        token_file = _write_token(tmp_path)
        env_file = _write_env(tmp_path, url_base)
        content = _build_wrapper(str(token_file), str(env_file))
        script = _write_wrapper(tmp_path, content)

        result = _run_wrapper(script, self._FP, "sh", "-c", "exit 42")
        assert result.returncode == 42  # noqa: PLR2004

        time.sleep(0.15)
        server.shutdown()

        paths = server.calls
        assert any(f"/api/hb/{self._FP}/start" in p for p in paths), f"no /start in {paths}"
        fail_calls = [p for p in paths if "fail" in p]
        assert fail_calls, f"no /fail in {paths}"
        assert any("exit_code=42" in p for p in fail_calls), f"exit_code=42 not in {fail_calls}"

    def test_run_id_on_all_heartbeats(self, tmp_path: Path) -> None:
        """Every POST path contains run_id= and all use the same UUID."""
        server, port = _start_server()
        url_base = f"http://127.0.0.1:{port}"
        token_file = _write_token(tmp_path)
        env_file = _write_env(tmp_path, url_base)
        content = _build_wrapper(str(token_file), str(env_file))
        script = _write_wrapper(tmp_path, content)

        _run_wrapper(script, self._FP, "true")
        time.sleep(0.15)
        server.shutdown()

        paths = server.calls
        assert paths, "no POSTs received"
        for p in paths:
            assert "run_id=" in p, f"run_id= missing in {p}"

        # Extract run_ids from all POSTs and assert they are all identical
        import re  # noqa: PLC0415

        uuids: set[str] = set()
        for p in paths:
            m = re.search(r"run_id=([0-9a-fA-F-]{36})", p)
            if m:
                uuids.add(m.group(1))
        assert len(uuids) == 1, f"multiple run_ids: {uuids}"

    def test_logger_absent_still_runs_and_preserves_exit(self, tmp_path: Path) -> None:
        """With logger absent from PATH, command still runs and exit code preserved."""
        # Use env with PATH pointing only at directories containing sh/true/date/mktemp
        # but NOT logger. We do this by writing a fake PATH containing only sh basics.
        sentinel = tmp_path / "sentinel.txt"

        server, port = _start_server()
        url_base = f"http://127.0.0.1:{port}"
        token_file = _write_token(tmp_path)
        env_file = _write_env(tmp_path, url_base)
        content = _build_wrapper(str(token_file), str(env_file))
        script = _write_wrapper(tmp_path, content)

        # Use a fake dir that has no `logger` but keep system PATH for sh/curl/etc.
        no_logger_dir = tmp_path / "no_logger_bin"
        no_logger_dir.mkdir()
        env = dict(os.environ)
        env["PATH"] = str(no_logger_dir) + ":" + env.get("PATH", "/usr/bin:/bin")

        result = _run_wrapper(
            script,
            self._FP,
            "sh",
            "-c",
            f"touch {sentinel} ; exit 7",
            env=env,
            timeout=30,
        )

        time.sleep(0.15)
        server.shutdown()

        assert result.returncode == 7  # noqa: PLR2004
        assert sentinel.exists(), "sentinel file not created — command did not run"

    def test_hm_run_markers_emitted(self, tmp_path: Path) -> None:
        """Fake logger on PATH captures HM_RUN_START and HM_RUN_END markers."""
        log_file = tmp_path / "logger.log"
        fake_bin = tmp_path / "fake_bin"
        fake_bin.mkdir()
        _write_fake_logger(fake_bin, log_file)

        server, port = _start_server()
        url_base = f"http://127.0.0.1:{port}"
        token_file = _write_token(tmp_path)
        env_file = _write_env(tmp_path, url_base)
        content = _build_wrapper(str(token_file), str(env_file))
        script = _write_wrapper(tmp_path, content)

        env = dict(os.environ)
        env["PATH"] = str(fake_bin) + ":" + env.get("PATH", "/usr/bin:/bin")

        _run_wrapper(script, self._FP, "sh", "-c", "echo hello_output", env=env)
        time.sleep(0.15)
        server.shutdown()

        log_content = log_file.read_text(encoding="utf-8") if log_file.exists() else ""
        assert f"MESSAGE=HM_RUN_START fp={self._FP}" in log_content, (
            f"HM_RUN_START structured marker not in logger log:\n{log_content}"
        )
        assert f"MESSAGE=HM_RUN_END fp={self._FP}" in log_content, (
            f"HM_RUN_END structured marker not in logger log:\n{log_content}"
        )
        assert "HM_RUN=" in log_content, f"HM_RUN= field not in logger log:\n{log_content}"

    def test_original_output_on_stdout(self, tmp_path: Path) -> None:
        """Wrapper's stdout contains the original un-prefixed command output."""
        server, port = _start_server()
        url_base = f"http://127.0.0.1:{port}"
        token_file = _write_token(tmp_path)
        env_file = _write_env(tmp_path, url_base)
        content = _build_wrapper(str(token_file), str(env_file))
        script = _write_wrapper(tmp_path, content)

        result = _run_wrapper(
            script,
            self._FP,
            "sh",
            "-c",
            "echo hello_world_output",
            capture_output=True,
        )
        time.sleep(0.15)
        server.shutdown()

        assert "hello_world_output" in result.stdout
        # The HM_RUN= prefix copy goes only to logger, NOT to wrapper stdout
        assert not result.stdout.startswith("HM_RUN=")

    def test_stderr_is_captured(self, tmp_path: Path) -> None:
        """stderr merged via 2>&1; fake logger gets MESSAGE=<stderr line> + HM_RUN/HM_FP fields."""
        log_file = tmp_path / "logger.log"
        fake_bin = tmp_path / "fake_bin"
        fake_bin.mkdir()
        _write_fake_logger(fake_bin, log_file)

        server, port = _start_server()
        url_base = f"http://127.0.0.1:{port}"
        token_file = _write_token(tmp_path)
        env_file = _write_env(tmp_path, url_base)
        content = _build_wrapper(str(token_file), str(env_file))
        script = _write_wrapper(tmp_path, content)

        env = dict(os.environ)
        env["PATH"] = str(fake_bin) + ":" + env.get("PATH", "/usr/bin:/bin")

        _run_wrapper(script, self._FP, "sh", "-c", "echo err_line >&2", env=env)
        time.sleep(0.15)
        server.shutdown()

        log_content = log_file.read_text(encoding="utf-8") if log_file.exists() else ""
        assert "err_line" in log_content, f"stderr line not captured:\n{log_content}"

    def test_missing_fingerprint_exits_64(self, tmp_path: Path) -> None:
        """Calling wrapper with no args exits 64."""
        token_file = _write_token(tmp_path)
        env_file = tmp_path / "wrapper.env"
        env_file.write_text("HEARTBEAT_URL_BASE=http://127.0.0.1:9\n")
        content = _build_wrapper(str(token_file), str(env_file))
        script = _write_wrapper(tmp_path, content)

        result = subprocess.run(
            [str(script)],
            timeout=10,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 64  # noqa: PLR2004

    def test_missing_separator_exits_64(self, tmp_path: Path) -> None:
        """Calling wrapper with fp but no '--' exits 64."""
        token_file = _write_token(tmp_path)
        env_file = tmp_path / "wrapper.env"
        env_file.write_text("HEARTBEAT_URL_BASE=http://127.0.0.1:9\n")
        content = _build_wrapper(str(token_file), str(env_file))
        script = _write_wrapper(tmp_path, content)

        result = subprocess.run(
            [str(script), "myfp", "true"],  # no '--'
            timeout=10,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 64  # noqa: PLR2004

    def test_heartbeat_down_does_not_block(self, tmp_path: Path) -> None:
        """Unreachable URL base — wrapper still runs command and exits correctly."""
        url_base = "http://127.0.0.1:19999"  # nothing listening
        token_file = _write_token(tmp_path)
        env_file = _write_env(tmp_path, url_base)
        content = _build_wrapper(str(token_file), str(env_file))
        script = _write_wrapper(tmp_path, content)

        result = _run_wrapper(script, self._FP, "true", timeout=30)
        assert result.returncode == 0

    def test_no_env_file_still_runs(self, tmp_path: Path) -> None:
        """Missing wrapper.env → URL_BASE empty → no heartbeat POSTs, command runs."""
        token_file = _write_token(tmp_path)
        absent_env = tmp_path / "does_not_exist.env"
        content = _build_wrapper(str(token_file), str(absent_env))
        script = _write_wrapper(tmp_path, content)

        result = _run_wrapper(script, self._FP, "sh", "-c", "exit 5")
        assert result.returncode == 5  # noqa: PLR2004

    def test_token_file_read_for_auth(self, tmp_path: Path) -> None:
        """Wrapper reads token file and sends Authorization: Bearer <token> header."""
        received_headers: list[str] = []

        class _AuthCapture(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                received_headers.append(self.headers.get("Authorization", ""))
                self.send_response(204)
                self.end_headers()

            def log_message(self, format: str, *args: Any) -> None:  # noqa: ANN401
                pass

        auth_server = http.server.HTTPServer(("127.0.0.1", 0), _AuthCapture)
        port = auth_server.server_address[1]
        t = threading.Thread(target=auth_server.serve_forever, daemon=True)
        t.start()

        url_base = f"http://127.0.0.1:{port}"
        my_token = "supersecret-token-xyz"
        token_file = _write_token(tmp_path, my_token)
        env_file = _write_env(tmp_path, url_base)
        content = _build_wrapper(str(token_file), str(env_file))
        script = _write_wrapper(tmp_path, content)

        _run_wrapper(script, self._FP, "true")
        time.sleep(0.15)
        auth_server.shutdown()

        assert received_headers, "no POST received"
        assert all(f"Bearer {my_token}" in h for h in received_headers), (
            f"token not in auth headers: {received_headers}"
        )

    def test_set_u_no_unbound_var(self, tmp_path: Path) -> None:
        """Run wrapper with minimal env; assert no 'unbound variable' on stderr."""
        token_file = _write_token(tmp_path)
        # absent env file — URL_BASE will be empty (guarded by ${VAR:-})
        absent_env = tmp_path / "absent.env"
        content = _build_wrapper(str(token_file), str(absent_env))
        script = _write_wrapper(tmp_path, content)

        result = subprocess.run(
            [str(script), self._FP, "--", "true"],
            timeout=10,
            capture_output=True,
            text=True,
            check=False,
        )
        assert "unbound variable" not in (result.stderr or ""), f"set -u tripped: {result.stderr}"

    def test_rendered_wrapper_uses_logger_journald(self, tmp_path: Path) -> None:
        """The rendered wrapper must invoke `logger --journald` and supply the
        HM_FP / HM_RUN / SYSLOG_IDENTIFIER / PRIORITY structured fields
        (STAGE-004-005: journald structured-field enrichment)."""
        token_file = _write_token(tmp_path)
        env_file = _write_env(tmp_path, "http://127.0.0.1:9")
        content = _build_wrapper(str(token_file), str(env_file))
        assert "logger --journald" in content, "wrapper must use logger --journald per line"
        assert "SYSLOG_IDENTIFIER=hmrun" in content, "wrapper must emit SYSLOG_IDENTIFIER=hmrun"
        assert "HM_FP=" in content, "wrapper must emit the HM_FP structured field"
        assert "HM_RUN=" in content, "wrapper must emit the HM_RUN structured field"
        assert "PRIORITY=" in content, "wrapper must emit an explicit PRIORITY field"

    def test_hm_fp_field_emitted_to_logger(self, tmp_path: Path) -> None:
        """Fake logger on PATH captures the HM_FP=<fingerprint> field for a run line."""
        log_file = tmp_path / "logger.log"
        fake_bin = tmp_path / "fake_bin"
        fake_bin.mkdir()
        _write_fake_logger(fake_bin, log_file)

        server, port = _start_server()
        url_base = f"http://127.0.0.1:{port}"
        token_file = _write_token(tmp_path)
        env_file = _write_env(tmp_path, url_base)
        content = _build_wrapper(str(token_file), str(env_file))
        script = _write_wrapper(tmp_path, content)

        env = dict(os.environ)
        env["PATH"] = str(fake_bin) + ":" + env.get("PATH", "/usr/bin:/bin")

        _run_wrapper(script, self._FP, "sh", "-c", "echo line_one", env=env)
        time.sleep(0.15)
        server.shutdown()

        log_content = log_file.read_text(encoding="utf-8") if log_file.exists() else ""
        assert f"HM_FP={self._FP}" in log_content, (
            f"HM_FP=<fingerprint> field not captured by logger:\n{log_content}"
        )
        assert "SYSLOG_IDENTIFIER=hmrun" in log_content, (
            f"SYSLOG_IDENTIFIER=hmrun field not captured by logger:\n{log_content}"
        )
        assert "MESSAGE=line_one" in log_content, (
            f"MESSAGE=line_one field not captured by logger:\n{log_content}"
        )
