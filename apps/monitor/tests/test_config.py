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


# --- VlQueryLimits ---------------------------------------------------------------


def test_vl_query_limits_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """load_vl_query_limits returns built-in defaults when no env vars are set."""
    from homelab_monitor.kernel.config import VlQueryLimits, load_vl_query_limits  # noqa: PLC0415

    monkeypatch.delenv("HOMELAB_MONITOR_VL_QUERY_MAX_LINES", raising=False)
    monkeypatch.delenv("HOMELAB_MONITOR_VL_QUERY_MAX_BYTES", raising=False)
    monkeypatch.delenv("HOMELAB_MONITOR_VL_QUERY_TIMEOUT_SECONDS", raising=False)
    cfg = load_vl_query_limits()
    assert cfg == VlQueryLimits()
    assert cfg.max_lines == 10_000  # noqa: PLR2004
    assert cfg.max_bytes == 5_000_000  # noqa: PLR2004
    assert cfg.timeout_seconds == 10.0  # noqa: PLR2004


def test_vl_query_limits_env_max_lines(monkeypatch: pytest.MonkeyPatch) -> None:
    """HOMELAB_MONITOR_VL_QUERY_MAX_LINES overrides max_lines."""
    from homelab_monitor.kernel.config import load_vl_query_limits  # noqa: PLC0415

    monkeypatch.setenv("HOMELAB_MONITOR_VL_QUERY_MAX_LINES", "500")
    monkeypatch.delenv("HOMELAB_MONITOR_VL_QUERY_MAX_BYTES", raising=False)
    monkeypatch.delenv("HOMELAB_MONITOR_VL_QUERY_TIMEOUT_SECONDS", raising=False)
    cfg = load_vl_query_limits()
    assert cfg.max_lines == 500  # noqa: PLR2004


def test_vl_query_limits_env_max_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    """HOMELAB_MONITOR_VL_QUERY_MAX_BYTES overrides max_bytes."""
    from homelab_monitor.kernel.config import load_vl_query_limits  # noqa: PLC0415

    monkeypatch.delenv("HOMELAB_MONITOR_VL_QUERY_MAX_LINES", raising=False)
    monkeypatch.setenv("HOMELAB_MONITOR_VL_QUERY_MAX_BYTES", "123456")
    monkeypatch.delenv("HOMELAB_MONITOR_VL_QUERY_TIMEOUT_SECONDS", raising=False)
    cfg = load_vl_query_limits()
    assert cfg.max_bytes == 123456  # noqa: PLR2004


def test_vl_query_limits_env_timeout_seconds(monkeypatch: pytest.MonkeyPatch) -> None:
    """HOMELAB_MONITOR_VL_QUERY_TIMEOUT_SECONDS overrides timeout_seconds."""
    from homelab_monitor.kernel.config import load_vl_query_limits  # noqa: PLC0415

    monkeypatch.delenv("HOMELAB_MONITOR_VL_QUERY_MAX_LINES", raising=False)
    monkeypatch.delenv("HOMELAB_MONITOR_VL_QUERY_MAX_BYTES", raising=False)
    monkeypatch.setenv("HOMELAB_MONITOR_VL_QUERY_TIMEOUT_SECONDS", "3.5")
    cfg = load_vl_query_limits()
    assert cfg.timeout_seconds == 3.5  # noqa: PLR2004


def test_vl_query_limits_all_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    """All three VL query limit env vars override all fields simultaneously."""
    from homelab_monitor.kernel.config import load_vl_query_limits  # noqa: PLC0415

    monkeypatch.setenv("HOMELAB_MONITOR_VL_QUERY_MAX_LINES", "25")
    monkeypatch.setenv("HOMELAB_MONITOR_VL_QUERY_MAX_BYTES", "9999")
    monkeypatch.setenv("HOMELAB_MONITOR_VL_QUERY_TIMEOUT_SECONDS", "1.0")
    cfg = load_vl_query_limits()
    assert cfg.max_lines == 25  # noqa: PLR2004
    assert cfg.max_bytes == 9999  # noqa: PLR2004
    assert cfg.timeout_seconds == 1.0


# --- CronRunReconcilerConfig ---------------------------------------------------------------


