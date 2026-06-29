"""Tests for the runbook loader module."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import yaml

from homelab_monitor.kernel.runbooks.loader import (
    RUNBOOK_CONFIG_FILENAME,
    RUNBOOK_PROMPT_FILENAME,
    scan_runbooks,
)


def _valid_config_dict(name: str = "test-runbook") -> dict[str, object]:
    """Create a minimal valid runbook config."""
    return {
        "runbook": 1,
        "name": name,
        "match_patterns": [{"alertname": "HighCPU"}],
        "risk_tag": "safe",
        "dry_run_required": True,
        "rate_limit_per_hour": 5,
        "cooldown_seconds": 300,
        "scoped_capabilities": {"docker": {"container": "c1", "allowed_actions": ["restart"]}},
    }


def _write_runbook(
    folder: Path, *, config: Mapping[str, object] | None = None, claude: str | None = "do the thing"
) -> None:
    """Write a test runbook folder with optional config and prompt files."""
    folder.mkdir(parents=True, exist_ok=True)
    if config is not None:
        (folder / RUNBOOK_CONFIG_FILENAME).write_text(yaml.safe_dump(config))
    if claude is not None:
        (folder / RUNBOOK_PROMPT_FILENAME).write_text(claude)


def test_scan_root_not_a_directory(tmp_path: Path) -> None:
    """Root missing -> one error mentioning 'not a directory', loaded == []."""
    missing_root = tmp_path / "does-not-exist"
    result = scan_runbooks(missing_root)

    assert result.loaded == []
    assert len(result.errors) == 1
    assert "not a directory" in result.errors[0].message
    assert str(missing_root) in result.errors[0].path


def test_scan_skips_non_dir_entries(tmp_path: Path) -> None:
    """A file sitting in root is ignored (not error, not loaded)."""
    (tmp_path / "just-a-file.txt").write_text("hello")
    result = scan_runbooks(tmp_path)

    assert result.loaded == []
    assert result.errors == []


def test_scan_skips_underscore_folders(tmp_path: Path) -> None:
    """_examples/ folder with valid contents is skipped (not loaded, not error)."""
    examples = tmp_path / "_examples"
    _write_runbook(examples, config=_valid_config_dict("example"))
    result = scan_runbooks(tmp_path)

    assert result.loaded == []
    assert result.errors == []


def test_scan_missing_runbook_yaml(tmp_path: Path) -> None:
    """Folder with CLAUDE.md but no runbook.yaml -> error 'missing runbook.yaml'."""
    folder = tmp_path / "no-config"
    _write_runbook(folder, config=None)
    result = scan_runbooks(tmp_path)

    assert result.loaded == []
    assert len(result.errors) == 1
    assert "missing runbook.yaml" in result.errors[0].message


def test_scan_invalid_config_reported(tmp_path: Path) -> None:
    """runbook.yaml with bad data -> error contains issue; not fatal."""
    folder = tmp_path / "bad-config"
    bad_config = {
        "runbook": 1,
        "name": "x",  # pattern is r"^[a-z][a-z0-9_-]{2,63}$" — too short
        "match_patterns": [{"alertname": "HighCPU"}],
        "risk_tag": "safe",
        "dry_run_required": True,
        "rate_limit_per_hour": 5,
        "cooldown_seconds": 300,
        "scoped_capabilities": {"docker": {"container": "c1", "allowed_actions": ["restart"]}},
    }
    _write_runbook(folder, config=bad_config)
    result = scan_runbooks(tmp_path)

    assert result.loaded == []
    assert len(result.errors) == 1
    assert str(folder) in result.errors[0].path


def test_scan_malformed_yaml_reported(tmp_path: Path) -> None:
    """runbook.yaml with garbage YAML -> reported as error, not raised."""
    folder = tmp_path / "malformed"
    folder.mkdir()
    (folder / RUNBOOK_CONFIG_FILENAME).write_text(": : :")
    (folder / RUNBOOK_PROMPT_FILENAME).write_text("prompt")
    result = scan_runbooks(tmp_path)

    assert result.loaded == []
    assert len(result.errors) == 1
    assert str(folder) in result.errors[0].path


def test_scan_missing_claude_md(tmp_path: Path) -> None:
    """Valid runbook.yaml, no CLAUDE.md -> error 'missing CLAUDE.md'."""
    folder = tmp_path / "no-prompt"
    _write_runbook(folder, config=_valid_config_dict(), claude=None)
    result = scan_runbooks(tmp_path)

    assert result.loaded == []
    assert len(result.errors) == 1
    assert "missing CLAUDE.md" in result.errors[0].message


def test_scan_empty_claude_md(tmp_path: Path) -> None:
    """Valid runbook.yaml, CLAUDE.md is whitespace-only -> error 'CLAUDE.md is empty'."""
    folder = tmp_path / "empty-prompt"
    _write_runbook(folder, config=_valid_config_dict(), claude="   \n  \n")
    result = scan_runbooks(tmp_path)

    assert result.loaded == []
    assert len(result.errors) == 1
    assert "CLAUDE.md is empty" in result.errors[0].message


def test_scan_valid_folder_loaded(tmp_path: Path) -> None:
    """Full valid folder -> one LoadedRunbook with correct config.name, no errors."""
    folder = tmp_path / "good-runbook"
    config = _valid_config_dict("my-runbook")
    _write_runbook(folder, config=config)
    result = scan_runbooks(tmp_path)

    assert len(result.loaded) == 1
    assert result.loaded[0].folder == folder
    assert result.loaded[0].config.name == "my-runbook"
    assert result.errors == []


def test_scan_one_bad_does_not_block_good(tmp_path: Path) -> None:
    """Root with one valid + one invalid-config folder -> 1 loaded, 1 error."""
    good = tmp_path / "good"
    bad = tmp_path / "bad"

    _write_runbook(good, config=_valid_config_dict("good-one"))
    bad_config = {"name": "x"}  # invalid
    _write_runbook(bad, config=bad_config)

    result = scan_runbooks(tmp_path)

    assert len(result.loaded) == 1
    assert result.loaded[0].config.name == "good-one"
    assert len(result.errors) == 1
    assert str(bad) in result.errors[0].path


def test_scan_skips_underscore_alongside_valid(tmp_path: Path) -> None:
    """Root with _examples/ (pihole exemplar) + a valid folder -> only valid one loaded."""
    examples = tmp_path / "_examples"
    valid = tmp_path / "real-runbook"

    _write_runbook(examples, config=_valid_config_dict("exemplar"))
    _write_runbook(valid, config=_valid_config_dict("real"))

    result = scan_runbooks(tmp_path)

    assert len(result.loaded) == 1
    assert result.loaded[0].config.name == "real"
    assert result.errors == []
