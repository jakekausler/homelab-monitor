"""Unit tests for VL retention reconciliation + AppSettingsRepository (STAGE-004-022)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from homelab_monitor.kernel.config import load_vl_disk_warning_config
from homelab_monitor.kernel.db.repositories.app_settings_repository import (
    AppSettingsRepository,
)
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.logs.vl_retention import (
    VL_RETENTION_DAYS_KEY,
    RetentionRangeError,
    compute_vl_disk_usage,
    persist_retention,
    reconcile_retention,
    resolve_retention,
)

# ---- pure reconcile_retention ----


def test_reconcile_no_override_no_env_is_default() -> None:
    state = reconcile_retention(effective_days=30, override=None, env_is_set=False)
    assert state.retention_days == 30  # noqa: PLR2004
    assert state.pending_retention_days is None
    assert state.retention_source == "default"
    assert state.restart_required is False


def test_reconcile_no_override_env_set_is_env() -> None:
    state = reconcile_retention(effective_days=14, override=None, env_is_set=True)
    assert state.retention_days == 14  # noqa: PLR2004
    assert state.pending_retention_days is None
    assert state.retention_source == "env"
    assert state.restart_required is False


def test_reconcile_override_differs_is_runtime_pending() -> None:
    state = reconcile_retention(effective_days=30, override=90, env_is_set=False)
    assert state.retention_days == 30  # noqa: PLR2004
    assert state.pending_retention_days == 90  # noqa: PLR2004
    assert state.retention_source == "runtime"
    assert state.restart_required is True


def test_reconcile_override_equals_effective_is_not_pending() -> None:
    state = reconcile_retention(effective_days=30, override=30, env_is_set=True)
    assert state.pending_retention_days is None
    assert state.restart_required is False
    # equals effective + env set → source is env (override is a no-op)
    assert state.retention_source == "env"


# ---- AppSettingsRepository ----


@pytest.mark.asyncio
async def test_app_settings_get_absent_returns_none(repo: SqliteRepository) -> None:
    r = AppSettingsRepository(repo)
    assert await r.get("nope") is None


@pytest.mark.asyncio
async def test_app_settings_set_then_get(repo: SqliteRepository) -> None:
    r = AppSettingsRepository(repo)
    await r.set("k", "v1")
    assert await r.get("k") == "v1"


@pytest.mark.asyncio
async def test_app_settings_set_upserts(repo: SqliteRepository) -> None:
    r = AppSettingsRepository(repo)
    await r.set("k", "v1")
    await r.set("k", "v2")
    assert await r.get("k") == "v2"


@pytest.mark.asyncio
async def test_app_settings_delete(repo: SqliteRepository) -> None:
    r = AppSettingsRepository(repo)
    await r.set("k", "v1")
    assert await r.delete("k") is True
    assert await r.get("k") is None
    assert await r.delete("k") is False


# ---- resolve_retention / persist_retention (repo + env) ----


@pytest.mark.asyncio
async def test_resolve_default_when_no_env_no_override(
    repo: SqliteRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("HOMELAB_MONITOR_VL_RETENTION_DAYS", raising=False)
    state = await resolve_retention(AppSettingsRepository(repo))
    assert state.retention_days == 30  # noqa: PLR2004
    assert state.retention_source == "default"
    assert state.pending_retention_days is None


@pytest.mark.asyncio
async def test_resolve_env_when_env_set(
    repo: SqliteRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOMELAB_MONITOR_VL_RETENTION_DAYS", "14")
    state = await resolve_retention(AppSettingsRepository(repo))
    assert state.retention_days == 14  # noqa: PLR2004
    assert state.retention_source == "env"


@pytest.mark.asyncio
async def test_resolve_env_set_to_default_value_is_env_source(
    repo: SqliteRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Env set to 30 (same as hard-coded default) must yield source='env', not 'default'."""
    monkeypatch.setenv("HOMELAB_MONITOR_VL_RETENTION_DAYS", "30")
    state = await resolve_retention(AppSettingsRepository(repo))
    assert state.retention_days == 30  # noqa: PLR2004
    assert state.retention_source == "env"
    assert state.pending_retention_days is None