def test_cron_run_reconciler_config_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """load_cron_run_reconciler_config returns built-in defaults when no env vars are set."""
    from homelab_monitor.kernel.config import (  # noqa: PLC0415
        CronRunReconcilerConfig,
        load_cron_run_reconciler_config,
    )

    monkeypatch.delenv("HOMELAB_MONITOR_CRON_RUN_RETENTION_DAYS", raising=False)
    monkeypatch.delenv("HOMELAB_MONITOR_CRON_RUN_MAX_ROWS_PER_CRON", raising=False)
    monkeypatch.delenv("HOMELAB_MONITOR_CRON_RUN_BMODE_TIMEOUT_HOURS", raising=False)
    monkeypatch.delenv("HOMELAB_MONITOR_CRON_RUN_ENRICH_GRACE_SECONDS", raising=False)
    monkeypatch.delenv("HOMELAB_MONITOR_CRON_RUN_ENRICH_MAX_PER_TICK", raising=False)
    monkeypatch.delenv("HOMELAB_MONITOR_CRON_RUN_ENRICH_WINDOW_SLACK_SECONDS", raising=False)
    cfg = load_cron_run_reconciler_config()
    assert cfg == CronRunReconcilerConfig()
    assert cfg.retention_days == 30  # noqa: PLR2004
    assert cfg.max_rows_per_cron == 50_000  # noqa: PLR2004
    assert cfg.bmode_timeout_hours == 6  # noqa: PLR2004
    assert cfg.enrich_grace_seconds == 15  # noqa: PLR2004
    assert cfg.enrich_max_per_tick == 200  # noqa: PLR2004
    assert cfg.enrich_window_slack_seconds == 30  # noqa: PLR2004


def test_cron_run_reconciler_config_env_retention_days(monkeypatch: pytest.MonkeyPatch) -> None:
    """HOMELAB_MONITOR_CRON_RUN_RETENTION_DAYS overrides retention_days."""
    from homelab_monitor.kernel.config import load_cron_run_reconciler_config  # noqa: PLC0415

    monkeypatch.setenv("HOMELAB_MONITOR_CRON_RUN_RETENTION_DAYS", "7")
    monkeypatch.delenv("HOMELAB_MONITOR_CRON_RUN_MAX_ROWS_PER_CRON", raising=False)
    monkeypatch.delenv("HOMELAB_MONITOR_CRON_RUN_BMODE_TIMEOUT_HOURS", raising=False)
    monkeypatch.delenv("HOMELAB_MONITOR_CRON_RUN_ENRICH_GRACE_SECONDS", raising=False)
    cfg = load_cron_run_reconciler_config()
    assert cfg.retention_days == 7  # noqa: PLR2004


def test_cron_run_reconciler_config_env_max_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    """HOMELAB_MONITOR_CRON_RUN_MAX_ROWS_PER_CRON overrides max_rows_per_cron."""
    from homelab_monitor.kernel.config import load_cron_run_reconciler_config  # noqa: PLC0415

    monkeypatch.delenv("HOMELAB_MONITOR_CRON_RUN_RETENTION_DAYS", raising=False)
    monkeypatch.setenv("HOMELAB_MONITOR_CRON_RUN_MAX_ROWS_PER_CRON", "1000")
    monkeypatch.delenv("HOMELAB_MONITOR_CRON_RUN_BMODE_TIMEOUT_HOURS", raising=False)
    monkeypatch.delenv("HOMELAB_MONITOR_CRON_RUN_ENRICH_GRACE_SECONDS", raising=False)
    cfg = load_cron_run_reconciler_config()
    assert cfg.max_rows_per_cron == 1000  # noqa: PLR2004


def test_cron_run_reconciler_config_env_bmode_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """HOMELAB_MONITOR_CRON_RUN_BMODE_TIMEOUT_HOURS overrides bmode_timeout_hours."""
    from homelab_monitor.kernel.config import load_cron_run_reconciler_config  # noqa: PLC0415

    monkeypatch.delenv("HOMELAB_MONITOR_CRON_RUN_RETENTION_DAYS", raising=False)
    monkeypatch.delenv("HOMELAB_MONITOR_CRON_RUN_MAX_ROWS_PER_CRON", raising=False)
    monkeypatch.setenv("HOMELAB_MONITOR_CRON_RUN_BMODE_TIMEOUT_HOURS", "12")
    monkeypatch.delenv("HOMELAB_MONITOR_CRON_RUN_ENRICH_GRACE_SECONDS", raising=False)
    cfg = load_cron_run_reconciler_config()
    assert cfg.bmode_timeout_hours == 12  # noqa: PLR2004


