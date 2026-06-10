"""Tests for :func:`load_disk_budget_config`."""

from __future__ import annotations

from pathlib import Path

import pytest

from homelab_monitor.kernel.config import (
    DEFAULT_ERROR_PATTERNS,
    CardinalityCapsConfig,
    CronAnomalyConfig,
    CronRunReconcilerConfig,
    DiskBudgetConfig,
    DrainConfig,
    ErrorPattern,
    ErrorRateOverride,
    LogsConfig,
    LogStreamBudgetConfig,
    NewSignatureConfig,
    SeverityFloor,
    SilenceDetectionConfig,
    TailConfig,
    VlHealthConfig,
    VlQueryLimits,
    load_cardinality_caps_config,
    load_cron_anomaly_config,
    load_cron_run_reconciler_config,
    load_disk_budget_config,
    load_drain_config,
    load_log_stream_budget_config,
    load_logs_config,
    load_new_signature_config,
    load_silence_detection_config,
    load_tail_config,
    load_vl_health_config,
    load_vl_query_limits,
    load_vl_retention_days,
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


def test_log_stream_budget_rejects_malformed_lines_per_sec(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Non-numeric lines_per_sec_per_stream raises ValueError with context."""
    cfg_file = tmp_path / "bad.yaml"
    cfg_file.write_text("log_stream_budget:\n  lines_per_sec_per_stream: not-a-number\n")
    monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(cfg_file))
    with pytest.raises(ValueError, match="lines_per_sec_per_stream must be a number"):
        load_log_stream_budget_config()


def test_log_stream_budget_rejects_malformed_bytes_per_day(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Non-integer bytes_per_day_per_stream raises ValueError with context."""
    cfg_file = tmp_path / "bad.yaml"
    cfg_file.write_text("log_stream_budget:\n  bytes_per_day_per_stream: not-an-int\n")
    monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(cfg_file))
    with pytest.raises(ValueError, match="bytes_per_day_per_stream must be an integer"):
        load_log_stream_budget_config()


# --- VlQueryLimits ---------------------------------------------------------------


def test_vl_query_limits_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """load_vl_query_limits returns built-in defaults when no env vars are set."""
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
    monkeypatch.setenv("HOMELAB_MONITOR_VL_QUERY_MAX_LINES", "500")
    monkeypatch.delenv("HOMELAB_MONITOR_VL_QUERY_MAX_BYTES", raising=False)
    monkeypatch.delenv("HOMELAB_MONITOR_VL_QUERY_TIMEOUT_SECONDS", raising=False)
    cfg = load_vl_query_limits()
    assert cfg.max_lines == 500  # noqa: PLR2004


def test_vl_query_limits_env_max_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    """HOMELAB_MONITOR_VL_QUERY_MAX_BYTES overrides max_bytes."""
    monkeypatch.delenv("HOMELAB_MONITOR_VL_QUERY_MAX_LINES", raising=False)
    monkeypatch.setenv("HOMELAB_MONITOR_VL_QUERY_MAX_BYTES", "123456")
    monkeypatch.delenv("HOMELAB_MONITOR_VL_QUERY_TIMEOUT_SECONDS", raising=False)
    cfg = load_vl_query_limits()
    assert cfg.max_bytes == 123456  # noqa: PLR2004


def test_vl_query_limits_env_timeout_seconds(monkeypatch: pytest.MonkeyPatch) -> None:
    """HOMELAB_MONITOR_VL_QUERY_TIMEOUT_SECONDS overrides timeout_seconds."""
    monkeypatch.delenv("HOMELAB_MONITOR_VL_QUERY_MAX_LINES", raising=False)
    monkeypatch.delenv("HOMELAB_MONITOR_VL_QUERY_MAX_BYTES", raising=False)
    monkeypatch.setenv("HOMELAB_MONITOR_VL_QUERY_TIMEOUT_SECONDS", "3.5")
    cfg = load_vl_query_limits()
    assert cfg.timeout_seconds == 3.5  # noqa: PLR2004


def test_vl_query_limits_all_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    """All three VL query limit env vars override all fields simultaneously."""
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
    monkeypatch.setenv("HOMELAB_MONITOR_CRON_RUN_RETENTION_DAYS", "7")
    monkeypatch.delenv("HOMELAB_MONITOR_CRON_RUN_MAX_ROWS_PER_CRON", raising=False)
    monkeypatch.delenv("HOMELAB_MONITOR_CRON_RUN_BMODE_TIMEOUT_HOURS", raising=False)
    monkeypatch.delenv("HOMELAB_MONITOR_CRON_RUN_ENRICH_GRACE_SECONDS", raising=False)
    cfg = load_cron_run_reconciler_config()
    assert cfg.retention_days == 7  # noqa: PLR2004


def test_cron_run_reconciler_config_env_max_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    """HOMELAB_MONITOR_CRON_RUN_MAX_ROWS_PER_CRON overrides max_rows_per_cron."""
    monkeypatch.delenv("HOMELAB_MONITOR_CRON_RUN_RETENTION_DAYS", raising=False)
    monkeypatch.setenv("HOMELAB_MONITOR_CRON_RUN_MAX_ROWS_PER_CRON", "1000")
    monkeypatch.delenv("HOMELAB_MONITOR_CRON_RUN_BMODE_TIMEOUT_HOURS", raising=False)
    monkeypatch.delenv("HOMELAB_MONITOR_CRON_RUN_ENRICH_GRACE_SECONDS", raising=False)
    cfg = load_cron_run_reconciler_config()
    assert cfg.max_rows_per_cron == 1000  # noqa: PLR2004


def test_cron_run_reconciler_config_env_bmode_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """HOMELAB_MONITOR_CRON_RUN_BMODE_TIMEOUT_HOURS overrides bmode_timeout_hours."""
    monkeypatch.delenv("HOMELAB_MONITOR_CRON_RUN_RETENTION_DAYS", raising=False)
    monkeypatch.delenv("HOMELAB_MONITOR_CRON_RUN_MAX_ROWS_PER_CRON", raising=False)
    monkeypatch.setenv("HOMELAB_MONITOR_CRON_RUN_BMODE_TIMEOUT_HOURS", "12")
    monkeypatch.delenv("HOMELAB_MONITOR_CRON_RUN_ENRICH_GRACE_SECONDS", raising=False)
    cfg = load_cron_run_reconciler_config()
    assert cfg.bmode_timeout_hours == 12  # noqa: PLR2004


def test_cron_run_reconciler_config_env_enrich_grace(monkeypatch: pytest.MonkeyPatch) -> None:
    """HOMELAB_MONITOR_CRON_RUN_ENRICH_GRACE_SECONDS overrides enrich_grace_seconds."""
    monkeypatch.delenv("HOMELAB_MONITOR_CRON_RUN_RETENTION_DAYS", raising=False)
    monkeypatch.delenv("HOMELAB_MONITOR_CRON_RUN_MAX_ROWS_PER_CRON", raising=False)
    monkeypatch.delenv("HOMELAB_MONITOR_CRON_RUN_BMODE_TIMEOUT_HOURS", raising=False)
    monkeypatch.setenv("HOMELAB_MONITOR_CRON_RUN_ENRICH_GRACE_SECONDS", "60")
    cfg = load_cron_run_reconciler_config()
    assert cfg.enrich_grace_seconds == 60  # noqa: PLR2004


def test_cron_run_reconciler_config_all_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    """All six reconciler env vars override all fields simultaneously."""
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
# STAGE-004-034: CronRunReconcilerConfig — new failure-enrich fields
# ---------------------------------------------------------------------------


def test_cron_run_reconciler_config_failure_enrich_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """New failure-enrich fields have correct defaults when no env vars are set."""
    monkeypatch.delenv("HOMELAB_MONITOR_CRON_ENRICHMENT_RETENTION_DAYS", raising=False)
    monkeypatch.delenv("HOMELAB_MONITOR_CRON_FAILURE_ENRICH_MAX_LINES", raising=False)
    monkeypatch.delenv("HOMELAB_MONITOR_CRON_FAILURE_ENRICH_MAX_ROWS_PER_CRON", raising=False)

    cfg = load_cron_run_reconciler_config()
    assert cfg.cron_failure_enrich_max_lines == 50  # noqa: PLR2004
    assert cfg.cron_failure_enrich_retention_days == 30  # noqa: PLR2004
    assert cfg.cron_failure_enrich_max_rows_per_cron == 100  # noqa: PLR2004


def test_cron_run_reconciler_config_failure_enrich_env_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Env vars for new failure-enrich fields override defaults."""
    monkeypatch.setenv("HOMELAB_MONITOR_CRON_ENRICHMENT_RETENTION_DAYS", "14")
    monkeypatch.setenv("HOMELAB_MONITOR_CRON_FAILURE_ENRICH_MAX_LINES", "25")
    monkeypatch.setenv("HOMELAB_MONITOR_CRON_FAILURE_ENRICH_MAX_ROWS_PER_CRON", "200")

    cfg = load_cron_run_reconciler_config()
    assert cfg.cron_failure_enrich_retention_days == 14  # noqa: PLR2004
    assert cfg.cron_failure_enrich_max_lines == 25  # noqa: PLR2004
    assert cfg.cron_failure_enrich_max_rows_per_cron == 200  # noqa: PLR2004


def test_cron_run_reconciler_config_failure_enrich_clamping_ge_1(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Values of 0 or negative are clamped to 1 for all three failure-enrich fields."""
    monkeypatch.setenv("HOMELAB_MONITOR_CRON_ENRICHMENT_RETENTION_DAYS", "0")
    monkeypatch.setenv("HOMELAB_MONITOR_CRON_FAILURE_ENRICH_MAX_LINES", "-5")
    monkeypatch.setenv("HOMELAB_MONITOR_CRON_FAILURE_ENRICH_MAX_ROWS_PER_CRON", "0")

    cfg = load_cron_run_reconciler_config()
    assert cfg.cron_failure_enrich_retention_days == 1
    assert cfg.cron_failure_enrich_max_lines == 1
    assert cfg.cron_failure_enrich_max_rows_per_cron == 1


# ---------------------------------------------------------------------------
# STAGE-002-014: CronAnomalyConfig + load_vl_retention_days
# ---------------------------------------------------------------------------


def test_load_cron_anomaly_config_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """load_cron_anomaly_config returns documented defaults when no env vars set."""
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
    monkeypatch.setenv("HOMELAB_MONITOR_CRON_ANOMALY_MIN_HISTORY", "5")
    monkeypatch.delenv("HOMELAB_MONITOR_CRON_ANOMALY_ROLLING_WINDOW", raising=False)
    monkeypatch.delenv("HOMELAB_MONITOR_CRON_ANOMALY_DURATION_K", raising=False)
    monkeypatch.delenv("HOMELAB_MONITOR_CRON_ANOMALY_OUTPUT_BAND", raising=False)

    cfg = load_cron_anomaly_config()
    assert cfg.min_history == 5  # noqa: PLR2004
    assert cfg.rolling_window == 20  # noqa: PLR2004  -- default unchanged


def test_load_cron_anomaly_config_rolling_window_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """HOMELAB_MONITOR_CRON_ANOMALY_ROLLING_WINDOW overrides rolling_window only."""
    monkeypatch.delenv("HOMELAB_MONITOR_CRON_ANOMALY_MIN_HISTORY", raising=False)
    monkeypatch.setenv("HOMELAB_MONITOR_CRON_ANOMALY_ROLLING_WINDOW", "50")
    monkeypatch.delenv("HOMELAB_MONITOR_CRON_ANOMALY_DURATION_K", raising=False)
    monkeypatch.delenv("HOMELAB_MONITOR_CRON_ANOMALY_OUTPUT_BAND", raising=False)

    cfg = load_cron_anomaly_config()
    assert cfg.rolling_window == 50  # noqa: PLR2004
    assert cfg.min_history == 10  # noqa: PLR2004  -- default unchanged


def test_load_cron_anomaly_config_duration_k_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """HOMELAB_MONITOR_CRON_ANOMALY_DURATION_K overrides duration_k only."""
    monkeypatch.delenv("HOMELAB_MONITOR_CRON_ANOMALY_MIN_HISTORY", raising=False)
    monkeypatch.delenv("HOMELAB_MONITOR_CRON_ANOMALY_ROLLING_WINDOW", raising=False)
    monkeypatch.setenv("HOMELAB_MONITOR_CRON_ANOMALY_DURATION_K", "2.5")
    monkeypatch.delenv("HOMELAB_MONITOR_CRON_ANOMALY_OUTPUT_BAND", raising=False)

    cfg = load_cron_anomaly_config()
    assert cfg.duration_k == 2.5  # noqa: PLR2004
    assert cfg.output_band == 0.5  # noqa: PLR2004  -- default unchanged


def test_load_cron_anomaly_config_output_band_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """HOMELAB_MONITOR_CRON_ANOMALY_OUTPUT_BAND overrides output_band only."""
    monkeypatch.delenv("HOMELAB_MONITOR_CRON_ANOMALY_MIN_HISTORY", raising=False)
    monkeypatch.delenv("HOMELAB_MONITOR_CRON_ANOMALY_ROLLING_WINDOW", raising=False)
    monkeypatch.delenv("HOMELAB_MONITOR_CRON_ANOMALY_DURATION_K", raising=False)
    monkeypatch.setenv("HOMELAB_MONITOR_CRON_ANOMALY_OUTPUT_BAND", "0.25")

    cfg = load_cron_anomaly_config()
    assert cfg.output_band == 0.25  # noqa: PLR2004
    assert cfg.duration_k == 4.0  # noqa: PLR2004  -- default unchanged


def test_load_cron_anomaly_config_all_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    """All four CRON_ANOMALY env vars override all four fields simultaneously."""
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
    monkeypatch.delenv("HOMELAB_MONITOR_VL_RETENTION_DAYS", raising=False)
    assert load_vl_retention_days() == 30  # noqa: PLR2004


def test_load_vl_retention_days_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """HOMELAB_MONITOR_VL_RETENTION_DAYS overrides the default."""
    monkeypatch.setenv("HOMELAB_MONITOR_VL_RETENTION_DAYS", "14")
    assert load_vl_retention_days() == 14  # noqa: PLR2004


# --- TailConfig / load_tail_config (STAGE-004-023) ------------------------------------------


def test_load_tail_config_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """load_tail_config returns built-in defaults when no env vars are set."""
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
    monkeypatch.delenv("HOMELAB_MONITOR_TAIL_POLL_MS", raising=False)
    monkeypatch.setenv("HOMELAB_MONITOR_TAIL_MAX_CONNECTIONS", "10")
    monkeypatch.delenv("HOMELAB_MONITOR_TAIL_MAX_LINES_PER_SEC", raising=False)
    monkeypatch.delenv("HOMELAB_MONITOR_TAIL_MAX_DURATION_S", raising=False)
    cfg = load_tail_config()
    assert cfg.max_connections == 10  # noqa: PLR2004


def test_load_tail_config_env_max_lines_per_sec(monkeypatch: pytest.MonkeyPatch) -> None:
    """HOMELAB_MONITOR_TAIL_MAX_LINES_PER_SEC overrides max_lines_per_sec (line 238)."""
    monkeypatch.delenv("HOMELAB_MONITOR_TAIL_POLL_MS", raising=False)
    monkeypatch.delenv("HOMELAB_MONITOR_TAIL_MAX_CONNECTIONS", raising=False)
    monkeypatch.setenv("HOMELAB_MONITOR_TAIL_MAX_LINES_PER_SEC", "500")
    monkeypatch.delenv("HOMELAB_MONITOR_TAIL_MAX_DURATION_S", raising=False)
    cfg = load_tail_config()
    assert cfg.max_lines_per_sec == 500  # noqa: PLR2004


def test_load_tail_config_all_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    """All four TAIL env vars override all four fields simultaneously."""
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
    monkeypatch.setenv("HOMELAB_MONITOR_TAIL_POLL_MS", "0")
    monkeypatch.setenv("HOMELAB_MONITOR_TAIL_MAX_CONNECTIONS", "0")
    monkeypatch.setenv("HOMELAB_MONITOR_TAIL_MAX_LINES_PER_SEC", "0")
    monkeypatch.setenv("HOMELAB_MONITOR_TAIL_MAX_DURATION_S", "0")
    cfg = load_tail_config()
    assert cfg.poll_ms == 1
    assert cfg.max_connections == 1
    assert cfg.max_lines_per_sec == 1
    assert cfg.max_duration_s == 1


def test_load_drain_config_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HOMELAB_MONITOR_DRAIN_INTERVAL_S", raising=False)
    monkeypatch.delenv("HOMELAB_MONITOR_DRAIN_BATCH_MAX_LINES", raising=False)
    monkeypatch.delenv("HOMELAB_MONITOR_DRAIN_INGEST_LAG_GRACE_S", raising=False)
    monkeypatch.delenv("HOMELAB_MONITOR_DRAIN_ENABLED", raising=False)
    cfg = load_drain_config()
    assert cfg == DrainConfig()
    assert cfg.interval_seconds == 300  # noqa: PLR2004
    assert cfg.batch_max_lines == 50_000  # noqa: PLR2004
    assert cfg.ingest_lag_grace_seconds == 30  # noqa: PLR2004
    assert cfg.enabled is True


def test_load_drain_config_all_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOMELAB_MONITOR_DRAIN_INTERVAL_S", "60")
    monkeypatch.setenv("HOMELAB_MONITOR_DRAIN_BATCH_MAX_LINES", "1000")
    monkeypatch.setenv("HOMELAB_MONITOR_DRAIN_INGEST_LAG_GRACE_S", "5")
    monkeypatch.setenv("HOMELAB_MONITOR_DRAIN_ENABLED", "false")
    monkeypatch.setenv("HOMELAB_MONITOR_DRAIN_CARDINALITY_WARN", "250000")
    cfg = load_drain_config()
    assert cfg.interval_seconds == 60  # noqa: PLR2004
    assert cfg.batch_max_lines == 1000  # noqa: PLR2004
    assert cfg.ingest_lag_grace_seconds == 5  # noqa: PLR2004
    assert cfg.enabled is False
    assert cfg.signature_cardinality_warn_threshold == 250000  # noqa: PLR2004


def test_load_drain_config_clamps(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOMELAB_MONITOR_DRAIN_INTERVAL_S", "0")
    monkeypatch.setenv("HOMELAB_MONITOR_DRAIN_BATCH_MAX_LINES", "0")
    monkeypatch.setenv("HOMELAB_MONITOR_DRAIN_INGEST_LAG_GRACE_S", "-5")
    monkeypatch.delenv("HOMELAB_MONITOR_DRAIN_ENABLED", raising=False)
    cfg = load_drain_config()
    assert cfg.interval_seconds == 1
    assert cfg.batch_max_lines == 1
    assert cfg.ingest_lag_grace_seconds == 0


def test_load_drain_config_enabled_truthy_variants(monkeypatch: pytest.MonkeyPatch) -> None:
    for raw, expected in (("1", True), ("yes", True), ("TRUE", True), ("0", False), ("off", False)):
        monkeypatch.setenv("HOMELAB_MONITOR_DRAIN_ENABLED", raw)
        assert load_drain_config().enabled is expected


def test_load_drain_config_bad_int_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOMELAB_MONITOR_DRAIN_INTERVAL_S", "not-a-number")
    with pytest.raises(ValueError):
        load_drain_config()


# --- NewSignatureConfig (STAGE-004-035) ------------------------------------------


def test_load_new_signature_config_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """load_new_signature_config returns documented defaults when no env vars set."""
    monkeypatch.delenv("HOMELAB_MONITOR_NEW_SIGNATURE_WINDOW_S", raising=False)
    monkeypatch.delenv("HOMELAB_MONITOR_NEW_SIGNATURE_SEVERITIES", raising=False)

    cfg = load_new_signature_config()
    defaults = NewSignatureConfig()
    assert cfg.window_seconds == defaults.window_seconds
    assert cfg.severities == defaults.severities

    # Verify the documented values per spec
    assert cfg.window_seconds == 300  # noqa: PLR2004
    assert cfg.severities == frozenset({"error", "critical", "warning"})


def test_load_new_signature_config_window_clamp(monkeypatch: pytest.MonkeyPatch) -> None:
    """window_seconds of 0 or negative is clamped to 1."""
    monkeypatch.setenv("HOMELAB_MONITOR_NEW_SIGNATURE_WINDOW_S", "0")
    monkeypatch.delenv("HOMELAB_MONITOR_NEW_SIGNATURE_SEVERITIES", raising=False)

    cfg = load_new_signature_config()
    assert cfg.window_seconds == 1


def test_load_new_signature_config_window_negative_clamp(monkeypatch: pytest.MonkeyPatch) -> None:
    """Negative window_seconds is clamped to 1."""
    monkeypatch.setenv("HOMELAB_MONITOR_NEW_SIGNATURE_WINDOW_S", "-100")
    monkeypatch.delenv("HOMELAB_MONITOR_NEW_SIGNATURE_SEVERITIES", raising=False)

    cfg = load_new_signature_config()
    assert cfg.window_seconds == 1


def test_load_new_signature_config_severities_parse_and_lowercase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Severities are lowercased, stripped, and split on comma."""
    monkeypatch.delenv("HOMELAB_MONITOR_NEW_SIGNATURE_WINDOW_S", raising=False)
    monkeypatch.setenv("HOMELAB_MONITOR_NEW_SIGNATURE_SEVERITIES", "error, INFO ,debug")

    cfg = load_new_signature_config()
    assert cfg.severities == frozenset({"error", "info", "debug"})


def test_load_new_signature_config_severities_empty_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty or whitespace-only severities fall back to default."""
    monkeypatch.delenv("HOMELAB_MONITOR_NEW_SIGNATURE_WINDOW_S", raising=False)
    monkeypatch.setenv("HOMELAB_MONITOR_NEW_SIGNATURE_SEVERITIES", " , ")

    cfg = load_new_signature_config()
    # Should fall back to the default set (error, critical, warning)
    assert cfg.severities == frozenset({"error", "critical", "warning"})


def test_load_new_signature_config_severities_custom_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """Custom severities override the default set."""
    monkeypatch.delenv("HOMELAB_MONITOR_NEW_SIGNATURE_WINDOW_S", raising=False)
    monkeypatch.setenv("HOMELAB_MONITOR_NEW_SIGNATURE_SEVERITIES", "critical,error")

    cfg = load_new_signature_config()
    assert cfg.severities == frozenset({"critical", "error"})


def test_load_new_signature_config_window_non_numeric_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-numeric window_seconds env value raises ValueError."""
    monkeypatch.setenv("HOMELAB_MONITOR_NEW_SIGNATURE_WINDOW_S", "not-a-number")
    monkeypatch.delenv("HOMELAB_MONITOR_NEW_SIGNATURE_SEVERITIES", raising=False)

    with pytest.raises(ValueError):
        load_new_signature_config()


def test_load_new_signature_config_all_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both env vars override both fields simultaneously."""
    monkeypatch.setenv("HOMELAB_MONITOR_NEW_SIGNATURE_WINDOW_S", "600")
    monkeypatch.setenv("HOMELAB_MONITOR_NEW_SIGNATURE_SEVERITIES", "warning,notice")

    cfg = load_new_signature_config()
    assert cfg.window_seconds == 600  # noqa: PLR2004
    assert cfg.severities == frozenset({"warning", "notice"})


# --- LogsConfig (error-rate config) ---------------------------------------------------


def test_load_logs_config_defaults_no_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """No config file -> defaults (DEFAULT_ERROR_PATTERNS, no overrides)."""
    monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(tmp_path / "missing.yaml"))
    cfg = load_logs_config()
    assert cfg == LogsConfig(
        error_patterns=DEFAULT_ERROR_PATTERNS,
        error_rate_overrides=(),
    )


def test_load_logs_config_logs_section_absent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """YAML without logs section -> defaults."""
    cfg_file = tmp_path / "homelab-monitor.yaml"
    cfg_file.write_text("disk_budget:\n  total_gb: 50\n")
    monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(cfg_file))
    cfg = load_logs_config()
    assert cfg.error_patterns == DEFAULT_ERROR_PATTERNS
    assert cfg.error_rate_overrides == ()


