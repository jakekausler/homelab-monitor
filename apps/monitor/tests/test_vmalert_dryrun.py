"""Tests for vmalert_dryrun module (STAGE-004-043A)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from homelab_monitor.kernel.logs.vmalert_dryrun import (
    run_vmalert_dryrun,
)


class TestDockerAvailable:
    """Tests for _docker_available availability check."""

    def test_docker_cli_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """shutil.which returns None -> skipped=True."""
        from homelab_monitor.kernel.logs import vmalert_dryrun  # noqa: PLC0415

        def _which(name: str) -> str | None:
            return None

        monkeypatch.setattr(vmalert_dryrun.shutil, "which", _which)
        result = run_vmalert_dryrun(
            "rule: test",
            image="test:1",
            timeout_s=1.0,
            mount_source="vol",
            work_dir="/tmp",
        )
        assert result.skipped is True
        assert result.ok is True
        assert result.stderr == ""

    def test_docker_image_inspect_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """docker image inspect returncode!=0 -> skipped=True."""
        from homelab_monitor.kernel.logs import vmalert_dryrun  # noqa: PLC0415

        def _which(name: str) -> str | None:
            return "/usr/bin/docker"

        monkeypatch.setattr(vmalert_dryrun.shutil, "which", _which)

        def mock_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="")

        monkeypatch.setattr(vmalert_dryrun.subprocess, "run", mock_run)
        result = run_vmalert_dryrun(
            "rule: test",
            image="test:1",
            timeout_s=1.0,
            mount_source="vol",
            work_dir="/tmp",
        )
        assert result.skipped is True
        assert result.ok is True

    def test_docker_image_inspect_oserror(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """docker image inspect raises OSError -> skipped=True."""
        from homelab_monitor.kernel.logs import vmalert_dryrun  # noqa: PLC0415

        def _which(name: str) -> str | None:
            return "/usr/bin/docker"

        monkeypatch.setattr(vmalert_dryrun.shutil, "which", _which)

        def _raise_oserror(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
            raise OSError("boom")

        monkeypatch.setattr(vmalert_dryrun.subprocess, "run", _raise_oserror)
        result = run_vmalert_dryrun(
            "rule: test",
            image="test:1",
            timeout_s=1.0,
            mount_source="vol",
            work_dir="/tmp",
        )
        assert result.skipped is True
        assert result.ok is True


class TestMountAndWorkdir:
    """Tests for mount_source / work_dir validation."""

    def test_mount_source_empty(self) -> None:
        """mount_source='' -> skipped=True."""
        result = run_vmalert_dryrun(
            "rule: test",
            image="test:1",
            timeout_s=1.0,
            mount_source="",
            work_dir="/tmp",
        )
        assert result.skipped is True
        assert result.ok is True

    def test_work_dir_empty(self) -> None:
        """work_dir='' -> skipped=True."""
        result = run_vmalert_dryrun(
            "rule: test",
            image="test:1",
            timeout_s=1.0,
            mount_source="vol",
            work_dir="",
        )
        assert result.skipped is True
        assert result.ok is True

    def test_workdir_mkdir_fails(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Path.mkdir raises OSError -> skipped=True."""
        from homelab_monitor.kernel.logs import vmalert_dryrun  # noqa: PLC0415

        def _which(name: str) -> str | None:
            return "/usr/bin/docker"

        monkeypatch.setattr(vmalert_dryrun.shutil, "which", _which)

        def mock_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

        monkeypatch.setattr(vmalert_dryrun.subprocess, "run", mock_run)

        def mock_mkdir(self: Path, *args: object, **kwargs: object) -> None:
            raise OSError("no perms")

        monkeypatch.setattr(Path, "mkdir", mock_mkdir)

        result = run_vmalert_dryrun(
            "rule: test",
            image="test:1",
            timeout_s=1.0,
            mount_source="vol",
            work_dir="/nonexistent",
        )
        assert result.skipped is True
        assert result.ok is True