def test_cron_run_reconciler_config_env_enrich_grace(monkeypatch: pytest.MonkeyPatch) -> None:
    """HOMELAB_MONITOR_CRON_RUN_ENRICH_GRACE_SECONDS overrides enrich_grace_seconds."""
    from homelab_monitor.kernel.config import load_cron_run_reconciler_config  # noqa: PLC0415

    monkeypatch.delenv("HOMELAB_MONITOR_CRON_RUN_RETENTION_DAYS", raising=False)
    monkeypatch.delenv("HOMELAB_MONITOR_CRON_RUN_MAX_ROWS_PER_CRON", raising=False)
    monkeypatch.delenv("HOMELAB_MONITOR_CRON_RUN_BMODE_TIMEOUT_HOURS", raising=False)
    monkeypatch.setenv("HOMELAB_MONITOR_CRON_RUN_ENRICH_GRACE_SECONDS", "60")
    cfg = load_cron_run_reconciler_config()
    assert cfg.enrich_grace_seconds == 60  # noqa: PLR2004


def test_cron_run_reconciler_config_all_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    """All six reconciler env vars override all fields simultaneously."""
    from homelab_monitor.kernel.config import load_cron_run_reconciler_config  # noqa: PLC0415

    monkeypatch.setenv("HOMELAB_MONITOR_CRON_RUN_RETENTION_DAYS", "14")
    monkeypatch.setenv("HOMELAB_MONITOR_CRON_RUN_MAX_ROWS_PER_CRON", "200")
    monkeypatch.setenv("HOMELAB_MONITOR_CRON_RUN_BMODE_TIMEOUT_HOURS", "2")
    monkeypatch.setenv("HOMELAB_MONITOR_CRON_RUN_ENRICH_GRACE_SECONDS", "30")
    monkeypatch.setenv("HOMELAB_MONITOR_CRON_RUN_ENRICH_MAX_PER_TICK", "100")
    monkeypatch.setenv("HOMELAB_MONITOR_CRON_RUN_ENRICH_WINDOW_SLACK_SECONDS", "45")
    cfg = load_cron_run_reconciler_config()
    assert cfg.retention_days == 14  # noqa: PLR2004
    assert cfg.max_rows_per_cron == 200  # noqa: PLR2004
    assert cfg.bmode_timeout_hours == 2  # noqa: PLR2004
    assert cfg.enrich_grace_seconds == 30  # noqa: PLR2004
    assert cfg.enrich_max_per_tick == 100  # noqa: PLR2004
    assert cfg.enrich_window_slack_seconds == 45  # noqa: PLR2004


def test_load_cron_run_reconciler_config_enrich_max_per_tick_default_and_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default and env override for enrich_max_per_tick."""
    from homelab_monitor.kernel.config import load_cron_run_reconciler_config  # noqa: PLC0415

    # Test default: env var not set
    monkeypatch.delenv("HOMELAB_MONITOR_CRON_RUN_ENRICH_MAX_PER_TICK", raising=False)
    monkeypatch.delenv("HOMELAB_MONITOR_CRON_RUN_RETENTION_DAYS", raising=False)
    monkeypatch.delenv("HOMELAB_MONITOR_CRON_RUN_MAX_ROWS_PER_CRON", raising=False)
    monkeypatch.delenv("HOMELAB_MONITOR_CRON_RUN_BMODE_TIMEOUT_HOURS", raising=False)
    monkeypatch.delenv("HOMELAB_MONITOR_CRON_RUN_ENRICH_GRACE_SECONDS", raising=False)
    cfg = load_cron_run_reconciler_config()
    assert cfg.enrich_max_per_tick == 200  # noqa: PLR2004

    # Test override: env var set
    monkeypatch.setenv("HOMELAB_MONITOR_CRON_RUN_ENRICH_MAX_PER_TICK", "50")
    cfg = load_cron_run_reconciler_config()
    assert cfg.enrich_max_per_tick == 50  # noqa: PLR2004


def test_load_cron_run_reconciler_config_enrich_window_slack_seconds_default_and_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default and env override for enrich_window_slack_seconds."""
    from homelab_monitor.kernel.config import load_cron_run_reconciler_config  # noqa: PLC0415

    # Test default: env var not set
    monkeypatch.delenv("HOMELAB_MONITOR_CRON_RUN_ENRICH_WINDOW_SLACK_SECONDS", raising=False)
    monkeypatch.delenv("HOMELAB_MONITOR_CRON_RUN_RETENTION_DAYS", raising=False)
    monkeypatch.delenv("HOMELAB_MONITOR_CRON_RUN_MAX_ROWS_PER_CRON", raising=False)
    monkeypatch.delenv("HOMELAB_MONITOR_CRON_RUN_BMODE_TIMEOUT_HOURS", raising=False)
    monkeypatch.delenv("HOMELAB_MONITOR_CRON_RUN_ENRICH_GRACE_SECONDS", raising=False)
    monkeypatch.delenv("HOMELAB_MONITOR_CRON_RUN_ENRICH_MAX_PER_TICK", raising=False)
    cfg = load_cron_run_reconciler_config()
    assert cfg.enrich_window_slack_seconds == 30  # noqa: PLR2004

    # Test override: env var set
    monkeypatch.setenv("HOMELAB_MONITOR_CRON_RUN_ENRICH_WINDOW_SLACK_SECONDS", "90")
    cfg = load_cron_run_reconciler_config()
    assert cfg.enrich_window_slack_seconds == 90  # noqa: PLR2004


