"""Tests for build_sources_schema.py (STAGE-003-009 Wave G)."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from homelab_monitor.kernel.docker.build_sources_schema import (
    BuildContextRemap,
    BuildSourcesConfig,
    BuildSourcesConfigError,
    ComposeFileEntry,
)


def _write(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "build-sources.yaml"
    p.write_text(content, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# ComposeFileEntry validation
# ---------------------------------------------------------------------------


def test_compose_file_entry_accepts_absolute_paths() -> None:
    """ComposeFileEntry accepts both absolute host_path and container_path."""
    entry = ComposeFileEntry(host_path="/storage/compose.yml", container_path="/host/compose.yml")
    assert entry.host_path == "/storage/compose.yml"
    assert entry.container_path == "/host/compose.yml"


def test_compose_file_entry_rejects_relative_host_path() -> None:
    """ComposeFileEntry raises ValidationError for relative host_path."""
    with pytest.raises(ValidationError):
        ComposeFileEntry(host_path="relative/path.yml", container_path="/host/path.yml")


def test_compose_file_entry_rejects_relative_container_path() -> None:
    """ComposeFileEntry raises ValidationError for relative container_path."""
    with pytest.raises(ValidationError):
        ComposeFileEntry(host_path="/storage/path.yml", container_path="relative/path.yml")


def test_compose_file_entry_rejects_extra_fields() -> None:
    """ComposeFileEntry raises ValidationError for extra fields (extra='forbid')."""
    with pytest.raises(ValidationError):
        ComposeFileEntry.model_validate(
            {
                "host_path": "/storage/path.yml",
                "container_path": "/host/path.yml",
                "unknown_field": "oops",
            }
        )


# ---------------------------------------------------------------------------
# BuildContextRemap validation
# ---------------------------------------------------------------------------


def test_build_context_remap_accepts_absolute_paths() -> None:
    """BuildContextRemap accepts both absolute host_prefix and container_prefix."""
    remap = BuildContextRemap(
        host_prefix="/storage/programs", container_prefix="/host-build-contexts/programs"
    )
    assert remap.host_prefix == "/storage/programs"
    assert remap.container_prefix == "/host-build-contexts/programs"


def test_build_context_remap_rejects_relative_host_prefix() -> None:
    """BuildContextRemap raises ValidationError for relative host_prefix."""
    with pytest.raises(ValidationError):
        BuildContextRemap(host_prefix="relative/prefix", container_prefix="/container/prefix")


def test_build_context_remap_rejects_relative_container_prefix() -> None:
    """BuildContextRemap raises ValidationError for relative container_prefix."""
    with pytest.raises(ValidationError):
        BuildContextRemap(host_prefix="/host/prefix", container_prefix="relative/prefix")


# ---------------------------------------------------------------------------
# BuildSourcesConfig model-level validation
# ---------------------------------------------------------------------------


def test_build_sources_config_happy_path() -> None:
    """BuildSourcesConfig accepts multi-compose + multi-remap valid config."""
    cfg = BuildSourcesConfig(
        compose_files=[
            ComposeFileEntry(host_path="/h/a.yml", container_path="/c/a.yml"),
            ComposeFileEntry(host_path="/h/b.yml", container_path="/c/b.yml"),
        ],
        build_context_roots=[
            BuildContextRemap(host_prefix="/h/programs", container_prefix="/c/programs"),
            BuildContextRemap(host_prefix="/h/apps", container_prefix="/c/apps"),
        ],
    )
    assert len(cfg.compose_files) == 2  # noqa: PLR2004 -- test-only literal
    assert len(cfg.build_context_roots) == 2  # noqa: PLR2004 -- test-only literal


def test_build_sources_config_rejects_empty_compose_files() -> None:
    """BuildSourcesConfig raises ValidationError for empty compose_files list."""
    with pytest.raises(ValidationError):
        BuildSourcesConfig(compose_files=[])


def test_build_sources_config_rejects_duplicate_container_path() -> None:
    """BuildSourcesConfig raises ValidationError for duplicate container_path."""
    with pytest.raises(ValidationError):
        BuildSourcesConfig(
            compose_files=[
                ComposeFileEntry(host_path="/h/a.yml", container_path="/c/same.yml"),
                ComposeFileEntry(host_path="/h/b.yml", container_path="/c/same.yml"),
            ]
        )


def test_build_sources_config_rejects_duplicate_host_prefix() -> None:
    """BuildSourcesConfig raises ValidationError for duplicate host_prefix."""
    with pytest.raises(ValidationError):
        BuildSourcesConfig(
            compose_files=[
                ComposeFileEntry(host_path="/h/a.yml", container_path="/c/a.yml"),
            ],
            build_context_roots=[
                BuildContextRemap(host_prefix="/storage", container_prefix="/c/storage"),
                BuildContextRemap(host_prefix="/storage", container_prefix="/c/storage2"),
            ],
        )


# ---------------------------------------------------------------------------
# BuildSourcesConfig.load_from_path — error reasons
# ---------------------------------------------------------------------------


def test_load_from_path_missing_file(tmp_path: Path) -> None:
    """load_from_path raises BuildSourcesConfigError(reason='file_not_found') for missing file."""
    p = tmp_path / "missing.yaml"
    with pytest.raises(BuildSourcesConfigError) as exc_info:
        BuildSourcesConfig.load_from_path(p)
    assert exc_info.value.reason == "file_not_found"


def test_load_from_path_malformed_yaml(tmp_path: Path) -> None:
    """load_from_path raises BuildSourcesConfigError(reason='malformed_yaml') for bad YAML."""
    p = _write(tmp_path, "compose_files: {\nbroken: [unclosed\n")
    with pytest.raises(BuildSourcesConfigError) as exc_info:
        BuildSourcesConfig.load_from_path(p)
    assert exc_info.value.reason == "malformed_yaml"


def test_load_from_path_list_root_rejected(tmp_path: Path) -> None:
    """load_from_path raises BuildSourcesConfigError(reason='non_dict_root') for list root."""
    p = _write(tmp_path, "- item1\n- item2\n")
    with pytest.raises(BuildSourcesConfigError) as exc_info:
        BuildSourcesConfig.load_from_path(p)
    assert exc_info.value.reason == "non_dict_root"


def test_load_from_path_missing_required_key(tmp_path: Path) -> None:
    """load_from_path raises BuildSourcesConfigError(reason='invalid_schema') for missing key."""
    p = _write(tmp_path, "build_context_roots: []\n")  # compose_files missing
    with pytest.raises(BuildSourcesConfigError) as exc_info:
        BuildSourcesConfig.load_from_path(p)
    assert exc_info.value.reason == "invalid_schema"


def test_load_from_path_empty_compose_files(tmp_path: Path) -> None:
    """load_from_path raises BuildSourcesConfigError(reason='invalid_schema') for empty list."""
    p = _write(tmp_path, "compose_files: []\nbuild_context_roots: []\n")
    with pytest.raises(BuildSourcesConfigError) as exc_info:
        BuildSourcesConfig.load_from_path(p)
    assert exc_info.value.reason == "invalid_schema"


def test_load_from_path_happy_path_roundtrip(tmp_path: Path) -> None:
    """load_from_path successfully loads a valid YAML and returns BuildSourcesConfig."""
    p = _write(
        tmp_path,
        (
            "compose_files:\n"
            "  - host_path: /storage/compose.yml\n"
            "    container_path: /host/compose.yml\n"
            "build_context_roots:\n"
            "  - host_prefix: /storage/programs\n"
            "    container_prefix: /host-build-contexts/programs\n"
        ),
    )
    cfg = BuildSourcesConfig.load_from_path(p)
    assert len(cfg.compose_files) == 1
    assert cfg.compose_files[0].host_path == "/storage/compose.yml"
    assert cfg.compose_files[0].container_path == "/host/compose.yml"
    assert len(cfg.build_context_roots) == 1
    assert cfg.build_context_roots[0].host_prefix == "/storage/programs"
