"""vmalert -dryRun exact-parser validation (STAGE-004-043A).

ADDITIVE layer above the always-on heuristic ``expr_validate.validate_expr``. Runs
the rendered single-rule YAML through ``victoriametrics/vmalert -dryRun
-rule.validateExpressions`` in a throwaway container. FAIL-OPEN: if docker / the
image / the mount is unavailable, returns skipped=True (the heuristic floor still
ran). Only a clean run with exit!=0 reports ok=False.

Docker tempfile visibility: the monitor is itself a container using the host docker
socket, so the tempfile must live on a path the HOST daemon can mount. We write into
a named-volume-backed work dir and mount that volume (by source) into the dry-run
container at /dryrun. See the stage spec's "DOCKER TEMPFILE VISIBILITY" section.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol

import structlog

_CONTAINER_MOUNT: Final[str] = "/dryrun"
_log = structlog.get_logger().bind(component="vmalert_dryrun")


@dataclass(frozen=True, slots=True)
class DryRunResult:
    """Outcome of a vmalert -dryRun attempt.

    skipped=True  -> dry-run could not run (docker/image/mount missing); treat as
                     inconclusive PASS (the heuristic floor already validated).
    ok            -> True on exit 0 (or when skipped); False on a clean exit!=0.
    stderr        -> captured stderr when ok is False (else "").
    """

    skipped: bool
    ok: bool
    stderr: str


class DryRunRunner(Protocol):
    """Callable injected into the repo: rule_yaml -> DryRunResult. Never raises."""

    def __call__(self, rule_yaml: str) -> DryRunResult: ...


def _docker_available(image: str) -> bool:
    """True iff the docker CLI exists AND the vmalert image can be inspected."""
    if shutil.which("docker") is None:
        return False
    try:
        proc = subprocess.run(
            ["docker", "image", "inspect", image],
            capture_output=True,
            text=True,
            timeout=10.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def run_vmalert_dryrun(
    rule_yaml: str,
    *,
    image: str,
    timeout_s: float,
    mount_source: str,
    work_dir: str,
) -> DryRunResult:
    """Run `docker run --rm -v <mount_source>:/dryrun:ro vmalert:<image>
    -dryRun -rule.validateExpressions -rule=/dryrun/<tmpfile>` on rule_yaml.

    mount_source is the docker -v SOURCE (named volume name or host path) backing
    work_dir. The work_dir is the monitor-side directory the tempfile is written
    into. The tempfile is referenced by basename within the container at
    /dryrun/<basename>.

    FAIL-OPEN: docker missing / image-inspect fail / OSError / TimeoutExpired ->
    DryRunResult(skipped=True, ok=True, stderr="").
    exit 0 -> DryRunResult(skipped=False, ok=True, stderr="").
    exit!=0 -> DryRunResult(skipped=False, ok=False, stderr=<captured>).
    """
    if not mount_source or not work_dir:
        _log.info("vmalert_dryrun.skip_no_mount")
        return DryRunResult(skipped=True, ok=True, stderr="")
    if not _docker_available(image):
        _log.info("vmalert_dryrun.skip_docker_unavailable", image=image)
        return DryRunResult(skipped=True, ok=True, stderr="")

    wd = Path(work_dir)
    try:
        wd.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        _log.info("vmalert_dryrun.skip_workdir_unwritable", work_dir=work_dir, error=str(exc))
        return DryRunResult(skipped=True, ok=True, stderr="")

    tmp_path: str | None = None
    try:
        fd, tmp_path = tempfile.mkstemp(prefix="dryrun-", suffix=".yaml", dir=str(wd))
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(rule_yaml)
        # Mount work_dir's backing (mount_source) at /dryrun in the container.
        # The tempfile sits at <work_dir>/<basename>, so reference it as
        # /dryrun/<basename> in the container.
        container_rule = f"{_CONTAINER_MOUNT}/{Path(tmp_path).name}"
        args = [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{mount_source}:{_CONTAINER_MOUNT}:ro",
            image,
            "-dryRun",
            "-rule.validateExpressions",
            f"-rule={container_rule}",
        ]
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        _log.info("vmalert_dryrun.skip_run_error", error=str(exc))
        return DryRunResult(skipped=True, ok=True, stderr="")
    finally:
        if tmp_path is not None:
            with contextlib.suppress(OSError):  # pragma: no cover -- benign cleanup race
                os.unlink(tmp_path)

    if proc.returncode == 0:
        return DryRunResult(skipped=False, ok=True, stderr="")
    return DryRunResult(skipped=False, ok=False, stderr=proc.stderr.strip())


__all__ = ["DryRunResult", "DryRunRunner", "run_vmalert_dryrun"]
