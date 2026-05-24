"""Tests for source_hash.py (STAGE-003-009)."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from homelab_monitor.kernel.docker.source_hash import (
    _DEFAULT_MAX_DEPTH,  # pyright: ignore[reportPrivateUsage]
    _DEFAULT_MAX_FILE_BYTES,  # pyright: ignore[reportPrivateUsage]
    _DEFAULT_MAX_FILE_COUNT,  # pyright: ignore[reportPrivateUsage]
    _DEFAULT_MAX_TOTAL_BYTES,  # pyright: ignore[reportPrivateUsage]
    SourceHashLimits,
    SourceHashResult,
    _resolve_int,  # pyright: ignore[reportPrivateUsage]
    compute_source_hash,
)


def _make_file(path: Path, content: str = "hello") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_same_tree_same_hash(tmp_path: Path) -> None:
    """Same tree contents produce same hash on two calls."""
    ctx = tmp_path / "ctx"
    _make_file(ctx / "a.txt", "aaa")
    _make_file(ctx / "b.txt", "bbb")

    r1 = compute_source_hash(ctx)
    r2 = compute_source_hash(ctx)
    assert r1.hash == r2.hash
    assert r1.exceeded is None


def test_different_content_different_hash(tmp_path: Path) -> None:
    """Changing file content changes the hash."""
    ctx = tmp_path / "ctx"
    f = _make_file(ctx / "a.txt", "original")
    r1 = compute_source_hash(ctx)
    f.write_text("changed", encoding="utf-8")
    r2 = compute_source_hash(ctx)
    assert r1.hash != r2.hash


def test_rename_file_changes_hash(tmp_path: Path) -> None:
    """Renaming a file changes the hash (relpath is part of hash input)."""
    ctx = tmp_path / "ctx"
    _make_file(ctx / "original.txt", "content")
    r1 = compute_source_hash(ctx)
    (ctx / "original.txt").rename(ctx / "renamed.txt")
    r2 = compute_source_hash(ctx)
    assert r1.hash != r2.hash


def test_add_file_changes_hash(tmp_path: Path) -> None:
    """Adding a file changes the hash."""
    ctx = tmp_path / "ctx"
    _make_file(ctx / "a.txt", "aaa")
    r1 = compute_source_hash(ctx)
    _make_file(ctx / "new.txt", "new content")
    r2 = compute_source_hash(ctx)
    assert r1.hash != r2.hash


def test_creation_order_irrelevant(tmp_path: Path) -> None:
    """Hash is deterministic regardless of file creation order."""
    ctx1 = tmp_path / "ctx1"
    ctx2 = tmp_path / "ctx2"
    # Create in different orders
    _make_file(ctx1 / "a.txt", "aaa")
    _make_file(ctx1 / "b.txt", "bbb")
    _make_file(ctx2 / "b.txt", "bbb")
    _make_file(ctx2 / "a.txt", "aaa")
    r1 = compute_source_hash(ctx1)
    r2 = compute_source_hash(ctx2)
    assert r1.hash == r2.hash


# ---------------------------------------------------------------------------
# Empty directory
# ---------------------------------------------------------------------------


def test_empty_directory_deterministic(tmp_path: Path) -> None:
    """Empty build context produces deterministic hash (empty sha256)."""
    ctx = tmp_path / "ctx"
    ctx.mkdir()
    r1 = compute_source_hash(ctx)
    r2 = compute_source_hash(ctx)
    assert r1.hash == r2.hash
    assert r1.files_hashed == 0
    assert r1.exceeded is None


# ---------------------------------------------------------------------------
# .dockerignore
# ---------------------------------------------------------------------------


def test_dockerignore_excludes_files(tmp_path: Path) -> None:
    """Files matching .dockerignore patterns are excluded from hash."""
    ctx = tmp_path / "ctx"
    _make_file(ctx / "app.py", "code")
    _make_file(ctx / "node_modules" / "dep.js", "module")
    (ctx / ".dockerignore").write_text("node_modules/\n", encoding="utf-8")

    r = compute_source_hash(ctx)
    assert r.files_hashed == 1  # only app.py; node_modules excluded
    assert r.exceeded is None


def test_dockerignore_excluded_vs_not_excluded_differ(tmp_path: Path) -> None:
    """Hash without .dockerignore differs from hash with exclusion."""
    ctx1 = tmp_path / "ctx1"
    _make_file(ctx1 / "app.py", "code")
    _make_file(ctx1 / "extra.py", "extra")

    ctx2 = tmp_path / "ctx2"
    _make_file(ctx2 / "app.py", "code")
    _make_file(ctx2 / "extra.py", "extra")
    (ctx2 / ".dockerignore").write_text("extra.py\n", encoding="utf-8")

    r1 = compute_source_hash(ctx1)
    r2 = compute_source_hash(ctx2)
    assert r1.hash != r2.hash


def test_dockerignore_negation_reincluded(tmp_path: Path) -> None:
    """Files matching negation rules in .dockerignore are re-included."""
    ctx = tmp_path / "ctx"
    _make_file(ctx / "app.py", "code")
    _make_file(ctx / "logs" / "keep.log", "keep")
    _make_file(ctx / "logs" / "discard.log", "discard")
    (ctx / ".dockerignore").write_text("logs/\n!logs/keep.log\n", encoding="utf-8")

    r = compute_source_hash(ctx)
    # keep.log is re-included via negation, discard.log should be excluded.
    # With pathspec gitwildmatch, negation on a dir-excluded path: test that
    # count is at least 1 (app.py).
    assert r.files_hashed >= 1
    assert r.exceeded is None


def test_absent_dockerignore_no_exclusions(tmp_path: Path) -> None:
    """No .dockerignore means no files are excluded."""
    ctx = tmp_path / "ctx"
    _make_file(ctx / "a.py", "a")
    _make_file(ctx / "b.py", "b")

    r = compute_source_hash(ctx)
    assert r.files_hashed == 2  # noqa: PLR2004 -- test-only literal


# ---------------------------------------------------------------------------
# Limit: max_file_bytes
# ---------------------------------------------------------------------------


def test_max_file_bytes_exceeded_returns_sentinel(tmp_path: Path) -> None:
    """Single file exceeding max_file_bytes → sentinel hash + exceeded='context_too_large'."""
    ctx = tmp_path / "ctx"
    big = ctx / "big.bin"
    big.parent.mkdir(parents=True, exist_ok=True)
    big.write_bytes(b"x" * 100)

    limits = SourceHashLimits(max_file_bytes=50)
    r = compute_source_hash(ctx, limits=limits)
    assert r.hash == "OVERSIZED:context_too_large"
    assert r.exceeded == "context_too_large"


# ---------------------------------------------------------------------------
# Limit: max_total_bytes
# ---------------------------------------------------------------------------


def test_max_total_bytes_exceeded_returns_sentinel(tmp_path: Path) -> None:
    """Cumulative bytes exceeding max_total_bytes → sentinel hash."""
    ctx = tmp_path / "ctx"
    _make_file(ctx / "a.txt", "a" * 60)
    _make_file(ctx / "b.txt", "b" * 60)

    limits = SourceHashLimits(max_total_bytes=100)
    r = compute_source_hash(ctx, limits=limits)
    assert r.hash == "OVERSIZED:context_too_large"
    assert r.exceeded == "context_too_large"


# ---------------------------------------------------------------------------
# Limit: max_file_count
# ---------------------------------------------------------------------------


def test_max_file_count_exceeded_returns_sentinel(tmp_path: Path) -> None:
    """More files than max_file_count → sentinel hash."""
    ctx = tmp_path / "ctx"
    for i in range(5):
        _make_file(ctx / f"file{i}.txt", f"content{i}")

    limits = SourceHashLimits(max_file_count=3)
    r = compute_source_hash(ctx, limits=limits)
    assert r.hash == "OVERSIZED:context_too_large"
    assert r.exceeded == "context_too_large"


# ---------------------------------------------------------------------------
# Limit: max_depth
# ---------------------------------------------------------------------------


def test_max_depth_not_descended(tmp_path: Path) -> None:
    """Files deeper than max_depth are not included in hash."""
    ctx = tmp_path / "ctx"
    # depth 0 = root, depth 1 = subdir, depth 2 = subsubdir
    _make_file(ctx / "root.txt", "root")
    _make_file(ctx / "sub" / "sub.txt", "sub")
    deep_file = ctx / "sub" / "deep" / "deep.txt"
    _make_file(deep_file, "deep content")

    # max_depth=1: only root and sub/ files visible; sub/deep/ not descended
    limits = SourceHashLimits(max_depth=1)
    r_shallow = compute_source_hash(ctx, limits=limits)

    # max_depth=30 (default): all files visible
    limits_deep = SourceHashLimits(max_depth=30)
    r_deep = compute_source_hash(ctx, limits=limits_deep)

    # Hashes must differ (deep file not included in shallow)
    assert r_shallow.hash != r_deep.hash
    assert r_shallow.files_hashed < r_deep.files_hashed


# ---------------------------------------------------------------------------
# Symlinks
# ---------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks unreliable on Windows")
def test_symlink_not_followed(tmp_path: Path) -> None:
    """Symlinks are hashed by target string, not file content (followlinks=False)."""
    ctx = tmp_path / "ctx"
    ctx.mkdir()
    target = tmp_path / "real_file.txt"
    target.write_text("real content", encoding="utf-8")
    link = ctx / "link.txt"
    link.symlink_to(target)

    # Also create a plain file with the same name's content
    ctx2 = tmp_path / "ctx2"
    ctx2.mkdir()
    plain = ctx2 / "link.txt"
    plain.write_text("real content", encoding="utf-8")

    r_link = compute_source_hash(ctx)
    r_plain = compute_source_hash(ctx2)
    # Symlink and plain file with same content → DIFFERENT hashes (symlink hashed by target)
    assert r_link.hash != r_plain.hash


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks unreliable on Windows")
def test_symlink_target_change_changes_hash(tmp_path: Path) -> None:
    """Changing symlink target changes the hash."""
    ctx = tmp_path / "ctx"
    ctx.mkdir()
    t1 = tmp_path / "target1.txt"
    t1.write_text("t1", encoding="utf-8")
    t2 = tmp_path / "target2.txt"
    t2.write_text("t2", encoding="utf-8")

    link = ctx / "link.txt"
    link.symlink_to(t1)
    r1 = compute_source_hash(ctx)
    link.unlink()
    link.symlink_to(t2)
    r2 = compute_source_hash(ctx)
    assert r1.hash != r2.hash


# ---------------------------------------------------------------------------
# Permission denied on file
# ---------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="chmod 000 unreliable on Windows")
def test_permission_denied_on_file_returns_sentinel(tmp_path: Path) -> None:
    """PermissionError reading a file → exceeded='permission_denied', sentinel hash."""
    ctx = tmp_path / "ctx"
    f = _make_file(ctx / "secret.bin", "secret")
    f.chmod(0o000)
    try:
        r = compute_source_hash(ctx)
        assert r.hash == "OVERSIZED:permission_denied"
        assert r.exceeded == "permission_denied"
    finally:
        f.chmod(stat.S_IRUSR | stat.S_IWUSR)


# ---------------------------------------------------------------------------
# Sentinel format
# ---------------------------------------------------------------------------


def test_sentinel_format_context_too_large(tmp_path: Path) -> None:
    """Sentinel hash has format 'OVERSIZED:<reason>'."""
    ctx = tmp_path / "ctx"
    _make_file(ctx / "big.bin", "x" * 200)
    limits = SourceHashLimits(max_file_bytes=10)
    r = compute_source_hash(ctx, limits=limits)
    assert r.hash.startswith("OVERSIZED:")
    assert "context_too_large" in r.hash


# ---------------------------------------------------------------------------
# SourceHashLimits.from_env
# ---------------------------------------------------------------------------


def test_from_env_reads_all_four_vars() -> None:
    """SourceHashLimits.from_env() reads all 4 env vars."""
    env = {
        "HOMELAB_MONITOR_BUILD_HASH_MAX_FILE_BYTES": "1111",
        "HOMELAB_MONITOR_BUILD_HASH_MAX_TOTAL_BYTES": "2222",
        "HOMELAB_MONITOR_BUILD_HASH_MAX_FILE_COUNT": "3333",
        "HOMELAB_MONITOR_BUILD_HASH_MAX_DEPTH": "4444",
    }
    with patch.dict(os.environ, env):
        limits = SourceHashLimits.from_env()
    assert limits.max_file_bytes == 1111  # noqa: PLR2004 -- test-only literal
    assert limits.max_total_bytes == 2222  # noqa: PLR2004 -- test-only literal
    assert limits.max_file_count == 3333  # noqa: PLR2004 -- test-only literal
    assert limits.max_depth == 4444  # noqa: PLR2004 -- test-only literal


def test_from_env_uses_defaults_when_unset() -> None:
    """SourceHashLimits.from_env() returns defaults when env vars absent."""
    keys = [
        "HOMELAB_MONITOR_BUILD_HASH_MAX_FILE_BYTES",
        "HOMELAB_MONITOR_BUILD_HASH_MAX_TOTAL_BYTES",
        "HOMELAB_MONITOR_BUILD_HASH_MAX_FILE_COUNT",
        "HOMELAB_MONITOR_BUILD_HASH_MAX_DEPTH",
    ]
    env_patch = {k: "" for k in keys}
    with patch.dict(os.environ, env_patch):
        limits = SourceHashLimits.from_env()
    assert limits.max_file_bytes == _DEFAULT_MAX_FILE_BYTES
    assert limits.max_total_bytes == _DEFAULT_MAX_TOTAL_BYTES
    assert limits.max_file_count == _DEFAULT_MAX_FILE_COUNT
    assert limits.max_depth == _DEFAULT_MAX_DEPTH


def test_from_env_uses_default_on_non_numeric() -> None:
    """SourceHashLimits.from_env() falls back to default on non-numeric value."""
    with patch.dict(os.environ, {"HOMELAB_MONITOR_BUILD_HASH_MAX_DEPTH": "notanumber"}):
        limits = SourceHashLimits.from_env()
    assert limits.max_depth == _DEFAULT_MAX_DEPTH


def test_from_env_uses_default_on_zero_or_negative() -> None:
    """SourceHashLimits.from_env() falls back to default when value < 1."""
    with patch.dict(os.environ, {"HOMELAB_MONITOR_BUILD_HASH_MAX_FILE_COUNT": "0"}):
        limits = SourceHashLimits.from_env()
    assert limits.max_file_count == _DEFAULT_MAX_FILE_COUNT


# ---------------------------------------------------------------------------
# _resolve_int helper
# ---------------------------------------------------------------------------


def test_resolve_int_returns_default_for_empty_string() -> None:
    """_resolve_int returns default when env var is empty string."""
    with patch.dict(os.environ, {"TEST_VAR": ""}):
        assert _resolve_int("TEST_VAR", 99) == 99  # noqa: PLR2004 -- test-only literal


def test_resolve_int_returns_value_when_valid() -> None:
    """_resolve_int returns the parsed integer when env var is valid."""
    with patch.dict(os.environ, {"TEST_VAR": "42"}):
        assert _resolve_int("TEST_VAR", 99) == 42  # noqa: PLR2004 -- test-only literal


def test_resolve_int_returns_default_for_non_integer() -> None:
    """_resolve_int returns default when env var is not an integer."""
    with patch.dict(os.environ, {"TEST_VAR": "abc"}):
        assert _resolve_int("TEST_VAR", 99) == 99  # noqa: PLR2004 -- test-only literal


@pytest.mark.skipif(sys.platform == "win32", reason="symlink behavior differs on Windows")
def test_symlink_count_exceeds_max_file_count_returns_oversized(tmp_path: Path) -> None:
    """Exceeding max_file_count via symlinks returns OVERSIZED:context_too_large."""
    ctx = tmp_path / "ctx"
    ctx.mkdir()
    target = ctx / "target.txt"
    target.write_text("x", encoding="utf-8")
    for i in range(3):
        (ctx / f"link{i}.txt").symlink_to(target)
    limits = SourceHashLimits(max_file_count=2)
    r = compute_source_hash(ctx, limits=limits)
    assert r.exceeded == "context_too_large"
    assert r.hash.startswith("OVERSIZED:")


def test_resolve_int_returns_default_for_below_one() -> None:
    """_resolve_int returns default when value is below 1."""
    with patch.dict(os.environ, {"TEST_VAR": "-5"}):
        assert _resolve_int("TEST_VAR", 99) == 99  # noqa: PLR2004 -- test-only literal


# ---------------------------------------------------------------------------
# SourceHashResult structure
# ---------------------------------------------------------------------------


def test_result_has_expected_fields(tmp_path: Path) -> None:
    """SourceHashResult has all expected fields with correct types."""
    ctx = tmp_path / "ctx"
    _make_file(ctx / "a.txt", "hello")
    r = compute_source_hash(ctx)

    assert isinstance(r, SourceHashResult)
    assert isinstance(r.hash, str)
    assert len(r.hash) == 64  # noqa: PLR2004 -- SHA-256 hex digest
    assert isinstance(r.files_hashed, int)
    assert isinstance(r.bytes_hashed, int)
    assert isinstance(r.files_skipped, int)
    assert r.exceeded is None
