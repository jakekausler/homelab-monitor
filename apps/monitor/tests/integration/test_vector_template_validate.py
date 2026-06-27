"""Integration test: rendered vector.toml.template passes `vector validate`.

Runs vector validate in a throwaway container. Skips fast when docker CLI is
absent.

Run via:
    make integration            # full rig stack
    pytest -m integration apps/monitor/tests/integration/test_vector_template_validate.py
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

# Path to the template (relative to repo root, resolved from this file's location)
_TEMPLATE_PATH = (
    Path(__file__).parent.parent.parent.parent.parent  # repo root
    / "deploy"
    / "vector"
    / "vector.toml.template"
)


def _docker_available() -> bool:
    return shutil.which("docker") is not None


def _render_template(docker_exclude: str = "[]") -> str:
    """Render deploy/vector/vector.toml.template with dummy values.

    Mirrors the logic in tests/test_vector_template.py::_render_template()
    exactly — any divergence would defeat the purpose of this gate.
    """
    from homelab_monitor.kernel.config import DEFAULT_REDACT_PATTERNS  # noqa: PLC0415
    from homelab_monitor.kernel.cron.render import (  # noqa: PLC0415
        build_redact_metric_entries,
        build_redact_strip_markers,
        build_redact_vrl,
    )

    text = _TEMPLATE_PATH.read_text(encoding="utf-8")
    text = text.replace("${CRON_EVENTS_INGEST_TOKEN}", "dummy-token-for-test")
    text = text.replace("${VECTOR_DOCKER_EXCLUDE}", docker_exclude)
    pats = list(DEFAULT_REDACT_PATTERNS)
    text = text.replace("${VECTOR_REDACT_TRANSFORMS}", build_redact_vrl(pats))
    text = text.replace("${VECTOR_REDACT_STRIP_MARKERS}", build_redact_strip_markers(pats))
    text = text.replace("${VECTOR_REDACT_METRICS}", build_redact_metric_entries(pats))
    text = text.replace("${HOMELAB_MONITOR_LOG_JSON_MAX_DEPTH}", "8")
    text = text.replace("${HOMELAB_MONITOR_LOG_JSON_MAX_FIELDS}", "100")
    text = text.replace("${HM_SYNOLOGY_SYSLOG_BIND_HOST}", "0.0.0.0")
    text = text.replace("${HM_SYNOLOGY_SYSLOG_PORT}", "5515")
    return text


@pytest.mark.integration
@pytest.mark.slow
def test_rendered_template_passes_vector_validate_via_container(tmp_path: Path) -> None:
    """Render vector.toml.template and validate it in a throwaway vector container.

    This test exists to catch VRL syntax errors (e.g. E651) that would
    crash-loop the production vector container. It spins up a one-shot
    timberio/vector container to validate the rendered config, then tears it down.

    Skip conditions (fast, no 30s hang):
    - docker CLI not on PATH (e.g. pure-Python CI without docker)
    """
    if not _docker_available():
        pytest.skip("docker CLI not on PATH")

    rendered = _render_template()

    # Write rendered config to a temp file on the host
    config_file = tmp_path / "vector-validate.toml"
    config_file.write_text(rendered, encoding="utf-8")
    host_path = str(config_file.absolute())

    # Run vector validate in a throwaway container
    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{host_path}:/validate.toml:ro",
            "timberio/vector:0.41.1-debian",
            "validate",
            "/validate.toml",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        pytest.fail(f"vector validate failed:\n{result.stdout}\n{result.stderr}")