# ---------------------------------------------------------------------------
# STAGE-002-014: CronAnomalyConfig + load_vl_retention_days
# ---------------------------------------------------------------------------


def test_load_cron_anomaly_config_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """load_cron_anomaly_config returns documented defaults when no env vars set."""
    from homelab_monitor.kernel.config import (  # noqa: PLC0415
        CronAnomalyConfig,
        load_cron_anomaly_config,
    )

    for var in (
        "HOMELAB_MONITOR_CRON_ANOMALY_MIN_HISTORY",
        "HOMELAB_MONITOR_CRON_ANOMALY_ROLLING_WINDOW",
        "HOMELAB_MONITOR_CRON_ANOMALY_DURATION_K",
        "HOMELAB_MONITOR_CRON_ANOMALY_OUTPUT_BAND",
    ):
        monkeypatch.delenv(var, raising=False)

    cfg = load_cron_anomaly_config()
    defaults = CronAnomalyConfig()
    assert cfg.min_history == defaults.min_history
    assert cfg.rolling_window == defaults.rolling_window
    assert cfg.duration_k == defaults.duration_k
    assert cfg.output_band == defaults.output_band

    # Verify the documented values per spec
    assert cfg.min_history == 10  # noqa: PLR2004
    assert cfg.rolling_window == 20  # noqa: PLR2004
    assert cfg.duration_k == 4.0  # noqa: PLR2004
    assert cfg.output_band == 0.5  # noqa: PLR2004


def test_load_cron_anomaly_config_min_history_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """HOMELAB_MONITOR_CRON_ANOMALY_MIN_HISTORY overrides min_history only."""
    from homelab_monitor.kernel.config import load_cron_anomaly_config  # noqa: PLC0415

    monkeypatch.setenv("HOMELAB_MONITOR_CRON_ANOMALY_MIN_HISTORY", "5")
    monkeypatch.delenv("HOMELAB_MONITOR_CRON_ANOMALY_ROLLING_WINDOW", raising=False)
    monkeypatch.delenv("HOMELAB_MONITOR_CRON_ANOMALY_DURATION_K", raising=False)
    monkeypatch.delenv("HOMELAB_MONITOR_CRON_ANOMALY_OUTPUT_BAND", raising=False)

    cfg = load_cron_anomaly_config()
    assert cfg.min_history == 5  # noqa: PLR2004
    assert cfg.rolling_window == 20  # noqa: PLR2004  -- default unchanged


def test_load_cron_anomaly_config_rolling_window_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """HOMELAB_MONITOR_CRON_ANOMALY_ROLLING_WINDOW overrides rolling_window only."""
    from homelab_monitor.kernel.config import load_cron_anomaly_config  # noqa: PLC0415

    monkeypatch.delenv("HOMELAB_MONITOR_CRON_ANOMALY_MIN_HISTORY", raising=False)
    monkeypatch.setenv("HOMELAB_MONITOR_CRON_ANOMALY_ROLLING_WINDOW", "50")
    monkeypatch.delenv("HOMELAB_MONITOR_CRON_ANOMALY_DURATION_K", raising=False)
    monkeypatch.delenv("HOMELAB_MONITOR_CRON_ANOMALY_OUTPUT_BAND", raising=False)

    cfg = load_cron_anomaly_config()
    assert cfg.rolling_window == 50  # noqa: PLR2004
    assert cfg.min_history == 10  # noqa: PLR2004  -- default unchanged


