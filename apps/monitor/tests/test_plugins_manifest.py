"""Tests for SubprocessManifest schema validation."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from homelab_monitor.kernel.plugins.manifest import (
    SubprocessManifest,
    _parse_duration,  # pyright: ignore[reportPrivateUsage]
)
from homelab_monitor.kernel.plugins.types import TrustLevel

_MIN_VALID = {
    "manifest": 1,
    "name": "test-plugin",
    "command": ["./run.sh"],
    "interval": "60s",
    "timeout": "10s",
}


def test_valid_manifest_parses_minimal_yaml() -> None:
    m = SubprocessManifest.model_validate(_MIN_VALID)
    assert m.name == "test-plugin"
    assert m.interval == timedelta(seconds=60)
    assert m.timeout == timedelta(seconds=10)
    assert m.trust_level == TrustLevel.TRUSTED


def test_invalid_manifest_version_rejected() -> None:
    with pytest.raises(ValidationError):
        SubprocessManifest.model_validate({**_MIN_VALID, "manifest": 2})


def test_invalid_name_regex_rejected() -> None:
    with pytest.raises(ValidationError):
        SubprocessManifest.model_validate({**_MIN_VALID, "name": "Bad Name!"})


def test_interval_must_be_at_least_5s() -> None:
    with pytest.raises(ValidationError):
        SubprocessManifest.model_validate({**_MIN_VALID, "interval": "3s", "timeout": "1s"})


def test_timeout_must_be_less_than_interval() -> None:
    with pytest.raises(ValidationError):
        SubprocessManifest.model_validate({**_MIN_VALID, "interval": "10s", "timeout": "10s"})


def test_command_must_be_nonempty() -> None:
    with pytest.raises(ValidationError):
        SubprocessManifest.model_validate({**_MIN_VALID, "command": []})


def test_trust_level_builtin_rejected() -> None:
    with pytest.raises(ValidationError):
        SubprocessManifest.model_validate({**_MIN_VALID, "trust_level": "builtin"})


def test_trust_level_untrusted_accepted() -> None:
    m = SubprocessManifest.model_validate({**_MIN_VALID, "trust_level": "untrusted"})
    assert m.trust_level == TrustLevel.UNTRUSTED


def test_duration_string_60s_parses_to_60_seconds() -> None:
    m = SubprocessManifest.model_validate({**_MIN_VALID, "interval": "60s"})
    assert m.interval == timedelta(seconds=60)


def test_duration_string_5m_parses_to_300_seconds() -> None:
    m = SubprocessManifest.model_validate({**_MIN_VALID, "interval": "5m", "timeout": "30s"})
    assert m.interval == timedelta(seconds=300)


def test_duration_string_1h_parses_correctly() -> None:
    m = SubprocessManifest.model_validate({**_MIN_VALID, "interval": "1h", "timeout": "30s"})
    assert m.interval == timedelta(hours=1)


def test_duration_int_60_parses_to_60_seconds() -> None:
    m = SubprocessManifest.model_validate({**_MIN_VALID, "interval": 60})
    assert m.interval == timedelta(seconds=60)


def test_invalid_duration_string_rejected() -> None:
    with pytest.raises(ValidationError):
        SubprocessManifest.model_validate({**_MIN_VALID, "interval": "10x"})


def test_extra_fields_rejected() -> None:
    with pytest.raises(ValidationError):
        SubprocessManifest.model_validate({**_MIN_VALID, "unknown_field": "x"})


def test_load_from_path_reads_yaml(tmp_path: Path) -> None:
    manifest_path = tmp_path / "plugin.yaml"
    manifest_path.write_text(yaml.safe_dump(_MIN_VALID))
    m = SubprocessManifest.load_from_path(manifest_path)
    assert m.name == "test-plugin"


def test_load_from_path_rejects_non_mapping(tmp_path: Path) -> None:
    manifest_path = tmp_path / "plugin.yaml"
    manifest_path.write_text("- 1\n- 2\n")  # YAML list, not mapping
    with pytest.raises(ValueError, match="must be a YAML mapping"):
        SubprocessManifest.load_from_path(manifest_path)


def test_duration_float_parses_correctly() -> None:
    """Float seconds parse to timedelta."""
    assert _parse_duration(60.0) == timedelta(seconds=60.0)
    assert _parse_duration(10.5) == timedelta(seconds=10.5)


def test_duration_timedelta_passthrough() -> None:
    """Timedelta object is passed through unchanged."""
    td = timedelta(seconds=120)
    assert _parse_duration(td) is td