def test_load_logs_config_error_patterns_absent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """logs: present but error_patterns absent -> defaults."""
    cfg_file = tmp_path / "homelab-monitor.yaml"
    cfg_file.write_text("logs:\n  redact: []\n")
    monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(cfg_file))
    cfg = load_logs_config()
    assert cfg.error_patterns == DEFAULT_ERROR_PATTERNS


def test_load_logs_config_error_patterns_parsed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """error_patterns entries are parsed into ErrorPattern instances."""
    cfg_file = tmp_path / "homelab-monitor.yaml"
    cfg_file.write_text(
        "logs:\n  error_patterns:\n    - kind: http5xx\n      regex: 'status[:=]\\s*5\\d{2}'\n"
    )
    monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(cfg_file))
    cfg = load_logs_config()
    assert cfg.error_patterns == (ErrorPattern(kind="http5xx", regex="status[:=]\\s*5\\d{2}"),)


def test_load_logs_config_error_patterns_empty_list(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """error_patterns: [] (explicit empty) -> empty tuple (NOT defaults)."""
    cfg_file = tmp_path / "homelab-monitor.yaml"
    cfg_file.write_text("logs:\n  error_patterns: []\n")
    monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(cfg_file))
    cfg = load_logs_config()
    assert cfg.error_patterns == ()


