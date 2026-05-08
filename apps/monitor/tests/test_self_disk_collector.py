"""Tests for :class:`SelfDiskCollector`."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import structlog
from sqlalchemy import text

from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.io import (
    InMemoryLogsWriter,
    MemoryRetainingMetricsWriter,
)
from homelab_monitor.kernel.plugins.types import CollectorConfig
from homelab_monitor.plugins.collectors.builtin.self_disk import SelfDiskCollector


def _ctx(
    writer: MemoryRetainingMetricsWriter,
    cfg: CollectorConfig,
    repo: SqliteRepository,
) -> CollectorContext:
    """Minimal CollectorContext for the disk collector."""
    return CollectorContext(
        config=cfg,
        db=repo,
        vm=writer,
        vl=InMemoryLogsWriter(),
        http=None,  # pyright: ignore[reportArgumentType]
        ssh=None,  # pyright: ignore[reportArgumentType]
        secrets=None,  # pyright: ignore[reportArgumentType]
        log=structlog.get_logger().bind(collector="self_disk"),  # pyright: ignore[reportArgumentType]
        ha=None,
    )


def _setup_dirs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Path]:
    """Create empty per-slot directories under tmp_path and point env at them."""
    paths = {
        "vm": tmp_path / "vm",
        "vl": tmp_path / "vl",
        "sqlite": tmp_path / "sqlite",
        "runbook": tmp_path / "runbook",
    }
    for p in paths.values():
        p.mkdir()
    monkeypatch.setenv("HOMELAB_MONITOR_VM_DATA_DIR", str(paths["vm"]))
    monkeypatch.setenv("HOMELAB_MONITOR_VL_DATA_DIR", str(paths["vl"]))
    monkeypatch.setenv("HOMELAB_MONITOR_SQLITE_DATA_DIR", str(paths["sqlite"]))
    monkeypatch.setenv("HOMELAB_MONITOR_RUNBOOK_TRANSCRIPTS_DIR", str(paths["runbook"]))
    monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(tmp_path / "homelab-monitor.yaml"))
    monkeypatch.delenv("HOMELAB_MONITOR_DISK_BUDGET_GB", raising=False)
    return paths


@pytest.mark.asyncio
async def test_run_emits_used_and_budget_gauges_per_slot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    repo: SqliteRepository,
) -> None:
    """Per-slot used + budget gauges and overall used_pct are emitted."""
    paths = _setup_dirs(monkeypatch, tmp_path)
    (paths["vm"] / "x").write_bytes(b"x" * 100)
    (paths["sqlite"] / "y").write_bytes(b"y" * 50)

    writer = MemoryRetainingMetricsWriter()
    cfg = CollectorConfig(name="self_disk")
    result = await SelfDiskCollector().run(_ctx(writer, cfg, repo))

    assert result.ok
    used_entries = {
        e.labels["slot"]: e.value
        for e in writer.snapshot()
        if e.name == "homelab_self_disk_used_bytes"
    }
    assert used_entries["vm"] == 100.0  # noqa: PLR2004
    assert used_entries["sqlite"] == 50.0  # noqa: PLR2004
    assert used_entries["vl"] == 0.0
    assert used_entries["runbook_transcripts"] == 0.0

    budget_entries = {
        e.labels["slot"]: e.value
        for e in writer.snapshot()
        if e.name == "homelab_self_disk_budget_bytes"
    }
    # Default: 50 GB * 0.6 = 30 GB for vm; just assert positive
    assert budget_entries["vm"] > 0
    assert budget_entries["vl"] > 0
    assert budget_entries["sqlite"] > 0

    pct = [e for e in writer.snapshot() if e.name == "homelab_self_disk_used_pct"]
    assert len(pct) == 1
    assert pct[0].value >= 0.0


@pytest.mark.asyncio
async def test_run_handles_missing_directories_gracefully(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    repo: SqliteRepository,
) -> None:
    """If a per-slot dir does not exist, used=0 and the tick still succeeds."""
    monkeypatch.setenv("HOMELAB_MONITOR_VM_DATA_DIR", str(tmp_path / "no-vm"))
    monkeypatch.setenv("HOMELAB_MONITOR_VL_DATA_DIR", str(tmp_path / "no-vl"))
    monkeypatch.setenv("HOMELAB_MONITOR_SQLITE_DATA_DIR", str(tmp_path / "no-sqlite"))
    monkeypatch.setenv("HOMELAB_MONITOR_RUNBOOK_TRANSCRIPTS_DIR", str(tmp_path / "no-runbook"))
    monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(tmp_path / "absent.yaml"))
    monkeypatch.delenv("HOMELAB_MONITOR_DISK_BUDGET_GB", raising=False)

    writer = MemoryRetainingMetricsWriter()
    cfg = CollectorConfig(name="self_disk")
    result = await SelfDiskCollector().run(_ctx(writer, cfg, repo))
    assert result.ok
    used_entries = {
        e.labels["slot"]: e.value
        for e in writer.snapshot()
        if e.name == "homelab_self_disk_used_bytes"
    }
    assert all(v == 0.0 for v in used_entries.values())


@pytest.mark.asyncio
async def test_run_emits_shrink_counter_above_critical_threshold(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    repo: SqliteRepository,
) -> None:
    """When used_pct > 95%, a shrink counter is emitted and audit row written."""
    paths = _setup_dirs(monkeypatch, tmp_path)
    # Force a tiny budget so any data tips us over 95%
    monkeypatch.setenv("HOMELAB_MONITOR_DISK_BUDGET_GB", "0.000001")  # 1 KB-ish total
    (paths["vm"] / "big").write_bytes(b"x" * 1024)  # 1 KB used

    writer = MemoryRetainingMetricsWriter()
    cfg = CollectorConfig(name="self_disk")
    result = await SelfDiskCollector().run(_ctx(writer, cfg, repo))
    assert result.ok

    shrink = [e for e in writer.snapshot() if e.name == "homelab_self_disk_shrink_total"]
    assert len(shrink) == 1
    assert shrink[0].labels["tier"] == "v1"

    # Audit row was written
    rows = await repo.fetch_all(
        text("SELECT who, what, before_json, after_json FROM audit_log WHERE what = :w"),
        {"w": "auto_shrink_decision"},
    )
    assert len(rows) == 1
    assert rows[0].who == "system:self_disk_shrinker"
    after = json.loads(rows[0].after_json)
    assert after["tier"] == "v1"
    assert after["action"] == "metric_only_emitted"


@pytest.mark.asyncio
async def test_run_does_not_emit_shrink_below_critical(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    repo: SqliteRepository,
) -> None:
    """When used_pct <= 95%, no shrink counter is emitted."""
    paths = _setup_dirs(monkeypatch, tmp_path)
    monkeypatch.setenv("HOMELAB_MONITOR_DISK_BUDGET_GB", "100")  # 100 GB
    (paths["vm"] / "small").write_bytes(b"x" * 1024)  # 1 KB << 95% of 100 GB

    writer = MemoryRetainingMetricsWriter()
    cfg = CollectorConfig(name="self_disk")
    result = await SelfDiskCollector().run(_ctx(writer, cfg, repo))
    assert result.ok
    shrink = [e for e in writer.snapshot() if e.name == "homelab_self_disk_shrink_total"]
    assert shrink == []


@pytest.mark.asyncio
async def test_run_records_config_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    repo: SqliteRepository,
) -> None:
    """A bad YAML config produces ok=False with an error and no metrics."""
    cfg_file = tmp_path / "bad.yaml"
    cfg_file.write_text("disk_budget:\n  vm_ratio: 0.5\n  vl_ratio: 0.5\n  sqlite_ratio: 0.5\n")
    monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(cfg_file))
    writer = MemoryRetainingMetricsWriter()
    cfg = CollectorConfig(name="self_disk")
    result = await SelfDiskCollector().run(_ctx(writer, cfg, repo))
    assert result.ok is False
    assert any("config" in e for e in result.errors)
    assert result.metrics_emitted == 0
