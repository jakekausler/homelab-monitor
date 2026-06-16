"""Tests for the watched-directory collision validator."""

from __future__ import annotations

import pytest

from homelab_monitor.kernel.watched_dirs import (
    WatchedDirError,
    container_name,
    container_target,
    main,
    mount_lines,
    validate,
)

# --------------------------------------------------------------------------- #
# name derivation
# --------------------------------------------------------------------------- #


def test_container_name_derivations() -> None:
    assert container_name("/var") == "var"
    assert container_name("/tmp") == "tmp"
    assert container_name("/var/log") == "var-log"


def test_container_name_rejects_root() -> None:
    with pytest.raises(WatchedDirError):
        container_name("/")


def test_container_target() -> None:
    assert container_target("/var") == "/host-watch/var"
    assert container_target("/var/log") == "/host-watch/var-log"


# --------------------------------------------------------------------------- #
# validate() rules
# --------------------------------------------------------------------------- #


def test_validate_accepts_default_pair() -> None:
    assert validate(["/tmp", "/var"]) == ["/tmp", "/var"]


def test_rule1_duplicate_path() -> None:
    with pytest.raises(WatchedDirError, match="duplicate"):
        validate(["/var", "/var/"])  # normalize -> both /var


def test_root_path_rejected() -> None:
    # '/' has no derivable /host-watch/<name>; rejected at name derivation.
    with pytest.raises(WatchedDirError, match="may not be '/'"):
        validate(["/"])


def test_rule4_target_name_collision() -> None:
    # '/a-b' and '/a/b' both map to /host-watch/a-b.
    with pytest.raises(WatchedDirError, match="both map to"):
        validate(["/a-b", "/a/b"])


def test_validate_requires_absolute_path() -> None:
    with pytest.raises(WatchedDirError, match="must be absolute"):
        validate(["relative/path"])


# --------------------------------------------------------------------------- #
# mount_lines + CLI
# --------------------------------------------------------------------------- #


def test_mount_lines() -> None:
    assert mount_lines(["/var", "/tmp"]) == [
        "/var:/host-watch/var:ro",
        "/tmp:/host-watch/tmp:ro",
    ]


def test_main_empty_input_ok() -> None:
    assert main([]) == 0


def test_main_success(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["/var"])
    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert out == "/var:/host-watch/var:ro"


def test_main_collision_returns_2(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["/"])
    assert rc == 2  # noqa: PLR2004
    err = capsys.readouterr().err
    assert "ERROR" in err