def test_load_logs_config_error_patterns_empty_value(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """error_patterns: (null value) -> defaults."""
    cfg_file = tmp_path / "homelab-monitor.yaml"
    cfg_file.write_text("logs:\n  error_patterns:\n")
    monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(cfg_file))
    cfg = load_logs_config()
    assert cfg.error_patterns == DEFAULT_ERROR_PATTERNS


def test_load_logs_config_error_pattern_missing_field_raises(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """error_patterns entry missing regex field raises ValueError."""
    cfg_file = tmp_path / "homelab-monitor.yaml"
    cfg_file.write_text("logs:\n  error_patterns:\n    - kind: panic\n")
    monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(cfg_file))
    with pytest.raises(ValueError, match="regex"):
        load_logs_config()


def test_load_logs_config_error_pattern_not_mapping_raises(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """error_patterns entry that is not a mapping raises ValueError."""
    cfg_file = tmp_path / "homelab-monitor.yaml"
    cfg_file.write_text("logs:\n  error_patterns: ['panic']\n")
    monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(cfg_file))
    with pytest.raises(ValueError, match="must be a mapping"):
        load_logs_config()


def test_load_logs_config_error_patterns_not_list_raises(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """error_patterns that is not a list raises ValueError."""
    cfg_file = tmp_path / "homelab-monitor.yaml"
    cfg_file.write_text("logs:\n  error_patterns: foo\n")
    monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(cfg_file))
    with pytest.raises(ValueError, match="must be a list"):
        load_logs_config()


