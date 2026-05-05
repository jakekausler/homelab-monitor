"""Tests for the master-key bootstrap and fingerprint helpers."""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from homelab_monitor.kernel.secrets.errors import MasterKeyError
from homelab_monitor.kernel.secrets.master_key import (
    ENV_VAR,
    EXPECTED_KEY_LEN,
    load_master_key,
    master_key_fingerprint,
)

KEY_A = bytes(range(32))
KEY_B = bytes(range(1, 33))


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def test_load_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Env var path returns the decoded 32-byte key."""
    monkeypatch.setenv(ENV_VAR, _b64(KEY_A))
    assert load_master_key() == KEY_A


def test_load_from_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """File path returns the decoded key when env is unset."""
    monkeypatch.delenv(ENV_VAR, raising=False)
    key_file = tmp_path / "master-key"
    key_file.write_text(_b64(KEY_A), encoding="utf-8")
    assert load_master_key(file_path=str(key_file)) == KEY_A


def test_env_takes_precedence_over_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Env var wins when both env and file are present."""
    monkeypatch.setenv(ENV_VAR, _b64(KEY_A))
    key_file = tmp_path / "master-key"
    key_file.write_text(_b64(KEY_B), encoding="utf-8")
    assert load_master_key(file_path=str(key_file)) == KEY_A


def test_empty_env_falls_through_to_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty / whitespace env var is treated as unset."""
    monkeypatch.setenv(ENV_VAR, "   ")
    key_file = tmp_path / "master-key"
    key_file.write_text(_b64(KEY_A), encoding="utf-8")
    assert load_master_key(file_path=str(key_file)) == KEY_A


def test_no_source_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Neither env nor file → MasterKeyError."""
    monkeypatch.delenv(ENV_VAR, raising=False)
    nonexistent = tmp_path / "missing"
    with pytest.raises(MasterKeyError, match="no master key"):
        load_master_key(file_path=str(nonexistent))


def test_malformed_base64_raises_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-base64 env var raises MasterKeyError."""
    monkeypatch.setenv(ENV_VAR, "!!!not-base64!!!")
    with pytest.raises(MasterKeyError, match="not valid base64"):
        load_master_key()


def test_malformed_base64_raises_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-base64 file content raises MasterKeyError."""
    monkeypatch.delenv(ENV_VAR, raising=False)
    key_file = tmp_path / "bad"
    key_file.write_text("???", encoding="utf-8")
    with pytest.raises(MasterKeyError, match="not valid base64"):
        load_master_key(file_path=str(key_file))


def test_wrong_length_raises_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """A base64 string that decodes to the wrong byte length raises."""
    monkeypatch.setenv(ENV_VAR, _b64(b"\x00" * 16))  # 16 bytes ≠ 32
    with pytest.raises(MasterKeyError, match="not exactly 32 bytes"):
        load_master_key()


def test_wrong_length_raises_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """File path likewise enforces length."""
    monkeypatch.delenv(ENV_VAR, raising=False)
    key_file = tmp_path / "short"
    key_file.write_text(_b64(b"\x00" * 8), encoding="utf-8")
    with pytest.raises(MasterKeyError, match="not exactly 32 bytes"):
        load_master_key(file_path=str(key_file))


def test_fingerprint_stable() -> None:
    """The fingerprint is deterministic for a given key."""
    fp1 = master_key_fingerprint(KEY_A)
    fp2 = master_key_fingerprint(KEY_A)
    assert fp1 == fp2
    assert len(fp1) == 64  # 32-byte HMAC → 64 hex chars  # noqa: PLR2004


def test_fingerprint_different_for_different_keys() -> None:
    """Different keys produce different fingerprints."""
    assert master_key_fingerprint(KEY_A) != master_key_fingerprint(KEY_B)


def test_fingerprint_does_not_reveal_key_bytes() -> None:
    """The fingerprint hex never contains the raw key bytes' hex representation."""
    fp = master_key_fingerprint(KEY_A)
    raw_hex = KEY_A.hex()
    assert raw_hex not in fp


def test_fingerprint_rejects_wrong_length() -> None:
    """Calling with a non-32-byte key raises MasterKeyError."""
    with pytest.raises(MasterKeyError):
        master_key_fingerprint(b"\x00" * 16)
    assert EXPECTED_KEY_LEN == 32  # noqa: PLR2004