def test_load_cron_anomaly_config_duration_k_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """HOMELAB_MONITOR_CRON_ANOMALY_DURATION_K overrides duration_k only."""
    from homelab_monitor.kernel.config import load_cron_anomaly_config  # noqa: PLC0415

    monkeypatch.delenv("HOMELAB_MONITOR_CRON_ANOMALY_MIN_HISTORY", raising=False)
    monkeypatch.delenv("HOMELAB_MONITOR_CRON_ANOMALY_ROLLING_WINDOW", raising=False)
    monkeypatch.setenv("HOMELAB_MONITOR_CRON_ANOMALY_DURATION_K", "2.5")
    monkeypatch.delenv("HOMELAB_MONITOR_CRON_ANOMALY_OUTPUT_BAND", raising=False)

    cfg = load_cron_anomaly_config()
    assert cfg.duration_k == 2.5  # noqa: PLR2004
    assert cfg.output_band == 0.5  # noqa: PLR2004  -- default unchanged


def test_load_cron_anomaly_config_output_band_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """HOMELAB_MONITOR_CRON_ANOMALY_OUTPUT_BAND overrides output_band only."""
    from homelab_monitor.kernel.config import load_cron_anomaly_config  # noqa: PLC0415

    monkeypatch.delenv("HOMELAB_MONITOR_CRON_ANOMALY_MIN_HISTORY", raising=False)
    monkeypatch.delenv("HOMELAB_MONITOR_CRON_ANOMALY_ROLLING_WINDOW", raising=False)
    monkeypatch.delenv("HOMELAB_MONITOR_CRON_ANOMALY_DURATION_K", raising=False)
    monkeypatch.setenv("HOMELAB_MONITOR_CRON_ANOMALY_OUTPUT_BAND", "0.25")

    cfg = load_cron_anomaly_config()
    assert cfg.output_band == 0.25  # noqa: PLR2004
    assert cfg.duration_k == 4.0  # noqa: PLR2004  -- default unchanged


def test_load_cron_anomaly_config_all_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    """All four CRON_ANOMALY env vars override all four fields simultaneously."""
    from homelab_monitor.kernel.config import load_cron_anomaly_config  # noqa: PLC0415

    monkeypatch.setenv("HOMELAB_MONITOR_CRON_ANOMALY_MIN_HISTORY", "3")
    monkeypatch.setenv("HOMELAB_MONITOR_CRON_ANOMALY_ROLLING_WINDOW", "15")
    monkeypatch.setenv("HOMELAB_MONITOR_CRON_ANOMALY_DURATION_K", "3.0")
    monkeypatch.setenv("HOMELAB_MONITOR_CRON_ANOMALY_OUTPUT_BAND", "0.75")

    cfg = load_cron_anomaly_config()
    assert cfg.min_history == 3  # noqa: PLR2004
    assert cfg.rolling_window == 15  # noqa: PLR2004
    assert cfg.duration_k == 3.0  # noqa: PLR2004
    assert cfg.output_band == 0.75  # noqa: PLR2004


def test_load_vl_retention_days_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """load_vl_retention_days returns 30 when env var is absent."""
    from homelab_monitor.kernel.config import load_vl_retention_days  # noqa: PLC0415

    monkeypatch.delenv("HOMELAB_MONITOR_VL_RETENTION_DAYS", raising=False)
    assert load_vl_retention_days() == 30  # noqa: PLR2004


def test_load_vl_retention_days_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """HOMELAB_MONITOR_VL_RETENTION_DAYS overrides the default."""
    from homelab_monitor.kernel.config import load_vl_retention_days  # noqa: PLC0415

    monkeypatch.setenv("HOMELAB_MONITOR_VL_RETENTION_DAYS", "14")
    assert load_vl_retention_days() == 14  # noqa: PLR2004


# --- TailConfig / load_tail_config (STAGE-004-023) ------------------------------------------