def test_load_logs_config_overrides_parsed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """error_rate_overrides entries are parsed."""
    cfg_file = tmp_path / "homelab-monitor.yaml"
    cfg_file.write_text(
        "logs:\n"
        "  error_patterns: []\n"
        "  error_rate_overrides:\n"
        "    - service: noisy\n"
        "      static_floor: 100\n"
        "    - service: critical\n"
        "      multiplier: 2\n"
    )
    monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(cfg_file))
    cfg = load_logs_config()
    assert cfg.error_rate_overrides == (
        ErrorRateOverride(service="noisy", static_floor=100.0, multiplier=None),
        ErrorRateOverride(service="critical", static_floor=None, multiplier=2.0),
    )


def test_load_logs_config_overrides_absent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """error_rate_overrides absent -> empty tuple."""
    cfg_file = tmp_path / "homelab-monitor.yaml"
    cfg_file.write_text("logs:\n  error_patterns: []\n")
    monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(cfg_file))
    cfg = load_logs_config()
    assert cfg.error_rate_overrides == ()


def test_load_logs_config_override_missing_service_raises(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """error_rate_overrides entry missing service raises ValueError."""
    cfg_file = tmp_path / "homelab-monitor.yaml"
    cfg_file.write_text("logs:\n  error_rate_overrides:\n    - static_floor: 5\n")
    monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(cfg_file))
    with pytest.raises(ValueError, match="service"):
        load_logs_config()