class TestSubprocessErrors:
    """Tests for subprocess execution errors."""

    def test_subprocess_timeout(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """subprocess.TimeoutExpired -> skipped=True."""
        from homelab_monitor.kernel.logs import vmalert_dryrun  # noqa: PLC0415

        def _which(name: str) -> str | None:
            return "/usr/bin/docker"

        monkeypatch.setattr(vmalert_dryrun.shutil, "which", _which)

        call_count = 0

        def mock_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
            raise subprocess.TimeoutExpired(cmd="docker", timeout=1.0)

        monkeypatch.setattr(vmalert_dryrun.subprocess, "run", mock_run)

        result = run_vmalert_dryrun(
            "rule: test",
            image="test:1",
            timeout_s=1.0,
            mount_source="vol",
            work_dir=str(tmp_path),
        )
        assert result.skipped is True
        assert result.ok is True

    def test_subprocess_oserror(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """subprocess.run raises OSError -> skipped=True."""
        from homelab_monitor.kernel.logs import vmalert_dryrun  # noqa: PLC0415

        def _which(name: str) -> str | None:
            return "/usr/bin/docker"

        monkeypatch.setattr(vmalert_dryrun.shutil, "which", _which)

        call_count = 0

        def mock_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
            raise OSError("docker not executable")

        monkeypatch.setattr(vmalert_dryrun.subprocess, "run", mock_run)

        result = run_vmalert_dryrun(
            "rule: test",
            image="test:1",
            timeout_s=1.0,
            mount_source="vol",
            work_dir=str(tmp_path),
        )
        assert result.skipped is True
        assert result.ok is True


class TestSuccessfulRuns:
    """Tests for successful vmalert runs."""

    def test_exit_0(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """docker run exit 0 -> skipped=False, ok=True."""
        from homelab_monitor.kernel.logs import vmalert_dryrun  # noqa: PLC0415

        def _which(name: str) -> str | None:
            return "/usr/bin/docker"

        monkeypatch.setattr(vmalert_dryrun.shutil, "which", _which)

        def mock_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

        monkeypatch.setattr(vmalert_dryrun.subprocess, "run", mock_run)

        result = run_vmalert_dryrun(
            "rule: test",
            image="test:1",
            timeout_s=1.0,
            mount_source="vol",
            work_dir=str(tmp_path),
        )
        assert result.skipped is False
        assert result.ok is True
        assert result.stderr == ""

    def test_exit_1_with_stderr(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """docker run exit 1 with stderr -> skipped=False, ok=False, stderr captured."""
        from homelab_monitor.kernel.logs import vmalert_dryrun  # noqa: PLC0415

        def _which(name: str) -> str | None:
            return "/usr/bin/docker"

        monkeypatch.setattr(vmalert_dryrun.shutil, "which", _which)

        call_count = 0

        def mock_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
            return subprocess.CompletedProcess(
                args=[], returncode=1, stdout="", stderr="invalid expr: parse error"
            )

        monkeypatch.setattr(vmalert_dryrun.subprocess, "run", mock_run)

        result = run_vmalert_dryrun(
            "rule: test",
            image="test:1",
            timeout_s=1.0,
            mount_source="vol",
            work_dir=str(tmp_path),
        )
        assert result.skipped is False
        assert result.ok is False
        assert result.stderr == "invalid expr: parse error"

    def test_tempfile_cleaned_on_success(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Tempfile is deleted after successful run."""
        from homelab_monitor.kernel.logs import vmalert_dryrun  # noqa: PLC0415

        def _which(name: str) -> str | None:
            return "/usr/bin/docker"

        monkeypatch.setattr(vmalert_dryrun.shutil, "which", _which)

        created_files: list[str] = []

        def mock_run(
            args: list[object], *a: object, **kw: object
        ) -> subprocess.CompletedProcess[str]:
            # Capture the file being run to verify it exists at call time
            for _i, arg in enumerate(args):
                if isinstance(arg, str) and arg.startswith("-rule="):
                    # Extract the path
                    rule_path = arg[6:]  # Strip "-rule="
                    # Convert container path back to host path (strip /dryrun/)
                    if rule_path.startswith("/dryrun/"):
                        host_filename = rule_path[8:]  # Strip "/dryrun/"
                        full_path = str(tmp_path / host_filename)
                        if Path(full_path).exists():
                            created_files.append(full_path)
            return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

        monkeypatch.setattr(vmalert_dryrun.subprocess, "run", mock_run)

        result = run_vmalert_dryrun(
            "rule: test",
            image="test:1",
            timeout_s=1.0,
            mount_source="vol",
            work_dir=str(tmp_path),
        )
        assert result.skipped is False
        assert result.ok is True
        # Verify that any tempfiles created are now gone
        for f in created_files:
            assert not Path(f).exists()

    def test_tempfile_cleaned_on_failure(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Tempfile is deleted even after failed run."""
        from homelab_monitor.kernel.logs import vmalert_dryrun  # noqa: PLC0415

        def _which(name: str) -> str | None:
            return "/usr/bin/docker"

        monkeypatch.setattr(vmalert_dryrun.shutil, "which", _which)

        created_files: list[str] = []
        call_count = 0

        def mock_run(
            args: list[object], *a: object, **kw: object
        ) -> subprocess.CompletedProcess[str]:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
            for _i, arg in enumerate(args):
                if isinstance(arg, str) and arg.startswith("-rule="):
                    rule_path = arg[6:]
                    if rule_path.startswith("/dryrun/"):
                        host_filename = rule_path[8:]
                        full_path = str(tmp_path / host_filename)
                        if Path(full_path).exists():
                            created_files.append(full_path)
            return subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="boom")

        monkeypatch.setattr(vmalert_dryrun.subprocess, "run", mock_run)

        result = run_vmalert_dryrun(
            "rule: test",
            image="test:1",
            timeout_s=1.0,
            mount_source="vol",
            work_dir=str(tmp_path),
        )
        assert result.skipped is False
        assert result.ok is False
        # Verify cleanup
        for f in created_files:
            assert not Path(f).exists()
