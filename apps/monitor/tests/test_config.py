"""Tests for :func:`load_disk_budget_config`."""

from __future__ import annotations

from pathlib import Path

import pytest

from homelab_monitor.kernel.config import (
    DiskBudgetConfig,
    LogStreamBudgetConfig,
    load_disk_budget_config,
    load_log_stream_budget_config,
)


def test_load_returns_defaults_when_file_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """No config file + no env override = built-in defaults."""
    monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(tmp_path / "missing.yaml"))
    monkeypatch.delenv("HOMELAB_MONITOR_DISK_BUDGET_GB", raising=False)
    cfg = load_disk_budget_config()
    assert cfg == DiskBudgetConfig()


def test_load_reads_yaml(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """YAML with disk_budget section overrides defaults."""
    cfg_file = tmp_path / "homelab-monitor.yaml"
    cfg_file.write_text(
        "disk_budget:\n  total_gb: 100\n  vm_ratio: 0.5\n  vl_ratio: 0.3\n  sqlite_ratio: 0.2\n"
    )
    monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(cfg_file))
    monkeypatch.delenv("HOMELAB_MONITOR_DISK_BUDGET_GB", raising=False)
    cfg = load_disk_budget_config()
    assert cfg.total_gb == 100.0  # noqa: PLR2004
    assert cfg.vm_ratio == 0.5  # noqa: PLR2004
    assert cfg.vl_ratio == 0.3  # noqa: PLR2004
    assert cfg.sqlite_ratio == 0.2  # noqa: PLR2004


def test_load_rejects_bad_ratios(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Ratios that don't sum to ~1.0 raise ValueError."""
    cfg_file = tmp_path / "bad.yaml"
    cfg_file.write_text("disk_budget:\n  vm_ratio: 0.5\n  vl_ratio: 0.5\n  sqlite_ratio: 0.5\n")
    monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(cfg_file))
    with pytest.raises(ValueError, match="ratios"):
        load_disk_budget_config()


def test_load_env_override_total_gb(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """HOMELAB_MONITOR_DISK_BUDGET_GB overrides total_gb only."""
    monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(tmp_path / "absent.yaml"))
    monkeypatch.setenv("HOMELAB_MONITOR_DISK_BUDGET_GB", "200")
    cfg = load_disk_budget_config()
    assert cfg.total_gb == 200.0  # noqa: PLR2004
    # Other ratios are still defaults
    defaults = DiskBudgetConfig()
    assert cfg.vm_ratio == defaults.vm_ratio


def test_load_env_override_combines_with_yaml(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """env override applied AFTER YAML load."""
    cfg_file = tmp_path / "homelab-monitor.yaml"
    cfg_file.write_text(
        "disk_budget:\n  total_gb: 50\n  vm_ratio: 0.7\n  vl_ratio: 0.2\n  sqlite_ratio: 0.1\n"
    )
    monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(cfg_file))
    monkeypatch.setenv("HOMELAB_MONITOR_DISK_BUDGET_GB", "300")
    cfg = load_disk_budget_config()
    assert cfg.total_gb == 300.0  # noqa: PLR2004  -- env wins
    assert cfg.vm_ratio == 0.7  # noqa: PLR2004  -- yaml ratio kept


def test_load_rejects_non_mapping_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A YAML file with a list as the root raises ValueError."""
    cfg_file = tmp_path / "bad.yaml"
    cfg_file.write_text("- a\n- b\n")
    monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(cfg_file))
    with pytest.raises(ValueError, match="config root must be a mapping"):
        load_disk_budget_config()


def test_load_rejects_non_mapping_disk_budget(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A `disk_budget:` value that isn't a mapping raises ValueError."""
    cfg_file = tmp_path / "bad.yaml"
    cfg_file.write_text("disk_budget: 42\n")
    monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(cfg_file))
    with pytest.raises(ValueError, match="disk_budget must be a mapping"):
        load_disk_budget_config()


def test_load_empty_disk_budget_section(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A `disk_budget:` key with empty value falls through to defaults."""
    cfg_file = tmp_path / "empty.yaml"
    cfg_file.write_text("disk_budget:\n")
    monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(cfg_file))
    monkeypatch.delenv("HOMELAB_MONITOR_DISK_BUDGET_GB", raising=False)
    cfg = load_disk_budget_config()
    assert cfg == DiskBudgetConfig()


# --- LogStreamBudgetConfig ---------------------------------------------------------------


def test_log_stream_budget_defaults_when_file_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """No config file = built-in defaults."""
    monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(tmp_path / "missing.yaml"))
    cfg = load_log_stream_budget_config()
    assert cfg == LogStreamBudgetConfig()


def test_log_stream_budget_yaml_override_lines_per_sec(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """YAML overrides lines_per_sec_per_stream."""
    cfg_file = tmp_path / "homelab-monitor.yaml"
    cfg_file.write_text("log_stream_budget:\n  lines_per_sec_per_stream: 25\n")
    monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(cfg_file))
    cfg = load_log_stream_budget_config()
    assert cfg.lines_per_sec_per_stream == 25.0  # noqa: PLR2004


def test_log_stream_budget_yaml_override_bytes_per_day(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """YAML overrides bytes_per_day_per_stream."""
    cfg_file = tmp_path / "homelab-monitor.yaml"
    cfg_file.write_text("log_stream_budget:\n  bytes_per_day_per_stream: 1048576\n")
    monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(cfg_file))
    cfg = load_log_stream_budget_config()
    assert cfg.bytes_per_day_per_stream == 1048576  # noqa: PLR2004


def test_log_stream_budget_rejects_non_mapping_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """YAML root that isn't a mapping raises ValueError."""
    cfg_file = tmp_path / "bad.yaml"
    cfg_file.write_text("- a\n- b\n")
    monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(cfg_file))
    with pytest.raises(ValueError, match="config root must be a mapping"):
        load_log_stream_budget_config()


def test_log_stream_budget_rejects_non_mapping_section(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """log_stream_budget value that isn't a mapping raises ValueError."""
    cfg_file = tmp_path / "bad.yaml"
    cfg_file.write_text("log_stream_budget: 42\n")
    monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(cfg_file))
    with pytest.raises(ValueError, match="log_stream_budget must be a mapping"):
        load_log_stream_budget_config()


def test_log_stream_budget_empty_section(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """log_stream_budget: with empty value falls through to defaults."""
    cfg_file = tmp_path / "empty.yaml"
    cfg_file.write_text("log_stream_budget:\n")
    monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(cfg_file))
    cfg = load_log_stream_budget_config()
    assert cfg == LogStreamBudgetConfig()