def test_load_logs_config_override_non_numeric_floor_raises(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """error_rate_overrides entry with non-numeric static_floor raises ValueError."""
    cfg_file = tmp_path / "homelab-monitor.yaml"
    cfg_file.write_text(
        "logs:\n  error_rate_overrides:\n    - service: x\n      static_floor: big\n"
    )
    monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(cfg_file))
    with pytest.raises(ValueError, match="must be numeric"):
        load_logs_config()


def test_load_logs_config_override_bool_floor_raises(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A boolean static_floor raises ValueError (bool is an int subclass — must be rejected)."""
    cfg_file = tmp_path / "homelab-monitor.yaml"
    cfg_file.write_text(
        "logs:\n  error_rate_overrides:\n    - service: svc\n      static_floor: true\n"
    )
    monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(cfg_file))
    with pytest.raises(ValueError, match="must be numeric"):
        load_logs_config()


def test_load_logs_config_logs_not_mapping_raises(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """logs: value that is not a mapping raises ValueError."""
    cfg_file = tmp_path / "homelab-monitor.yaml"
    cfg_file.write_text("logs: [a, b]\n")
    monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(cfg_file))
    with pytest.raises(ValueError, match="logs must be a mapping"):
        load_logs_config()


def test_load_logs_config_root_not_mapping_raises(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """YAML root that is not a mapping raises ValueError."""
    cfg_file = tmp_path / "homelab-monitor.yaml"
    cfg_file.write_text("- a\n- b\n")
    monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(cfg_file))
    with pytest.raises(ValueError, match="config root must be a mapping"):
        load_logs_config()


def test_load_logs_config_overrides_null_returns_empty(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """error_rate_overrides: null (explicit null value) -> empty tuple."""
    cfg_file = tmp_path / "homelab-monitor.yaml"
    cfg_file.write_text("logs:\n  error_rate_overrides:\n")
    monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(cfg_file))
    cfg = load_logs_config()
    assert cfg.error_rate_overrides == ()


def test_load_logs_config_overrides_not_list_raises(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """error_rate_overrides that is not a list raises ValueError."""
    cfg_file = tmp_path / "homelab-monitor.yaml"
    cfg_file.write_text("logs:\n  error_rate_overrides:\n    foo: bar\n")
    monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(cfg_file))
    with pytest.raises(ValueError, match="must be a list"):
        load_logs_config()


def test_load_logs_config_override_entry_not_mapping_raises(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """error_rate_overrides list entry that is not a mapping raises ValueError."""
    cfg_file = tmp_path / "homelab-monitor.yaml"
    cfg_file.write_text("logs:\n  error_rate_overrides:\n    - just a string\n")
    monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(cfg_file))
    with pytest.raises(ValueError, match="must be a mapping"):
        load_logs_config()


# --- severity_escalation config (STAGE-004-039) ----------------------------------


def test_load_logs_config_severity_escalation_parsed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """severity_escalation.excluded_services and severity_floors are parsed."""
    cfg_file = tmp_path / "homelab-monitor.yaml"
    cfg_file.write_text(
        "logs:\n"
        "  severity_escalation:\n"
        "    excluded_services:\n"
        "      - noisy-svc\n"
        "      - dev-debug\n"
        "    severity_floors:\n"
        "      - service: my-svc\n"
        "        floor: error\n"
        "      - service: other-svc\n"
        "        floor: warning\n"
    )
    monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(cfg_file))
    cfg = load_logs_config()
    assert cfg.severity_escalation_excluded_services == ("noisy-svc", "dev-debug")
    assert cfg.severity_escalation_floors == (
        SeverityFloor(service="my-svc", floor="error"),
        SeverityFloor(service="other-svc", floor="warning"),
    )


def test_load_logs_config_severity_escalation_absent_defaults_empty(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """severity_escalation absent -> both fields default to empty tuples."""
    cfg_file = tmp_path / "homelab-monitor.yaml"
    cfg_file.write_text("logs:\n  error_patterns: []\n")
    monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(cfg_file))
    cfg = load_logs_config()
    assert cfg.severity_escalation_excluded_services == ()
    assert cfg.severity_escalation_floors == ()


def test_load_logs_config_severity_escalation_null_defaults_empty(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """severity_escalation: null -> both fields default to empty tuples."""
    cfg_file = tmp_path / "homelab-monitor.yaml"
    cfg_file.write_text("logs:\n  severity_escalation:\n")
    monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(cfg_file))
    cfg = load_logs_config()
    assert cfg.severity_escalation_excluded_services == ()
    assert cfg.severity_escalation_floors == ()


def test_load_logs_config_severity_escalation_excluded_only(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """excluded_services present, severity_floors absent -> floors defaults to empty."""
    cfg_file = tmp_path / "homelab-monitor.yaml"
    cfg_file.write_text("logs:\n  severity_escalation:\n    excluded_services:\n      - svc-a\n")
    monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(cfg_file))
    cfg = load_logs_config()
    assert cfg.severity_escalation_excluded_services == ("svc-a",)
    assert cfg.severity_escalation_floors == ()


def test_load_logs_config_severity_escalation_excluded_null_defaults_empty(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """excluded_services present but null -> defaults to empty tuple."""
    cfg_file = tmp_path / "homelab-monitor.yaml"
    cfg_file.write_text("logs:\n  severity_escalation:\n    excluded_services:\n")
    monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(cfg_file))
    cfg = load_logs_config()
    assert cfg.severity_escalation_excluded_services == ()
    assert cfg.severity_escalation_floors == ()


def test_load_logs_config_severity_floors_null_defaults_empty(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """severity_floors present but null -> defaults to empty tuple."""
    cfg_file = tmp_path / "homelab-monitor.yaml"
    cfg_file.write_text("logs:\n  severity_escalation:\n    severity_floors:\n")
    monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(cfg_file))
    cfg = load_logs_config()
    assert cfg.severity_escalation_excluded_services == ()
    assert cfg.severity_escalation_floors == ()


def test_load_logs_config_severity_escalation_not_mapping_raises(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """severity_escalation that is not a mapping raises ValueError."""
    cfg_file = tmp_path / "homelab-monitor.yaml"
    cfg_file.write_text("logs:\n  severity_escalation: [a, b]\n")
    monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(cfg_file))
    with pytest.raises(ValueError, match="must be a mapping"):
        load_logs_config()


def test_load_logs_config_severity_escalation_excluded_not_list_raises(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """excluded_services that is not a list raises ValueError."""
    cfg_file = tmp_path / "homelab-monitor.yaml"
    cfg_file.write_text("logs:\n  severity_escalation:\n    excluded_services: not-a-list\n")
    monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(cfg_file))
    with pytest.raises(ValueError, match="must be a list"):
        load_logs_config()


def test_load_logs_config_severity_escalation_excluded_non_string_raises(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """excluded_services entry that is not a non-empty string raises ValueError."""
    cfg_file = tmp_path / "homelab-monitor.yaml"
    cfg_file.write_text("logs:\n  severity_escalation:\n    excluded_services:\n      - 42\n")
    monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(cfg_file))
    with pytest.raises(ValueError, match="must be a non-empty string"):
        load_logs_config()


def test_load_logs_config_severity_floors_not_list_raises(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """severity_floors that is not a list raises ValueError."""
    cfg_file = tmp_path / "homelab-monitor.yaml"
    cfg_file.write_text("logs:\n  severity_escalation:\n    severity_floors: foo\n")
    monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(cfg_file))
    with pytest.raises(ValueError, match="must be a list"):
        load_logs_config()


def test_load_logs_config_severity_floors_entry_not_mapping_raises(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """severity_floors list entry that is not a mapping raises ValueError."""
    cfg_file = tmp_path / "homelab-monitor.yaml"
    cfg_file.write_text(
        "logs:\n  severity_escalation:\n    severity_floors:\n      - just a string\n"
    )
    monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(cfg_file))
    with pytest.raises(ValueError, match="must be a mapping"):
        load_logs_config()


def test_load_logs_config_severity_floors_missing_service_raises(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """severity_floors entry missing service raises ValueError."""
    cfg_file = tmp_path / "homelab-monitor.yaml"
    cfg_file.write_text(
        "logs:\n  severity_escalation:\n    severity_floors:\n      - floor: error\n"
    )
    monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(cfg_file))
    with pytest.raises(ValueError, match="service"):
        load_logs_config()


def test_load_logs_config_severity_floors_missing_floor_raises(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """severity_floors entry missing floor raises ValueError."""
    cfg_file = tmp_path / "homelab-monitor.yaml"
    cfg_file.write_text(
        "logs:\n  severity_escalation:\n    severity_floors:\n      - service: my-svc\n"
    )
    monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(cfg_file))
    with pytest.raises(ValueError, match="floor"):
        load_logs_config()


# --- SilenceDetectionConfig (STAGE-004-038) -------------------------------------------


def test_load_silence_detection_config_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """load_silence_detection_config returns documented defaults when no env vars set."""
    monkeypatch.delenv("HOMELAB_MONITOR_SILENCE_MIN_S", raising=False)
    monkeypatch.delenv("HOMELAB_MONITOR_SILENCE_MAX_S", raising=False)
    monkeypatch.delenv("HOMELAB_MONITOR_SILENCE_CRON_GRACE_S", raising=False)
    cfg = load_silence_detection_config()
    defaults = SilenceDetectionConfig()
    assert cfg.silent_min_seconds == defaults.silent_min_seconds == 900  # noqa: PLR2004
    assert cfg.silent_max_seconds == defaults.silent_max_seconds == 3600  # noqa: PLR2004
    assert cfg.cron_grace_seconds == defaults.cron_grace_seconds == 900  # noqa: PLR2004


def test_load_silence_detection_config_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    """All three env vars override their fields."""
    monkeypatch.setenv("HOMELAB_MONITOR_SILENCE_MIN_S", "600")
    monkeypatch.setenv("HOMELAB_MONITOR_SILENCE_MAX_S", "7200")
    monkeypatch.setenv("HOMELAB_MONITOR_SILENCE_CRON_GRACE_S", "300")

    cfg = load_silence_detection_config()
    assert cfg.silent_min_seconds == 600  # noqa: PLR2004
    assert cfg.silent_max_seconds == 7200  # noqa: PLR2004
    assert cfg.cron_grace_seconds == 300  # noqa: PLR2004


def test_load_silence_detection_config_clamp_below_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each field clamped to >= 1 when env sets to 0."""
    monkeypatch.setenv("HOMELAB_MONITOR_SILENCE_MIN_S", "0")
    monkeypatch.setenv("HOMELAB_MONITOR_SILENCE_MAX_S", "0")
    monkeypatch.setenv("HOMELAB_MONITOR_SILENCE_CRON_GRACE_S", "0")

    cfg = load_silence_detection_config()
    assert cfg.silent_min_seconds == 1
    assert cfg.silent_max_seconds == 1
    assert cfg.cron_grace_seconds == 1


# STAGE-004-041: VlHealthConfig + load_vl_health_config
def test_load_vl_health_config_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """load_vl_health_config returns 5.0s default when env var is absent."""
    monkeypatch.delenv("HOMELAB_MONITOR_VL_HEALTH_TIMEOUT_S", raising=False)
    cfg = load_vl_health_config()
    assert cfg.timeout_s == 5.0  # noqa: PLR2004


def test_load_vl_health_config_env_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HOMELAB_MONITOR_VL_HEALTH_TIMEOUT_S overrides the default."""
    monkeypatch.setenv("HOMELAB_MONITOR_VL_HEALTH_TIMEOUT_S", "10.0")
    cfg = load_vl_health_config()
    assert cfg.timeout_s == 10.0  # noqa: PLR2004


def test_load_vl_health_config_malformed_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Malformed env value propagates ValueError (mirrors VlDiskWarningConfig pattern)."""
    monkeypatch.setenv("HOMELAB_MONITOR_VL_HEALTH_TIMEOUT_S", "not-a-float")
    with pytest.raises(ValueError):
        load_vl_health_config()


def test_vl_health_config_is_frozen() -> None:
    """VlHealthConfig instances are frozen (immutable)."""
    cfg = VlHealthConfig()
    with pytest.raises((AttributeError, TypeError)):
        cfg.timeout_s = 99.0  # type: ignore[misc]


# --- CardinalityCapsConfig ---------------------------------------------------------------


def test_cardinality_caps_defaults_when_file_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """No config file = built-in defaults."""
    monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(tmp_path / "missing.yaml"))
    monkeypatch.delenv("HOMELAB_MONITOR_CARDINALITY_CAP_DEFAULT", raising=False)
    cfg = load_cardinality_caps_config()
    assert cfg == CardinalityCapsConfig()
    assert cfg.default == 500  # noqa: PLR2004
    assert cfg.cap_for("anything") == 500  # noqa: PLR2004