def test_load_tail_config_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """load_tail_config returns built-in defaults when no env vars are set."""
    from homelab_monitor.kernel.config import TailConfig, load_tail_config  # noqa: PLC0415

    monkeypatch.delenv("HOMELAB_MONITOR_TAIL_POLL_MS", raising=False)
    monkeypatch.delenv("HOMELAB_MONITOR_TAIL_MAX_CONNECTIONS", raising=False)
    monkeypatch.delenv("HOMELAB_MONITOR_TAIL_MAX_LINES_PER_SEC", raising=False)
    monkeypatch.delenv("HOMELAB_MONITOR_TAIL_MAX_DURATION_S", raising=False)
    cfg = load_tail_config()
    assert cfg == TailConfig()
    assert cfg.poll_ms == 1000  # noqa: PLR2004
    assert cfg.max_connections == 5  # noqa: PLR2004
    assert cfg.max_lines_per_sec == 200  # noqa: PLR2004
    assert cfg.max_duration_s == 3600  # noqa: PLR2004


def test_load_tail_config_env_max_connections(monkeypatch: pytest.MonkeyPatch) -> None:
    """HOMELAB_MONITOR_TAIL_MAX_CONNECTIONS overrides max_connections (line 235)."""
    from homelab_monitor.kernel.config import load_tail_config  # noqa: PLC0415

    monkeypatch.delenv("HOMELAB_MONITOR_TAIL_POLL_MS", raising=False)
    monkeypatch.setenv("HOMELAB_MONITOR_TAIL_MAX_CONNECTIONS", "10")
    monkeypatch.delenv("HOMELAB_MONITOR_TAIL_MAX_LINES_PER_SEC", raising=False)
    monkeypatch.delenv("HOMELAB_MONITOR_TAIL_MAX_DURATION_S", raising=False)
    cfg = load_tail_config()
    assert cfg.max_connections == 10  # noqa: PLR2004


def test_load_tail_config_env_max_lines_per_sec(monkeypatch: pytest.MonkeyPatch) -> None:
    """HOMELAB_MONITOR_TAIL_MAX_LINES_PER_SEC overrides max_lines_per_sec (line 238)."""
    from homelab_monitor.kernel.config import load_tail_config  # noqa: PLC0415

    monkeypatch.delenv("HOMELAB_MONITOR_TAIL_POLL_MS", raising=False)
    monkeypatch.delenv("HOMELAB_MONITOR_TAIL_MAX_CONNECTIONS", raising=False)
    monkeypatch.setenv("HOMELAB_MONITOR_TAIL_MAX_LINES_PER_SEC", "500")
    monkeypatch.delenv("HOMELAB_MONITOR_TAIL_MAX_DURATION_S", raising=False)
    cfg = load_tail_config()
    assert cfg.max_lines_per_sec == 500  # noqa: PLR2004


def test_load_tail_config_all_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    """All four TAIL env vars override all four fields simultaneously."""
    from homelab_monitor.kernel.config import load_tail_config  # noqa: PLC0415

    monkeypatch.setenv("HOMELAB_MONITOR_TAIL_POLL_MS", "500")
    monkeypatch.setenv("HOMELAB_MONITOR_TAIL_MAX_CONNECTIONS", "10")
    monkeypatch.setenv("HOMELAB_MONITOR_TAIL_MAX_LINES_PER_SEC", "500")
    monkeypatch.setenv("HOMELAB_MONITOR_TAIL_MAX_DURATION_S", "7200")
    cfg = load_tail_config()
    assert cfg.poll_ms == 500  # noqa: PLR2004
    assert cfg.max_connections == 10  # noqa: PLR2004
    assert cfg.max_lines_per_sec == 500  # noqa: PLR2004
    assert cfg.max_duration_s == 7200  # noqa: PLR2004


def test_load_tail_config_clamps_zero_to_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """All four TAIL env vars set to 0 are clamped up to the floor of 1."""
    from homelab_monitor.kernel.config import load_tail_config  # noqa: PLC0415

    monkeypatch.setenv("HOMELAB_MONITOR_TAIL_POLL_MS", "0")
    monkeypatch.setenv("HOMELAB_MONITOR_TAIL_MAX_CONNECTIONS", "0")
    monkeypatch.setenv("HOMELAB_MONITOR_TAIL_MAX_LINES_PER_SEC", "0")
    monkeypatch.setenv("HOMELAB_MONITOR_TAIL_MAX_DURATION_S", "0")
    cfg = load_tail_config()
    assert cfg.poll_ms == 1
    assert cfg.max_connections == 1
    assert cfg.max_lines_per_sec == 1
    assert cfg.max_duration_s == 1
