"""Unit tests for DockerContainerOverride schema."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from homelab_monitor.kernel.docker.override_schema import DockerContainerOverride

_EXPECTED_PROBE_COUNT = 2
_DEFAULT_INTERVAL_SECONDS = 30
_DEFAULT_TIMEOUT_SECONDS = 10


def test_valid_override_parses() -> None:
    """Happy path: container + two probes (http + tcp), all defaults."""
    data = {
        "container": "test_svc",
        "probes": [
            {"kind": "http", "name": "api", "target": "http://localhost:8080/health"},
            {"kind": "tcp", "name": "port", "target": "localhost:5432"},
        ],
    }
    override = DockerContainerOverride.model_validate(data, context={"filename_stem": "test_svc"})
    assert override.container == "test_svc"
    assert len(override.probes) == _EXPECTED_PROBE_COUNT
    assert override.probes[0].kind == "http"
    assert override.probes[1].kind == "tcp"
    assert override.disabled is False
    assert override.exec_authorized is False


def test_invalid_kind_rejected() -> None:
    """kind: ssh raises ValidationError."""
    data = {
        "container": "test_svc",
        "probes": [{"kind": "ssh", "name": "shell", "target": "localhost"}],
    }
    with pytest.raises(ValidationError):
        DockerContainerOverride.model_validate(data, context={"filename_stem": "test_svc"})


def test_duplicate_probe_identity_rejected() -> None:
    """Two probes with same (kind, name) raises ValidationError."""
    data = {
        "container": "test_svc",
        "probes": [
            {"kind": "http", "name": "api", "target": "http://localhost:8080"},
            {"kind": "http", "name": "api", "target": "http://localhost:9090"},
        ],
    }
    with pytest.raises(ValidationError) as exc_info:
        DockerContainerOverride.model_validate(data, context={"filename_stem": "test_svc"})
    assert "duplicate probe identity" in str(exc_info.value)


def test_extra_field_forbidden_top_level() -> None:
    """Extra key on DockerContainerOverride raises ValidationError."""
    data = {
        "container": "test_svc",
        "unknown_field": "bad",
    }
    with pytest.raises(ValidationError):
        DockerContainerOverride.model_validate(data, context={"filename_stem": "test_svc"})


def test_extra_field_forbidden_in_probe() -> None:
    """Extra key inside a probe raises ValidationError."""
    data = {
        "container": "test_svc",
        "probes": [
            {
                "kind": "http",
                "name": "api",
                "target": "http://localhost:8080",
                "unknown_field": "bad",
            }
        ],
    }
    with pytest.raises(ValidationError):
        DockerContainerOverride.model_validate(data, context={"filename_stem": "test_svc"})


def test_container_must_match_filename_stem() -> None:
    """container: foo but file bar.yaml fails with helpful message."""
    with tempfile.TemporaryDirectory() as tmpdir:
        file_path = Path(tmpdir) / "bar.yaml"
        file_path.write_text("container: foo\nprobes: []\n")
        with pytest.raises(ValueError) as exc_info:
            DockerContainerOverride.load_from_path(file_path)
        assert "must equal filename stem" in str(exc_info.value)
        assert "bar" in str(exc_info.value)


def test_load_from_path_rejects_non_mapping() -> None:
    """YAML root is a list, raises ValueError."""
    with tempfile.TemporaryDirectory() as tmpdir:
        file_path = Path(tmpdir) / "bad.yaml"
        file_path.write_text("- item1\n- item2\n")
        with pytest.raises(ValueError) as exc_info:
            DockerContainerOverride.load_from_path(file_path)
        assert "must be a YAML mapping" in str(exc_info.value)


def test_load_from_path_rejects_malformed_yaml() -> None:
    """Invalid YAML syntax raises yaml.YAMLError."""
    with tempfile.TemporaryDirectory() as tmpdir:
        file_path = Path(tmpdir) / "bad.yaml"
        file_path.write_text("  \n invalid: [yaml: syntax\n")
        with pytest.raises(yaml.YAMLError):
            DockerContainerOverride.load_from_path(file_path)


def test_disabled_flag_defaults_false() -> None:
    """disabled defaults to False."""
    data = {"container": "test_svc"}
    override = DockerContainerOverride.model_validate(data, context={"filename_stem": "test_svc"})
    assert override.disabled is False


def test_exec_authorized_defaults_false() -> None:
    """exec_authorized defaults to False."""
    data = {"container": "test_svc"}
    override = DockerContainerOverride.model_validate(data, context={"filename_stem": "test_svc"})
    assert override.exec_authorized is False


def test_probes_default_empty_list() -> None:
    """No probes: parses to empty list (legal — operator disabling all probes)."""
    data = {"container": "test_svc"}
    override = DockerContainerOverride.model_validate(data, context={"filename_stem": "test_svc"})
    assert override.probes == []


def test_probe_defaults() -> None:
    """Probe defaults: enabled=True, interval_seconds=30, timeout_seconds=10."""
    data = {
        "container": "test_svc",
        "probes": [{"kind": "http", "name": "default", "target": "http://localhost"}],
    }
    override = DockerContainerOverride.model_validate(data, context={"filename_stem": "test_svc"})
    probe = override.probes[0]
    assert probe.enabled is True
    assert probe.interval_seconds == _DEFAULT_INTERVAL_SECONDS
    assert probe.timeout_seconds == _DEFAULT_TIMEOUT_SECONDS


def test_load_from_path_valid_file() -> None:
    """load_from_path reads YAML and cross-checks filename stem."""
    with tempfile.TemporaryDirectory() as tmpdir:
        file_path = Path(tmpdir) / "myservice.yaml"
        file_path.write_text(
            "container: myservice\n"
            "exec_authorized: true\n"
            "probes:\n"
            "  - kind: http\n"
            "    name: health\n"
            "    target: http://localhost:8080/health\n"
        )
        override = DockerContainerOverride.load_from_path(file_path)
        assert override.container == "myservice"
        assert override.exec_authorized is True
        assert len(override.probes) == 1
        assert override.probes[0].kind == "http"


def test_validator_raises_when_context_missing() -> None:
    """The container-name validator REQUIRES context={'filename_stem': ...};
    direct model_validate without context raises ValidationError (covers override_schema.py:48)."""
    with pytest.raises(ValidationError, match="requires context"):
        DockerContainerOverride.model_validate({"container": "foo", "probes": []})