def test_cardinality_caps_reads_yaml_families(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """YAML with cardinality_caps section overrides defaults."""
    cfg_file = tmp_path / "homelab-monitor.yaml"
    cfg_file.write_text(
        "cardinality_caps:\n  default: 500\n  families:\n    homelab_ha_entity_available: 2500\n"
    )
    monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(cfg_file))
    monkeypatch.delenv("HOMELAB_MONITOR_CARDINALITY_CAP_DEFAULT", raising=False)
    cfg = load_cardinality_caps_config()
    assert cfg.cap_for("homelab_ha_entity_available") == 2500  # noqa: PLR2004
    assert cfg.cap_for("other") == 500  # noqa: PLR2004


def test_cardinality_caps_yaml_default_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """YAML default overrides built-in default."""
    cfg_file = tmp_path / "homelab-monitor.yaml"
    cfg_file.write_text("cardinality_caps:\n  default: 1000\n")
    monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(cfg_file))
    monkeypatch.delenv("HOMELAB_MONITOR_CARDINALITY_CAP_DEFAULT", raising=False)
    cfg = load_cardinality_caps_config()
    assert cfg.default == 1000  # noqa: PLR2004
    assert cfg.cap_for("x") == 1000  # noqa: PLR2004


def test_cardinality_caps_env_overrides_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """HOMELAB_MONITOR_CARDINALITY_CAP_DEFAULT env overrides default."""
    monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(tmp_path / "absent.yaml"))
    monkeypatch.setenv("HOMELAB_MONITOR_CARDINALITY_CAP_DEFAULT", "999")
    cfg = load_cardinality_caps_config()
    assert cfg.default == 999  # noqa: PLR2004


