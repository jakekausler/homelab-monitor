"""Tests for ``hm secrets`` CLI subcommands."""

from __future__ import annotations

import base64
import io
import sqlite3
from urllib.parse import urlparse

import pytest

from homelab_monitor.cli.main import main
from homelab_monitor.cli.secrets import REVEAL_ENV
from homelab_monitor.kernel.secrets.master_key import ENV_VAR

KEY = bytes(range(32))
KEY_B64 = base64.b64encode(KEY).decode("ascii")
NEW_KEY = bytes(range(32, 64))
NEW_KEY_B64 = base64.b64encode(NEW_KEY).decode("ascii")


@pytest.fixture
def cli_env(db_url_env: str, monkeypatch: pytest.MonkeyPatch) -> str:
    """Environment with DB URL + master key set; REVEAL is intentionally NOT set."""
    monkeypatch.setenv(ENV_VAR, KEY_B64)
    monkeypatch.delenv(REVEAL_ENV, raising=False)
    return db_url_env


def _stdin(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    """Replace ``sys.stdin`` with a StringIO containing ``value``."""
    monkeypatch.setattr("sys.stdin", io.StringIO(value))


def test_set_then_get_requires_reveal(
    cli_env: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``hm secrets get`` without REVEAL=1 exits 1; with REVEAL=1 it prints."""
    main(["migrate"])
    capsys.readouterr()
    _stdin(monkeypatch, "hl-cli-test-value")
    assert main(["secrets", "set", "tok", "--from-stdin"]) == 0
    capsys.readouterr()

    rc = main(["secrets", "get", "tok"])
    captured = capsys.readouterr()
    assert rc == 1
    assert REVEAL_ENV in captured.err

    monkeypatch.setenv(REVEAL_ENV, "1")
    rc = main(["secrets", "get", "tok"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "hl-cli-test-value" in captured.out


def test_get_missing_secret_errors(
    cli_env: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Getting an unknown name with REVEAL=1 exits 1."""
    main(["migrate"])
    capsys.readouterr()
    monkeypatch.setenv(REVEAL_ENV, "1")
    rc = main(["secrets", "get", "missing"])
    captured = capsys.readouterr()
    assert rc == 1
    assert "no secret" in captured.err


def test_set_strips_trailing_newline(
    cli_env: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stdin pipes from ``echo`` have a trailing newline that must be stripped."""
    main(["migrate"])
    capsys.readouterr()
    _stdin(monkeypatch, "value-with-newline\n")
    assert main(["secrets", "set", "tok", "--from-stdin"]) == 0
    capsys.readouterr()

    monkeypatch.setenv(REVEAL_ENV, "1")
    main(["secrets", "get", "tok"])
    out = capsys.readouterr().out.strip()
    assert out == "value-with-newline"


def test_list_no_secrets(
    cli_env: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``hm secrets list`` on an empty store prints ``(no secrets)``."""
    main(["migrate"])
    capsys.readouterr()
    rc = main(["secrets", "list"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "(no secrets)" in out


def test_list_prints_metadata_only(
    cli_env: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """List output contains names + timestamps, never plaintext values."""
    main(["migrate"])
    capsys.readouterr()
    _stdin(monkeypatch, "alpha-secret-value")
    main(["secrets", "set", "alpha", "--from-stdin"])
    _stdin(monkeypatch, "beta-secret-value")
    main(["secrets", "set", "beta", "--from-stdin"])
    capsys.readouterr()

    rc = main(["secrets", "list"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "alpha" in out
    assert "beta" in out
    assert "alpha-secret-value" not in out
    assert "beta-secret-value" not in out


def test_rotate(
    cli_env: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``hm secrets rotate`` replaces the value."""
    main(["migrate"])
    capsys.readouterr()
    _stdin(monkeypatch, "old-val")
    main(["secrets", "set", "tok", "--from-stdin"])
    _stdin(monkeypatch, "new-val")
    rc = main(["secrets", "rotate", "tok", "--from-stdin"])
    capsys.readouterr()
    assert rc == 0

    monkeypatch.setenv(REVEAL_ENV, "1")
    main(["secrets", "get", "tok"])
    assert "new-val" in capsys.readouterr().out


def test_rotate_unknown_errors(
    cli_env: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rotating an unknown name exits 1 with ``no secret`` on stderr."""
    main(["migrate"])
    capsys.readouterr()
    _stdin(monkeypatch, "v")
    rc = main(["secrets", "rotate", "missing", "--from-stdin"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "no secret" in err


def test_delete(
    cli_env: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``hm secrets delete`` removes the secret."""
    main(["migrate"])
    capsys.readouterr()
    _stdin(monkeypatch, "v")
    main(["secrets", "set", "tok", "--from-stdin"])
    rc = main(["secrets", "delete", "tok"])
    capsys.readouterr()
    assert rc == 0

    rc = main(["secrets", "list"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "tok" not in out


def test_delete_unknown_errors(
    cli_env: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Deleting a missing secret exits 1 with ``no secret`` on stderr."""
    main(["migrate"])
    capsys.readouterr()
    rc = main(["secrets", "delete", "never-existed"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "no secret" in err


def test_rotate_master(
    cli_env: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``hm secrets rotate-master`` reads new key from stdin and re-encrypts."""
    main(["migrate"])
    capsys.readouterr()
    _stdin(monkeypatch, "value-a")
    main(["secrets", "set", "alpha", "--from-stdin"])
    capsys.readouterr()

    _stdin(monkeypatch, NEW_KEY_B64)
    rc = main(["secrets", "rotate-master", "--from-stdin"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "1 secret" in out
    assert "old fingerprint" in out
    assert "new fingerprint" in out

    # After rotate, the env still points at the OLD key — verify the operation
    # succeeded server-side by re-running with the new key set.
    monkeypatch.setenv(ENV_VAR, NEW_KEY_B64)
    monkeypatch.setenv(REVEAL_ENV, "1")
    main(["secrets", "get", "alpha"])
    assert "value-a" in capsys.readouterr().out


def test_rotate_master_rejects_bad_key(
    cli_env: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A malformed base64 stdin payload exits 1."""
    main(["migrate"])
    capsys.readouterr()
    _stdin(monkeypatch, "!!!not-base64!!!")
    rc = main(["secrets", "rotate-master", "--from-stdin"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "not valid base64" in err


def test_no_subcommand_prints_usage(
    cli_env: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``hm secrets`` with no subcommand exits 2 and prints usage."""
    main(["migrate"])
    capsys.readouterr()
    rc = main(["secrets"])
    err = capsys.readouterr().err
    assert rc == 2  # noqa: PLR2004
    assert "usage" in err.lower()


def test_master_key_unset_errors(
    db_url_env: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If ENV_VAR and the file are both missing, secrets commands exit 1."""
    monkeypatch.delenv(ENV_VAR, raising=False)
    # Force load_master_key to look at a guaranteed-missing path. We monkeypatch
    # the constant DEFAULT_KEY_FILE so the loader doesn't accidentally find a
    # real /run/secrets/master-key on the dev host.
    monkeypatch.setattr(
        "homelab_monitor.kernel.secrets.master_key.DEFAULT_KEY_FILE",
        "/nonexistent/path/master-key",
    )
    main(["migrate"])
    capsys.readouterr()
    _stdin(monkeypatch, "x")
    rc = main(["secrets", "set", "tok", "--from-stdin"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "no master key" in err


def test_get_corrupted_secret_errors(
    cli_env: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the DB row is corrupted, ``get`` exits 1 with an integrity error."""
    main(["migrate"])
    capsys.readouterr()
    _stdin(monkeypatch, "v")
    main(["secrets", "set", "tok", "--from-stdin"])
    capsys.readouterr()

    # Corrupt the row directly using a sync sqlite connection — bypass the
    # async repo to simulate disk-level tampering.
    parsed = urlparse(cli_env.replace("sqlite+aiosqlite", "sqlite"))
    db_file = parsed.path
    conn = sqlite3.connect(db_file)
    try:
        conn.execute(
            "UPDATE secrets SET ciphertext = ? WHERE name = ?",
            ("AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA", "tok"),
        )
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setenv(REVEAL_ENV, "1")
    rc = main(["secrets", "get", "tok"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "tag verification" in err or "ciphertext" in err


def test_list_master_key_error(
    db_url_env: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``hm secrets list`` reports MasterKeyError when no master key configured."""
    monkeypatch.delenv(ENV_VAR, raising=False)
    monkeypatch.setattr(
        "homelab_monitor.kernel.secrets.master_key.DEFAULT_KEY_FILE",
        "/nonexistent/path/master-key",
    )
    main(["migrate"])
    capsys.readouterr()
    rc = main(["secrets", "list"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "no master key" in err


def test_get_master_key_error(
    db_url_env: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``hm secrets get`` reports MasterKeyError when no master key configured."""
    monkeypatch.delenv(ENV_VAR, raising=False)
    monkeypatch.setattr(
        "homelab_monitor.kernel.secrets.master_key.DEFAULT_KEY_FILE",
        "/nonexistent/path/master-key",
    )
    main(["migrate"])
    capsys.readouterr()
    monkeypatch.setenv(REVEAL_ENV, "1")
    rc = main(["secrets", "get", "anything"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "no master key" in err


def test_rotate_master_key_error(
    db_url_env: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``hm secrets rotate`` reports MasterKeyError when no master key configured."""
    monkeypatch.delenv(ENV_VAR, raising=False)
    monkeypatch.setattr(
        "homelab_monitor.kernel.secrets.master_key.DEFAULT_KEY_FILE",
        "/nonexistent/path/master-key",
    )
    main(["migrate"])
    capsys.readouterr()
    _stdin(monkeypatch, "value")
    rc = main(["secrets", "rotate", "anything", "--from-stdin"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "no master key" in err


def test_delete_master_key_error(
    db_url_env: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``hm secrets delete`` reports MasterKeyError when no master key configured."""
    monkeypatch.delenv(ENV_VAR, raising=False)
    monkeypatch.setattr(
        "homelab_monitor.kernel.secrets.master_key.DEFAULT_KEY_FILE",
        "/nonexistent/path/master-key",
    )
    main(["migrate"])
    capsys.readouterr()
    rc = main(["secrets", "delete", "anything"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "no master key" in err


def test_rotate_master_master_key_error(
    db_url_env: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``hm secrets rotate-master`` reports MasterKeyError after stdin parses but before repo build.

    Stdin is valid base64 (parses fine), but no master key is configured for the repo build,
    so the error fires from `_build_repo()` inside the second try block.
    """
    # Set a valid base64 master key for stdin, but unset it from env so _build_repo fails.
    valid_b64 = base64.b64encode(bytes(range(32))).decode("ascii")
    monkeypatch.delenv(ENV_VAR, raising=False)
    monkeypatch.setattr(
        "homelab_monitor.kernel.secrets.master_key.DEFAULT_KEY_FILE",
        "/nonexistent/path/master-key",
    )
    main(["migrate"])
    capsys.readouterr()
    _stdin(monkeypatch, valid_b64)
    rc = main(["secrets", "rotate-master", "--from-stdin"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "no master key" in err


def test_rotate_master_integrity_error_on_corrupted_secret(
    cli_env: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``hm secrets rotate-master`` reports SecretIntegrityError if any row fails to decrypt."""
    main(["migrate"])
    capsys.readouterr()
    # Set a secret first.
    _stdin(monkeypatch, "value")
    main(["secrets", "set", "tok", "--from-stdin"])
    capsys.readouterr()

    # Corrupt the row directly via sync sqlite3 (same pattern as test_get_corrupted_secret_errors).
    parsed = urlparse(cli_env.replace("sqlite+aiosqlite", "sqlite"))
    db_file = parsed.path
    conn = sqlite3.connect(db_file)
    try:
        conn.execute(
            "UPDATE secrets SET ciphertext = ? WHERE name = ?",
            ("AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA", "tok"),
        )
        conn.commit()
    finally:
        conn.close()

    # Attempt rotate-master — should fail with SecretIntegrityError when decrypting tok.
    _stdin(monkeypatch, NEW_KEY_B64)
    rc = main(["secrets", "rotate-master", "--from-stdin"])
    err = capsys.readouterr().err
    assert rc == 1
    # SecretIntegrityError message will contain something about tag verification or decrypt.
    # Don't assert exact wording — just that an error was reported.
    assert "error:" in err.lower() or "integrity" in err.lower()


def test_rotate_strips_trailing_newline(
    cli_env: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rotate via stdin pipe handles trailing newline like set does."""
    main(["migrate"])
    capsys.readouterr()
    _stdin(monkeypatch, "old-val")
    main(["secrets", "set", "tok", "--from-stdin"])
    _stdin(monkeypatch, "new-val-with-newline\n")
    rc = main(["secrets", "rotate", "tok", "--from-stdin"])
    capsys.readouterr()
    assert rc == 0

    monkeypatch.setenv(REVEAL_ENV, "1")
    main(["secrets", "get", "tok"])
    out = capsys.readouterr().out.strip()
    assert out == "new-val-with-newline"


def test_set_strips_crlf(
    cli_env: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stdin pipes from CRLF sources have a trailing \\r\\n that must be stripped."""
    main(["migrate"])
    capsys.readouterr()
    _stdin(monkeypatch, "value-with-crlf\r\n")
    assert main(["secrets", "set", "tok", "--from-stdin"]) == 0
    capsys.readouterr()

    monkeypatch.setenv(REVEAL_ENV, "1")
    main(["secrets", "get", "tok"])
    out = capsys.readouterr().out.strip()
    assert out == "value-with-crlf"