@pytest.mark.asyncio
async def test_resolve_corrupt_override_treated_as_no_override(
    repo: SqliteRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-int stored override must not raise — behaves as no override."""
    monkeypatch.delenv("HOMELAB_MONITOR_VL_RETENTION_DAYS", raising=False)
    r = AppSettingsRepository(repo)
    await r.set(VL_RETENTION_DAYS_KEY, "abc")
    state = await resolve_retention(r)
    # No override → source is "default", pending is None, no crash
    assert state.pending_retention_days is None
    assert state.retention_source == "default"
    assert state.restart_required is False


@pytest.mark.asyncio
async def test_persist_differs_sets_pending(
    repo: SqliteRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("HOMELAB_MONITOR_VL_RETENTION_DAYS", raising=False)
    r = AppSettingsRepository(repo)
    state = await persist_retention(r, 90)
    assert state.pending_retention_days == 90  # noqa: PLR2004
    assert state.restart_required is True
    assert state.retention_source == "runtime"
    assert await r.get(VL_RETENTION_DAYS_KEY) == "90"


@pytest.mark.asyncio
async def test_persist_equals_effective_clears_pending(
    repo: SqliteRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("HOMELAB_MONITOR_VL_RETENTION_DAYS", raising=False)
    r = AppSettingsRepository(repo)
    await persist_retention(r, 90)  # stash an override first
    state = await persist_retention(r, 30)  # equals effective default
    assert state.pending_retention_days is None
    assert state.restart_required is False
    assert await r.get(VL_RETENTION_DAYS_KEY) is None


@pytest.mark.asyncio
async def test_persist_out_of_range_raises(repo: SqliteRepository) -> None:
    r = AppSettingsRepository(repo)
    with pytest.raises(RetentionRangeError):
        await persist_retention(r, 0)
    with pytest.raises(RetentionRangeError):
        await persist_retention(r, 400)


# ---- compute_vl_disk_usage ----


def test_compute_disk_usage_with_known_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vl = tmp_path / "vl"
    vl.mkdir()
    (vl / "data.bin").write_bytes(b"x" * 1024)
    monkeypatch.setenv("HOMELAB_MONITOR_VL_DATA_DIR", str(vl))
    monkeypatch.delenv("HOMELAB_MONITOR_DISK_BUDGET_GB", raising=False)
    usage = compute_vl_disk_usage()
    assert usage.disk_used_gb > 0.0
    assert usage.disk_used_pct > 0.0
    assert usage.budget_available is True


def test_compute_disk_usage_zero_budget_guards_divzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vl = tmp_path / "vl"
    vl.mkdir()
    (vl / "data.bin").write_bytes(b"x" * 1024)
    monkeypatch.setenv("HOMELAB_MONITOR_VL_DATA_DIR", str(vl))
    monkeypatch.setenv("HOMELAB_MONITOR_DISK_BUDGET_GB", "0")
    usage = compute_vl_disk_usage()
    assert usage.disk_used_pct == 0.0
    assert usage.budget_available is False


def test_compute_disk_usage_config_error_degrades_gracefully(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """load_disk_budget_config raising ValueError must not propagate — returns 0/0."""

    def _raise() -> object:
        raise ValueError("bad config")

    monkeypatch.setattr(
        "homelab_monitor.kernel.logs.vl_retention.load_disk_budget_config",
        _raise,
    )
    usage = compute_vl_disk_usage()
    assert usage.disk_used_gb == 0.0
    assert usage.disk_used_pct == 0.0
    assert usage.budget_available is False


def test_compute_disk_usage_yaml_error_degrades_gracefully(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """load_disk_budget_config raising yaml.YAMLError must not propagate — returns 0/0."""

    def _raise() -> object:
        raise yaml.YAMLError("bad yaml")

    monkeypatch.setattr(
        "homelab_monitor.kernel.logs.vl_retention.load_disk_budget_config",
        _raise,
    )
    usage = compute_vl_disk_usage()
    assert usage.disk_used_gb == 0.0
    assert usage.disk_used_pct == 0.0
    assert usage.budget_available is False


# ---- config loader ----


def test_disk_warning_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HOMELAB_MONITOR_VL_DISK_WARN_PCT", raising=False)
    monkeypatch.delenv("HOMELAB_MONITOR_VL_DISK_CRIT_PCT", raising=False)
    cfg = load_vl_disk_warning_config()
    assert cfg.warn_pct == 70  # noqa: PLR2004
    assert cfg.crit_pct == 85  # noqa: PLR2004


def test_disk_warning_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOMELAB_MONITOR_VL_DISK_WARN_PCT", "60")
    monkeypatch.setenv("HOMELAB_MONITOR_VL_DISK_CRIT_PCT", "80")
    cfg = load_vl_disk_warning_config()
    assert cfg.warn_pct == 60  # noqa: PLR2004
    assert cfg.crit_pct == 80  # noqa: PLR2004


def test_disk_warning_inverted_env_normalizes(monkeypatch: pytest.MonkeyPatch) -> None:
    """warn > crit in env must be normalized to warn <= crit."""
    monkeypatch.setenv("HOMELAB_MONITOR_VL_DISK_WARN_PCT", "85")
    monkeypatch.setenv("HOMELAB_MONITOR_VL_DISK_CRIT_PCT", "70")
    cfg = load_vl_disk_warning_config()
    assert cfg.warn_pct == 70  # noqa: PLR2004
    assert cfg.crit_pct == 85  # noqa: PLR2004
