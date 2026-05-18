"""Tests for scripts/hm-crontab-snapshot.sh (STAGE-002-009).

Each test builds a temporary tree, writes a fake `crontab` script (via
HM_CRONTAB_BIN), runs the snapshot script via subprocess (with
HM_CRON_SNAPSHOT_TEST_ROOT pointed at a tmp tree), then asserts the snapshot
directory state. Mirrors the pattern from tests/scripts/test_hm_cron_apply.py.

NOT counted toward kernel coverage (this is a bash-script harness).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_SCRIPT = Path(__file__).parents[4] / "scripts" / "hm-crontab-snapshot.sh"
assert _SCRIPT.exists(), f"snapshot script not found at {_SCRIPT}"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_tree(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    """Create a minimal fake filesystem root.

    Returns (root, spool_dir, snapshot_dir, fakebin_dir).
    The snapshot dir and fakebin dir are created; the script creates
    snapshot_dir via `mkdir -p` so we also pre-create it here to make
    assertions simpler (tests that want it absent can rmdir it).
    """
    root = tmp_path / "root"
    spool = root / "var" / "spool" / "cron" / "crontabs"
    spool.mkdir(parents=True)
    snap = root / "var" / "lib" / "homelab-monitor" / "crontab-snapshot"
    snap.mkdir(parents=True)
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    return root, spool, snap, fakebin


def _write_fake_crontab(fakebin: Path, fake_crontab_dir: Path) -> Path:
    """Write a fake `crontab` script to fakebin/.

    The fake script reads -l -u <user> args and emits the content of
    $FAKE_CRONTAB_DIR/<user> if it exists; else exits 1.
    """
    fake_bin = fakebin / "crontab"
    fake_bin.write_text(
        "#!/usr/bin/env bash\n"
        "# Fake `crontab` for the snapshot-script harness.\n"
        "set -eu\n"
        'user=""\n'
        "while [[ $# -gt 0 ]]; do\n"
        '    case "$1" in\n'
        "        -l) shift ;;\n"
        '        -u) user="$2"; shift 2 ;;\n'
        "        *)  shift ;;\n"
        "    esac\n"
        "done\n"
        'f="$FAKE_CRONTAB_DIR/$user"\n'
        'if [[ -f "$f" ]]; then cat "$f"; exit 0; fi\n'
        'echo "no crontab for $user" >&2\n'
        "exit 1\n",
        encoding="utf-8",
    )
    fake_bin.chmod(0o755)
    fake_crontab_dir.mkdir(parents=True, exist_ok=True)
    return fake_bin


def _run_script(
    root: Path,
    fake_crontab_bin: Path,
    fake_crontab_dir: Path,
    *,
    extra_env: dict[str, str] | None = None,
    timeout: int = 15,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["HM_CRON_SNAPSHOT_TEST_ROOT"] = str(root)
    env["HM_CRONTAB_BIN"] = str(fake_crontab_bin)
    env["FAKE_CRONTAB_DIR"] = str(fake_crontab_dir)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(_SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_snapshot_writes_per_user_file(tmp_path: Path) -> None:
    """Spool has alice + bob; fake-crontabs has both → snapshot has both files."""
    root, spool, snap, fakebin = _make_tree(tmp_path)
    fake_crontab_dir = tmp_path / "fake-crontabs"
    fake_bin = _write_fake_crontab(fakebin, fake_crontab_dir)

    # Create spool files (names only matter — script enumerates them)
    (spool / "alice").write_text("alice-spool", encoding="utf-8")
    (spool / "bob").write_text("bob-spool", encoding="utf-8")

    # Create fake crontab content for each user
    (fake_crontab_dir / "alice").write_text("*/5 * * * * /opt/alice/sync.sh\n", encoding="utf-8")
    (fake_crontab_dir / "bob").write_text("0 3 * * * /opt/bob/backup.sh\n", encoding="utf-8")

    proc = _run_script(root, fake_bin, fake_crontab_dir)
    assert proc.returncode == 0, proc.stderr

    assert (snap / "alice").exists()
    assert (snap / "bob").exists()
    # Mode must be 0644
    assert oct((snap / "alice").stat().st_mode & 0o777) == oct(0o644)
    assert oct((snap / "bob").stat().st_mode & 0o777) == oct(0o644)


@pytest.mark.slow
def test_snapshot_content_verbatim(tmp_path: Path) -> None:
    """Snapshot file content equals the fake crontab output verbatim."""
    root, spool, snap, fakebin = _make_tree(tmp_path)
    fake_crontab_dir = tmp_path / "fake-crontabs"
    fake_bin = _write_fake_crontab(fakebin, fake_crontab_dir)

    raw_content = "17 * * * * /opt/x.sh\n"
    (spool / "alice").write_text("alice-spool", encoding="utf-8")
    (fake_crontab_dir / "alice").write_text(raw_content, encoding="utf-8")

    proc = _run_script(root, fake_bin, fake_crontab_dir)
    assert proc.returncode == 0, proc.stderr

    # The script wraps output with `printf '%s\n'`, so content ends with newline.
    written = (snap / "alice").read_text(encoding="utf-8")
    # Content should be the raw crontab lines (possibly with one trailing newline).
    assert "/opt/x.sh" in written
    assert "17 * * * *" in written


@pytest.mark.slow
def test_snapshot_skips_user_with_no_crontab(tmp_path: Path) -> None:
    """Spool has alice but crontab -l exits 1 → no snapshot file, script exits 0."""
    root, spool, snap, fakebin = _make_tree(tmp_path)
    fake_crontab_dir = tmp_path / "fake-crontabs"
    fake_bin = _write_fake_crontab(fakebin, fake_crontab_dir)

    (spool / "alice").write_text("alice-spool", encoding="utf-8")
    # No fake-crontab file for alice → crontab -l exits 1

    proc = _run_script(root, fake_bin, fake_crontab_dir)
    assert proc.returncode == 0, proc.stderr
    assert not (snap / "alice").exists()


@pytest.mark.slow
def test_snapshot_skips_empty_crontab(tmp_path: Path) -> None:
    """Fake crontab returns empty output → no snapshot file written."""
    root, spool, snap, fakebin = _make_tree(tmp_path)
    fake_crontab_dir = tmp_path / "fake-crontabs"
    fake_bin = _write_fake_crontab(fakebin, fake_crontab_dir)

    (spool / "alice").write_text("alice-spool", encoding="utf-8")
    (fake_crontab_dir / "alice").write_text("", encoding="utf-8")  # empty

    proc = _run_script(root, fake_bin, fake_crontab_dir)
    assert proc.returncode == 0, proc.stderr
    assert not (snap / "alice").exists()


@pytest.mark.slow
def test_snapshot_prunes_stale_file(tmp_path: Path) -> None:
    """Pre-existing ghost snapshot file pruned when user no longer in spool."""
    root, spool, snap, fakebin = _make_tree(tmp_path)
    fake_crontab_dir = tmp_path / "fake-crontabs"
    fake_bin = _write_fake_crontab(fakebin, fake_crontab_dir)

    # ghost: in snapshot but NOT in spool
    (snap / "ghost").write_text("stale content\n", encoding="utf-8")

    # alice: in spool + has a crontab
    (spool / "alice").write_text("alice-spool", encoding="utf-8")
    (fake_crontab_dir / "alice").write_text("*/5 * * * * /opt/alice/job.sh\n", encoding="utf-8")

    proc = _run_script(root, fake_bin, fake_crontab_dir)
    assert proc.returncode == 0, proc.stderr

    assert not (snap / "ghost").exists(), "stale snapshot file must be pruned"
    assert (snap / "alice").exists()


@pytest.mark.slow
def test_snapshot_skips_dotfiles_in_spool(tmp_path: Path) -> None:
    """Dotfiles in spool dir are skipped; only real user files snapshotted."""
    root, spool, snap, fakebin = _make_tree(tmp_path)
    fake_crontab_dir = tmp_path / "fake-crontabs"
    fake_bin = _write_fake_crontab(fakebin, fake_crontab_dir)

    (spool / ".hidden").write_text("dot-spool", encoding="utf-8")
    (spool / "alice").write_text("alice-spool", encoding="utf-8")
    (fake_crontab_dir / "alice").write_text("*/5 * * * * /opt/alice/job.sh\n", encoding="utf-8")

    proc = _run_script(root, fake_bin, fake_crontab_dir)
    assert proc.returncode == 0, proc.stderr

    assert not (snap / ".hidden").exists()
    assert (snap / "alice").exists()


@pytest.mark.slow
def test_snapshot_skips_subdirs_in_spool(tmp_path: Path) -> None:
    """Subdirectories in spool dir are skipped (only regular files enumerate users)."""
    root, spool, snap, fakebin = _make_tree(tmp_path)
    fake_crontab_dir = tmp_path / "fake-crontabs"
    fake_bin = _write_fake_crontab(fakebin, fake_crontab_dir)

    (spool / "subdir").mkdir()
    (spool / "alice").write_text("alice-spool", encoding="utf-8")
    (fake_crontab_dir / "alice").write_text("*/5 * * * * /opt/alice/job.sh\n", encoding="utf-8")

    proc = _run_script(root, fake_bin, fake_crontab_dir)
    assert proc.returncode == 0, proc.stderr

    assert not (snap / "subdir").exists()
    assert (snap / "alice").exists()


@pytest.mark.slow
def test_snapshot_missing_spool_dir_exits_zero(tmp_path: Path) -> None:
    """No spool dir at all → script exits 0 with WARN, no crash."""
    root = tmp_path / "root"
    root.mkdir()
    # Deliberately do NOT create spool dir
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    fake_crontab_dir = tmp_path / "fake-crontabs"
    fake_bin = _write_fake_crontab(fakebin, fake_crontab_dir)

    proc = _run_script(root, fake_bin, fake_crontab_dir)
    assert proc.returncode == 0, proc.stderr
    assert "WARN" in proc.stdout


@pytest.mark.slow
def test_snapshot_missing_crontab_bin_exits_zero(tmp_path: Path) -> None:
    """HM_CRONTAB_BIN points to nonexistent binary → script exits 0 with WARN."""
    root, _spool, _snap, _fakebin = _make_tree(tmp_path)
    fake_crontab_dir = tmp_path / "fake-crontabs"
    fake_crontab_dir.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env["HM_CRON_SNAPSHOT_TEST_ROOT"] = str(root)
    env["HM_CRONTAB_BIN"] = "/nonexistent/crontab"
    env["FAKE_CRONTAB_DIR"] = str(fake_crontab_dir)

    proc = subprocess.run(
        ["bash", str(_SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "WARN" in proc.stdout


@pytest.mark.slow
def test_snapshot_atomic_no_temp_left(tmp_path: Path) -> None:
    """After a normal run no .<user>.tmp.* temp files remain in the snapshot dir."""
    root, spool, snap, fakebin = _make_tree(tmp_path)
    fake_crontab_dir = tmp_path / "fake-crontabs"
    fake_bin = _write_fake_crontab(fakebin, fake_crontab_dir)

    (spool / "alice").write_text("alice-spool", encoding="utf-8")
    (fake_crontab_dir / "alice").write_text("*/5 * * * * /opt/alice/job.sh\n", encoding="utf-8")

    proc = _run_script(root, fake_bin, fake_crontab_dir)
    assert proc.returncode == 0, proc.stderr

    temp_files = list(snap.glob(".*.tmp.*"))
    assert temp_files == [], f"stray temp files found: {temp_files}"


@pytest.mark.slow
def test_snapshot_sweeps_stray_temp(tmp_path: Path) -> None:
    """Pre-existing stray temp file from an interrupted run is cleaned up."""
    root, spool, snap, fakebin = _make_tree(tmp_path)
    fake_crontab_dir = tmp_path / "fake-crontabs"
    fake_bin = _write_fake_crontab(fakebin, fake_crontab_dir)

    # Plant a stale temp file
    stray = snap / ".alice.tmp.OLD"
    stray.write_text("partial write\n", encoding="utf-8")

    (spool / "alice").write_text("alice-spool", encoding="utf-8")
    (fake_crontab_dir / "alice").write_text("*/5 * * * * /opt/alice/job.sh\n", encoding="utf-8")

    proc = _run_script(root, fake_bin, fake_crontab_dir)
    assert proc.returncode == 0, proc.stderr

    assert not stray.exists(), "stray temp file must be swept on next run"


@pytest.mark.slow
def test_snapshot_overwrites_existing(tmp_path: Path) -> None:
    """Pre-existing snapshot file is overwritten with new content atomically."""
    root, spool, snap, fakebin = _make_tree(tmp_path)
    fake_crontab_dir = tmp_path / "fake-crontabs"
    fake_bin = _write_fake_crontab(fakebin, fake_crontab_dir)

    old_content = "0 1 * * * /opt/old.sh\n"
    new_content = "17 * * * * /opt/new.sh\n"

    # Pre-create snapshot file with old content
    (snap / "alice").write_text(old_content, encoding="utf-8")

    (spool / "alice").write_text("alice-spool", encoding="utf-8")
    (fake_crontab_dir / "alice").write_text(new_content, encoding="utf-8")

    proc = _run_script(root, fake_bin, fake_crontab_dir)
    assert proc.returncode == 0, proc.stderr

    written = (snap / "alice").read_text(encoding="utf-8")
    assert "/opt/new.sh" in written
    assert "/opt/old.sh" not in written