def test_cardinality_caps_env_override_keeps_yaml_families(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """env default override keeps yaml families."""
    cfg_file = tmp_path / "homelab-monitor.yaml"
    cfg_file.write_text(
        "cardinality_caps:\n  default: 500\n  families:\n    homelab_ha_entity_available: 2500\n"
    )
    monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(cfg_file))
    monkeypatch.setenv("HOMELAB_MONITOR_CARDINALITY_CAP_DEFAULT", "999")
    cfg = load_cardinality_caps_config()
    assert cfg.default == 999  # noqa: PLR2004
    assert cfg.cap_for("homelab_ha_entity_available") == 2500  # noqa: PLR2004


def test_cardinality_caps_rejects_non_mapping_section(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """cardinality_caps must be a mapping."""
    cfg_file = tmp_path / "bad.yaml"
    cfg_file.write_text("cardinality_caps: 42\n")
    monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(cfg_file))
    with pytest.raises(ValueError, match="cardinality_caps must be a mapping"):
        load_cardinality_caps_config()


def test_cardinality_caps_rejects_non_mapping_families(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """families must be a mapping."""
    cfg_file = tmp_path / "bad.yaml"
    cfg_file.write_text("cardinality_caps:\n  families: 7\n")
    monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(cfg_file))
    with pytest.raises(ValueError, match="families must be a mapping"):
        load_cardinality_caps_config()


