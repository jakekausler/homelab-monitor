"""Tests for the cron-with-heartbeat.sh.tmpl wrapper script (STAGE-002-009).

Actually executes the wrapper template as a script:
- Substitutes all 4 template variables
- Stands up a tiny local HTTP server to receive heartbeat POSTs
- Asserts correct call sequence and exit-code propagation
- Asserts heartbeat failures do NOT block the wrapped command
"""

from __future__ import annotations

import http.server
import threading
import time
from importlib.resources import files
from pathlib import Path
from typing import Any

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

    def log_message(self, format: str, *args: Any) -> None:  # noqa: ANN401 -- overrides stdlib BaseHTTPRequestHandler.log_message signature
        pass  # suppress default stderr logging


class _HeartbeatServer(http.server.HTTPServer):
    def __init__(self, *args: Any, **kwargs: Any) -> None:  # noqa: ANN401 -- passthrough to stdlib HTTPServer.__init__
        super().__init__(*args, **kwargs)
        self.calls: list[str] = []


def _start_server() -> tuple[_HeartbeatServer, int]:
    server = _HeartbeatServer(("127.0.0.1", 0), _HeartbeatCollector)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server, port


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_wrapper(
    fingerprint: str,
    url_base: str,
    token_file: str,
    install_date: str = "2024-01-15",
) -> str:
    """Read the template and substitute all 4 variables."""
    tmpl = (
        files("homelab_monitor")
        .joinpath("data", "cron-with-heartbeat.sh.tmpl")
        .read_text(encoding="utf-8")
    )
    return (
        tmpl.replace("{{FINGERPRINT}}", fingerprint)
        .replace("{{HEARTBEAT_URL_BASE}}", url_base)
        .replace("{{TOKEN_FILE_PATH}}", token_file)
        .replace("{{INSTALL_DATE}}", install_date)
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


def _run_wrapper(script: Path, *cmd_args: str, timeout: int = 10) -> int:
    """Run the wrapper script with given args; return exit code."""
    import subprocess  # noqa: PLC0415

    result = subprocess.run(
        [str(script), "--", *cmd_args],
        timeout=timeout,
        capture_output=False,
        check=False,
    )
    return result.returncode


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestWrapperScript:
    def test_start_then_ok_on_success(self, tmp_path: Path) -> None:
        """/start is POSTed before the command, then /ok on exit 0."""
        server, port = _start_server()
        url_base = f"http://127.0.0.1:{port}"
        token_file = _write_token(tmp_path)
        fp = "abc123"

        content = _build_wrapper(fp, url_base, str(token_file))
        script = _write_wrapper(tmp_path, content)

        rc = _run_wrapper(script, "true")

        assert rc == 0
        # Give server a moment to process last POST
        time.sleep(0.1)
        server.shutdown()

        paths = server.calls
        assert any(f"/api/hb/{fp}/start" in p for p in paths), f"no /start in {paths}"
        assert any(f"/api/hb/{fp}/ok" in p for p in paths), f"no /ok in {paths}"
        assert not any("fail" in p for p in paths), f"unexpected /fail in {paths}"

    def test_start_then_fail_on_nonzero(self, tmp_path: Path) -> None:
        """/start is POSTed, then /fail?exit_code=N on non-zero exit."""
        server, port = _start_server()
        url_base = f"http://127.0.0.1:{port}"
        token_file = _write_token(tmp_path)
        fp = "def456"

        content = _build_wrapper(fp, url_base, str(token_file))
        script = _write_wrapper(tmp_path, content)

        rc = _run_wrapper(script, "sh", "-c", "exit 42")

        assert rc == 42  # noqa: PLR2004
        time.sleep(0.1)
        server.shutdown()

        paths = server.calls
        assert any(f"/api/hb/{fp}/start" in p for p in paths), f"no /start in {paths}"
        fail_calls = [p for p in paths if "fail" in p]
        assert fail_calls, f"no /fail in {paths}"
        assert any("exit_code=42" in p for p in fail_calls), f"exit_code=42 not in {fail_calls}"

    def test_exit_code_preserved(self, tmp_path: Path) -> None:
        """Wrapper preserves the exact exit code of the wrapped command."""
        server, port = _start_server()
        url_base = f"http://127.0.0.1:{port}"
        token_file = _write_token(tmp_path)
        fp = "ghi789"

        content = _build_wrapper(fp, url_base, str(token_file))
        script = _write_wrapper(tmp_path, content)

        for expected_rc in (0, 1, 2, 127):
            rc = _run_wrapper(script, "sh", "-c", f"exit {expected_rc}")
            assert rc == expected_rc, f"expected exit code {expected_rc}, got {rc}"

        server.shutdown()

    def test_heartbeat_down_does_not_block(self, tmp_path: Path) -> None:
        """When HTTP server is unreachable, wrapper still runs command + exits correctly."""
        # Use a port that's not listening
        url_base = "http://127.0.0.1:19999"  # nothing listening here
        token_file = _write_token(tmp_path)
        fp = "jkl012"

        content = _build_wrapper(fp, url_base, str(token_file))
        # Reduce curl timeout from 5s to speed up test (the template hardcodes 5s max-time)
        # We can't easily change it; just run with timeout guard and assert command ran
        script = _write_wrapper(tmp_path, content)

        # command itself is instant; the curl --max-time 5 may add delay
        # We accept the test being slow (up to ~10s) but it MUST not hang forever
        rc = _run_wrapper(script, "true", timeout=30)
        assert rc == 0

    def test_missing_separator_exits_64(self, tmp_path: Path) -> None:
        """Calling wrapper without '--' separator exits 64 per spec."""
        import subprocess  # noqa: PLC0415

        server, port = _start_server()
        url_base = f"http://127.0.0.1:{port}"
        token_file = _write_token(tmp_path)
        fp = "sep001"

        content = _build_wrapper(fp, url_base, str(token_file))
        script = _write_wrapper(tmp_path, content)

        # Call WITHOUT '--'
        result = subprocess.run(
            [str(script), "true"],  # no '--' separator
            timeout=10,
            capture_output=True,
            check=False,
        )
        server.shutdown()
        assert result.returncode == 64  # noqa: PLR2004

    def test_token_file_read_for_auth(self, tmp_path: Path) -> None:
        """Wrapper reads token file and sends Authorization header."""
        received_headers: list[str] = []

        class _AuthCapture(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                received_headers.append(self.headers.get("Authorization", ""))
                self.send_response(204)
                self.end_headers()

            def log_message(self, format: str, *args: Any) -> None:  # noqa: ANN401 -- overrides stdlib BaseHTTPRequestHandler.log_message signature
                pass

        auth_server = http.server.HTTPServer(("127.0.0.1", 0), _AuthCapture)
        port = auth_server.server_address[1]
        t = threading.Thread(target=auth_server.serve_forever, daemon=True)
        t.start()

        url_base = f"http://127.0.0.1:{port}"
        my_token = "supersecret-token-xyz"
        token_file = _write_token(tmp_path, my_token)
        fp = "tok001"

        content = _build_wrapper(fp, url_base, str(token_file))
        script = _write_wrapper(tmp_path, content)
        _run_wrapper(script, "true")

        time.sleep(0.1)
        auth_server.shutdown()

        assert received_headers, "no POST received"
        assert all(f"Bearer {my_token}" in h for h in received_headers), (
            f"token not in auth headers: {received_headers}"
        )
