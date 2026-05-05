"""CRITICAL: assert plaintext secret values never appear in stdout/stderr/structlog.

Captures ALL THREE channels and runs every CLI op. The only place the sentinel
is allowed to appear is the explicit ``hm secrets get`` stdout when REVEAL=1 —
that's the one operation whose entire purpose is to print the value.
"""

from __future__ import annotations

import base64
import io
import logging
import sqlite3
from urllib.parse import urlparse

import pytest

from homelab_monitor.cli.main import main
from homelab_monitor.cli.secrets import REVEAL_ENV
from homelab_monitor.kernel.secrets.master_key import ENV_VAR

KEY = bytes(range(32))
KEY_B64 = base64.b64encode(KEY).decode("ascii")
NEW_KEY_B64 = base64.b64encode(bytes(range(32, 64))).decode("ascii")

SENTINEL = "hl-test-secret-v1-7c3f8b9a-DO-NOT-LEAK"


@pytest.fixture
def setup_env(db_url_env: str, monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv(ENV_VAR, KEY_B64)
    monkeypatch.delenv(REVEAL_ENV, raising=False)
    return db_url_env


def _stdin(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(value))


class _LogCapture(logging.Handler):
    """Capture every log record's formatted message."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(self.format(record))


def _attach_root_capture() -> _LogCapture:
    """Attach a capture handler to the root logger.

    structlog (when configured) routes through stdlib logging; SQLAlchemy and
    Alembic also log to stdlib. Capturing at the root catches all of them.
    """
    handler = _LogCapture()
    handler.setFormatter(logging.Formatter("%(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    return handler


def _detach(handler: _LogCapture) -> None:
    logging.getLogger().removeHandler(handler)


def _assert_no_leak(
    captured: object,
    log_records: list[str],
    *,
    allow_in_stdout: bool = False,
) -> None:
    """Fail unless the sentinel is absent from stderr + log records.

    ``allow_in_stdout=True`` permits the sentinel in stdout (the one OK case is
    ``hm secrets get`` with REVEAL=1).
    """
    if not allow_in_stdout:
        assert SENTINEL not in captured.out, f"sentinel leaked to stdout: {captured.out!r}"  # type: ignore[union-attr]
    assert SENTINEL not in captured.err, f"sentinel leaked to stderr: {captured.err!r}"  # type: ignore[union-attr]
    for rec in log_records:
        assert SENTINEL not in rec, f"sentinel leaked to logs: {rec!r}"


def test_set_does_not_leak(
    setup_env: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    main(["migrate"])
    capsys.readouterr()
    handler = _attach_root_capture()
    try:
        _stdin(monkeypatch, SENTINEL)
        rc = main(["secrets", "set", "tok", "--from-stdin"])
    finally:
        _detach(handler)
    captured = capsys.readouterr()
    assert rc == 0
    _assert_no_leak(captured, handler.records)


def test_list_does_not_leak(
    setup_env: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    main(["migrate"])
    capsys.readouterr()
    _stdin(monkeypatch, SENTINEL)
    main(["secrets", "set", "tok", "--from-stdin"])
    capsys.readouterr()

    handler = _attach_root_capture()
    try:
        rc = main(["secrets", "list"])
    finally:
        _detach(handler)
    captured = capsys.readouterr()
    assert rc == 0
    _assert_no_leak(captured, handler.records)


def test_get_without_reveal_does_not_leak(
    setup_env: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """REVEAL not set → get fails AND must not leak."""
    main(["migrate"])
    capsys.readouterr()
    _stdin(monkeypatch, SENTINEL)
    main(["secrets", "set", "tok", "--from-stdin"])
    capsys.readouterr()

    handler = _attach_root_capture()
    try:
        rc = main(["secrets", "get", "tok"])
    finally:
        _detach(handler)
    captured = capsys.readouterr()
    assert rc == 1
    _assert_no_leak(captured, handler.records)


def test_get_with_reveal_only_leaks_to_stdout(
    setup_env: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """REVEAL=1 → sentinel allowed in stdout only; never stderr or logs."""
    main(["migrate"])
    capsys.readouterr()
    _stdin(monkeypatch, SENTINEL)
    main(["secrets", "set", "tok", "--from-stdin"])
    capsys.readouterr()
    monkeypatch.setenv(REVEAL_ENV, "1")

    handler = _attach_root_capture()
    try:
        rc = main(["secrets", "get", "tok"])
    finally:
        _detach(handler)
    captured = capsys.readouterr()
    assert rc == 0
    assert SENTINEL in captured.out  # this is the ONE allowed channel
    _assert_no_leak(captured, handler.records, allow_in_stdout=True)


def test_rotate_does_not_leak(
    setup_env: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    main(["migrate"])
    capsys.readouterr()
    _stdin(monkeypatch, "old-value")
    main(["secrets", "set", "tok", "--from-stdin"])
    capsys.readouterr()

    handler = _attach_root_capture()
    try:
        _stdin(monkeypatch, SENTINEL)
        rc = main(["secrets", "rotate", "tok", "--from-stdin"])
    finally:
        _detach(handler)
    captured = capsys.readouterr()
    assert rc == 0
    _assert_no_leak(captured, handler.records)


def test_delete_does_not_leak(
    setup_env: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    main(["migrate"])
    capsys.readouterr()
    _stdin(monkeypatch, SENTINEL)
    main(["secrets", "set", "tok", "--from-stdin"])
    capsys.readouterr()

    handler = _attach_root_capture()
    try:
        rc = main(["secrets", "delete", "tok"])
    finally:
        _detach(handler)
    captured = capsys.readouterr()
    assert rc == 0
    _assert_no_leak(captured, handler.records)


def test_rotate_master_does_not_leak(
    setup_env: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    main(["migrate"])
    capsys.readouterr()
    _stdin(monkeypatch, SENTINEL)
    main(["secrets", "set", "tok", "--from-stdin"])
    capsys.readouterr()

    handler = _attach_root_capture()
    try:
        _stdin(monkeypatch, NEW_KEY_B64)
        rc = main(["secrets", "rotate-master", "--from-stdin"])
    finally:
        _detach(handler)
    captured = capsys.readouterr()
    assert rc == 0
    _assert_no_leak(captured, handler.records)


def test_no_leak_on_corrupted_get(
    db_url_env: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Sentinel value must not leak via stderr or logs when get fails on corrupted ciphertext."""
    SENTINEL = "no-leak-corrupt-7c4f8a2-e6"
    KEY_B64 = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8="
    monkeypatch.setenv(ENV_VAR, KEY_B64)
    monkeypatch.setenv(REVEAL_ENV, "1")

    main(["migrate"])
    capsys.readouterr()

    # Set the secret with the sentinel value.
    monkeypatch.setattr("sys.stdin", io.StringIO(SENTINEL))
    main(["secrets", "set", "leak_corrupt", "--from-stdin"])
    capsys.readouterr()

    # Corrupt the row directly via sqlite3.
    parsed = urlparse(db_url_env.replace("sqlite+aiosqlite", "sqlite"))
    db_file = parsed.path
    conn = sqlite3.connect(db_file)
    try:
        conn.execute(
            "UPDATE secrets SET ciphertext = ? WHERE name = ?",
            ("AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA", "leak_corrupt"),
        )
        conn.commit()
    finally:
        conn.close()

    # Attempt get — should fail.
    caplog.set_level("DEBUG")
    rc = main(["secrets", "get", "leak_corrupt"])
    captured = capsys.readouterr()
    assert rc == 1
    log_text = "\n".join(record.getMessage() for record in caplog.records)
    assert SENTINEL not in captured.out, "sentinel leaked to stdout"
    assert SENTINEL not in captured.err, "sentinel leaked to stderr"
    assert SENTINEL not in log_text, "sentinel leaked to log records"
