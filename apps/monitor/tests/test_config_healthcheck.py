"""Tests for load_healthcheck_log_config() (STAGE-004-033).

Sync tests; no asyncio needed. Tests defaults, env overrides, and clamping.
"""

from __future__ import annotations

import pytest

from homelab_monitor.kernel.config import load_healthcheck_log_config


def test_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """No env vars set: defaults match spec."""
    monkeypatch.delenv("HOMELAB_MONITOR_HEALTHCHECK_LOG_WINDOW_S", raising=False)
    monkeypatch.delenv("HOMELAB_MONITOR_HEALTHCHECK_ENRICHMENT_RETENTION_DAYS", raising=False)

    cfg = load_healthcheck_log_config()
    assert cfg.window_before_s == 30  # noqa: PLR2004
    assert cfg.window_after_s == 30  # noqa: PLR2004
    assert cfg.line_limit == 100  # noqa: PLR2004
    assert cfg.retention_days == 7  # noqa: PLR2004
    assert cfg.max_rows_per_container == 50  # noqa: PLR2004


def test_window_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """HOMELAB_MONITOR_HEALTHCHECK_LOG_WINDOW_S overrides window_before_s."""
    monkeypatch.setenv("HOMELAB_MONITOR_HEALTHCHECK_LOG_WINDOW_S", "120")
    monkeypatch.delenv("HOMELAB_MONITOR_HEALTHCHECK_ENRICHMENT_RETENTION_DAYS", raising=False)

    cfg = load_healthcheck_log_config()
    assert cfg.window_before_s == 120  # noqa: PLR2004


def test_retention_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """HOMELAB_MONITOR_HEALTHCHECK_ENRICHMENT_RETENTION_DAYS overrides retention_days."""
    monkeypatch.delenv("HOMELAB_MONITOR_HEALTHCHECK_LOG_WINDOW_S", raising=False)
    monkeypatch.setenv("HOMELAB_MONITOR_HEALTHCHECK_ENRICHMENT_RETENTION_DAYS", "14")

    cfg = load_healthcheck_log_config()
    assert cfg.retention_days == 14  # noqa: PLR2004


def test_window_clamped_min_1(monkeypatch: pytest.MonkeyPatch) -> None:
    """window_before_s is clamped to at least 1 when env var is 0."""
    monkeypatch.setenv("HOMELAB_MONITOR_HEALTHCHECK_LOG_WINDOW_S", "0")
    monkeypatch.delenv("HOMELAB_MONITOR_HEALTHCHECK_ENRICHMENT_RETENTION_DAYS", raising=False)

    cfg = load_healthcheck_log_config()
    assert cfg.window_before_s == 1


def test_retention_clamped_min_1(monkeypatch: pytest.MonkeyPatch) -> None:
    """retention_days is clamped to at least 1 when env var is 0."""
    monkeypatch.delenv("HOMELAB_MONITOR_HEALTHCHECK_LOG_WINDOW_S", raising=False)
    monkeypatch.setenv("HOMELAB_MONITOR_HEALTHCHECK_ENRICHMENT_RETENTION_DAYS", "0")

    cfg = load_healthcheck_log_config()
    assert cfg.retention_days == 1