def test_cardinality_caps_rejects_non_int_family_value(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """family cap value must be an integer."""
    cfg_file = tmp_path / "bad.yaml"
    cfg_file.write_text("cardinality_caps:\n  families:\n    fam: notanumber\n")
    monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(cfg_file))
    with pytest.raises(ValueError, match="must be an integer"):
        load_cardinality_caps_config()


def test_cardinality_caps_rejects_non_int_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """default cap value must be an integer."""
    cfg_file = tmp_path / "bad.yaml"
    cfg_file.write_text("cardinality_caps:\n  default: notanumber\n")
    monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(cfg_file))
    with pytest.raises(ValueError, match="must be an integer"):
        load_cardinality_caps_config()


def test_cardinality_caps_rejects_non_mapping_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """config root must be a mapping."""
    cfg_file = tmp_path / "bad.yaml"
    cfg_file.write_text("- a\n- b\n")
    monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(cfg_file))
    with pytest.raises(ValueError, match="config root must be a mapping"):
        load_cardinality_caps_config()


def test_cardinality_caps_null_families_is_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """families: null falls through to empty dict."""
    cfg_file = tmp_path / "empty.yaml"
    cfg_file.write_text("cardinality_caps:\n  default: 500\n  families:\n")
    monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(cfg_file))
    cfg = load_cardinality_caps_config()
    assert cfg.families == {}
    assert cfg.cap_for("x") == 500  # noqa: PLR2004


def test_cardinality_caps_rejects_bool_family_value(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """family cap value cannot be a bool."""
    cfg_file = tmp_path / "bad.yaml"
    cfg_file.write_text("cardinality_caps:\n  families:\n    fam: true\n")
    monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(cfg_file))
    with pytest.raises(ValueError, match="must be an integer"):
        load_cardinality_caps_config()


def test_cardinality_caps_rejects_bool_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """default cap value cannot be a bool (bool is subclass of int but must be rejected)."""
    cfg_file = tmp_path / "bad.yaml"
    cfg_file.write_text("cardinality_caps:\n  default: true\n")
    monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(cfg_file))
    with pytest.raises(ValueError, match="'default' must be an integer"):
        load_cardinality_caps_config()


def test_cardinality_caps_rejects_empty_string_family_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """families mapping key must be a non-empty string."""
    cfg_file = tmp_path / "bad.yaml"
    cfg_file.write_text('cardinality_caps:\n  families:\n    "": 500\n')
    monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(cfg_file))
    with pytest.raises(ValueError, match="families key must be a non-empty string"):
        load_cardinality_caps_config()


def test_cardinality_caps_accepts_negative_values(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A negative cap loads as-is (the capper clamps it to 0 at apply time)."""
    cfg_file = tmp_path / "neg.yaml"
    cfg_file.write_text("cardinality_caps:\n  default: -5\n  families:\n    fam: -1\n")
    monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(cfg_file))
    monkeypatch.delenv("HOMELAB_MONITOR_CARDINALITY_CAP_DEFAULT", raising=False)
    cfg = load_cardinality_caps_config()
    assert cfg.cap_for("fam") == -1
    assert cfg.cap_for("unknown") == -5  # noqa: PLR2004
