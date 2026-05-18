"""Tests for HeartbeatStateCollector (T3 — STAGE-002-010).

Pattern: mirrors test_self_disk_collector.py — uses MemoryRetainingMetricsWriter
and a real in-memory migrated DB (the `repo` fixture).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import structlog
from sqlalchemy import text

from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.db.time import utc_now_iso
from homelab_monitor.kernel.metrics.heartbeat_collector import HeartbeatStateCollector
from homelab_monitor.kernel.metrics.multiplex import MultiplexMetricsWriter
from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.io import InMemoryLogsWriter, MemoryRetainingMetricsWriter
from homelab_monitor.kernel.plugins.types import CollectorConfig

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ctx(writer: MemoryRetainingMetricsWriter, repo: SqliteRepository) -> CollectorContext:
    """Minimal CollectorContext for HeartbeatStateCollector."""
    return CollectorContext(
        config=CollectorConfig(name="heartbeat_state"),
        db=repo,
        vm=writer,
        vl=InMemoryLogsWriter(),
        http=None,  # pyright: ignore[reportArgumentType]
        ssh=None,  # pyright: ignore[reportArgumentType]
        secrets=None,  # pyright: ignore[reportArgumentType]
        log=structlog.get_logger().bind(collector="heartbeat_state"),  # pyright: ignore[reportArgumentType]
        ha=None,
    )


async def _seed_cron(  # noqa: PLR0913
    repo: SqliteRepository,
    *,
    fingerprint: str,
    name: str = "job",
    host: str = "h",
    cadence_seconds: int = 3600,
    wrapper_installed: int = 0,
    hidden_at: str | None = None,
    soft_deleted_at: str | None = None,
    last_discovered_at: str | None = None,
) -> None:
    now = utc_now_iso()
    async with repo.engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO crons (fingerprint, name, host, command, schedule, "
                "schedule_canonical, cadence_seconds, expected_grace_seconds, "
                "enabled, last_seen_state, created_at, updated_at, hidden_at, "
                "source_path, wrapper_last_seen_at, wrapper_installed, "
                "soft_deleted_at, last_discovered_at) VALUES ("
                ":fp, :name, :host, '/cmd', '* * * * *', '* * * * *', :cadence, "
                "300, 1, 'unknown', :now, :now, :hidden, NULL, NULL, :wi, :sda, :lda)"
            ),
            {
                "fp": fingerprint,
                "name": name,
                "host": host,
                "cadence": cadence_seconds,
                "now": now,
                "hidden": hidden_at,
                "wi": wrapper_installed,
                "sda": soft_deleted_at,
                "lda": last_discovered_at,
            },
        )


async def _seed_state(  # noqa: PLR0913
    repo: SqliteRepository,
    *,
    fingerprint: str,
    last_ok_at: str | None = None,
    last_fail_at: str | None = None,
    current_streak: int = 0,
    expected_next_at: str | None = None,
    last_duration_seconds: float | None = None,
    logscrape_runs_since_heartbeat: int = 0,
) -> None:
    now = utc_now_iso()
    async with repo.engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO heartbeats_state (cron_fingerprint, current_state, "
                "last_start_at, last_ok_at, last_fail_at, current_streak, "
                "expected_next_at, last_duration_seconds, last_exit_code, updated_at, "
                "observed_runs_total, last_observed_run_at, logscrape_runs_since_heartbeat) "
                "VALUES (:fp, 'ok', NULL, :ok_at, :fail_at, :streak, :next_at, "
                ":dur, NULL, :now, 0, NULL, :logscrape)"
            ),
            {
                "fp": fingerprint,
                "ok_at": last_ok_at,
                "fail_at": last_fail_at,
                "streak": current_streak,
                "next_at": expected_next_at,
                "dur": last_duration_seconds,
                "now": now,
                "logscrape": logscrape_runs_since_heartbeat,
            },
        )


def _names(writer: MemoryRetainingMetricsWriter) -> set[str]:
    return {e.name for e in writer.snapshot()}


def _values_by_name(writer: MemoryRetainingMetricsWriter, name: str) -> list[float]:
    return [e.value for e in writer.snapshot() if e.name == name]


def _labels_by_name(writer: MemoryRetainingMetricsWriter, name: str) -> list[dict[str, str]]:
    return [e.labels for e in writer.snapshot() if e.name == name]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_collector_emits_six_metrics_for_cron_with_full_state(
    repo: SqliteRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All 6 metrics emitted when cron has full state row."""
    # Disable host-proc so @reboot logic doesn't interfere
    monkeypatch.setenv("HM_HOST_PROC_DIR", "/nonexistent-proc")
    now_iso = utc_now_iso()
    future_iso = (datetime.now(UTC) + timedelta(seconds=120)).isoformat()

    await _seed_cron(repo, fingerprint="fp-full", cadence_seconds=3600)
    await _seed_state(
        repo,
        fingerprint="fp-full",
        last_ok_at=now_iso,
        last_fail_at=now_iso,
        current_streak=5,
        expected_next_at=future_iso,
        last_duration_seconds=10.0,
        logscrape_runs_since_heartbeat=2,
    )

    writer = MemoryRetainingMetricsWriter()
    result = await HeartbeatStateCollector().run(_ctx(writer, repo))

    assert result.ok
    assert "homelab_heartbeat_seconds_since_last_ok" in _names(writer)
    assert "homelab_heartbeat_seconds_since_last_fail" in _names(writer)
    assert "homelab_heartbeat_current_streak" in _names(writer)
    assert "homelab_heartbeat_expected_next_seconds" in _names(writer)
    assert "homelab_heartbeat_last_duration_seconds" in _names(writer)
    assert "homelab_heartbeat_logscrape_count_since_last_heartbeat" in _names(writer)
    assert result.metrics_emitted == 6  # noqa: PLR2004


@pytest.mark.asyncio
async def test_wrapper_installed_label_yes_and_no(
    repo: SqliteRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """wrapper_installed label is 'yes' when column is 1, 'no' when 0."""
    monkeypatch.setenv("HM_HOST_PROC_DIR", "/nonexistent-proc")

    await _seed_cron(repo, fingerprint="fp-wi-yes", name="wi-yes", wrapper_installed=1)
    await _seed_cron(repo, fingerprint="fp-wi-no", name="wi-no", wrapper_installed=0)

    writer = MemoryRetainingMetricsWriter()
    await HeartbeatStateCollector().run(_ctx(writer, repo))

    labels_list = _labels_by_name(writer, "homelab_heartbeat_logscrape_count_since_last_heartbeat")
    wi_labels = {lbl["name"]: lbl["wrapper_installed"] for lbl in labels_list}
    assert wi_labels.get("wi-yes") == "yes"
    assert wi_labels.get("wi-no") == "no"


@pytest.mark.asyncio
async def test_hidden_cron_emits_no_series(
    repo: SqliteRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hidden cron (hidden_at set) → no metrics emitted for it."""
    monkeypatch.setenv("HM_HOST_PROC_DIR", "/nonexistent-proc")
    await _seed_cron(repo, fingerprint="fp-hidden", name="hidden-job", hidden_at=utc_now_iso())

    writer = MemoryRetainingMetricsWriter()
    result = await HeartbeatStateCollector().run(_ctx(writer, repo))

    assert result.ok
    all_labels = [e.labels for e in writer.snapshot()]
    assert all(lbl.get("name") != "hidden-job" for lbl in all_labels)


@pytest.mark.asyncio
async def test_soft_deleted_cron_emits_no_series(
    repo: SqliteRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Soft-deleted cron (soft_deleted_at set) → no metrics emitted (D7)."""
    monkeypatch.setenv("HM_HOST_PROC_DIR", "/nonexistent-proc")
    await _seed_cron(
        repo, fingerprint="fp-sda", name="soft-deleted-job", soft_deleted_at=utc_now_iso()
    )

    writer = MemoryRetainingMetricsWriter()
    result = await HeartbeatStateCollector().run(_ctx(writer, repo))

    assert result.ok
    all_labels = [e.labels for e in writer.snapshot()]
    assert all(lbl.get("name") != "soft-deleted-job" for lbl in all_labels)


@pytest.mark.asyncio
async def test_never_pinged_cron_emits_only_logscrape_zero(
    repo: SqliteRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cron with no heartbeats_state row → only logscrape counter (value 0) emitted."""
    monkeypatch.setenv("HM_HOST_PROC_DIR", "/nonexistent-proc")
    await _seed_cron(repo, fingerprint="fp-never", name="never-pinged")

    writer = MemoryRetainingMetricsWriter()
    result = await HeartbeatStateCollector().run(_ctx(writer, repo))

    assert result.ok
    names = _names(writer)
    assert "homelab_heartbeat_logscrape_count_since_last_heartbeat" in names
    logscrape_vals = _values_by_name(
        writer, "homelab_heartbeat_logscrape_count_since_last_heartbeat"
    )
    assert logscrape_vals == [0.0]

    # Other metrics should NOT be emitted (all NULL state)
    assert "homelab_heartbeat_seconds_since_last_ok" not in names
    assert "homelab_heartbeat_seconds_since_last_fail" not in names
    assert "homelab_heartbeat_current_streak" not in names
    assert "homelab_heartbeat_last_duration_seconds" not in names
    assert "homelab_heartbeat_expected_next_seconds" not in names


@pytest.mark.asyncio
async def test_interval_cron_expected_next_past_is_negative(
    repo: SqliteRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Interval cron with expected_next_at in the past → negative expected_next_seconds."""
    monkeypatch.setenv("HM_HOST_PROC_DIR", "/nonexistent-proc")
    past_iso = (datetime.now(UTC) - timedelta(seconds=200)).isoformat()
    await _seed_cron(repo, fingerprint="fp-past", name="past-job", cadence_seconds=3600)
    await _seed_state(repo, fingerprint="fp-past", expected_next_at=past_iso, current_streak=1)

    writer = MemoryRetainingMetricsWriter()
    await HeartbeatStateCollector().run(_ctx(writer, repo))

    vals = _values_by_name(writer, "homelab_heartbeat_expected_next_seconds")
    assert len(vals) == 1
    assert vals[0] < 0


@pytest.mark.asyncio
async def test_interval_cron_expected_next_future_is_positive(
    repo: SqliteRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Interval cron with expected_next_at in the future → positive expected_next_seconds."""
    monkeypatch.setenv("HM_HOST_PROC_DIR", "/nonexistent-proc")
    future_iso = (datetime.now(UTC) + timedelta(seconds=500)).isoformat()
    await _seed_cron(repo, fingerprint="fp-future", name="future-job", cadence_seconds=3600)
    await _seed_state(repo, fingerprint="fp-future", expected_next_at=future_iso, current_streak=1)

    writer = MemoryRetainingMetricsWriter()
    await HeartbeatStateCollector().run(_ctx(writer, repo))

    vals = _values_by_name(writer, "homelab_heartbeat_expected_next_seconds")
    assert len(vals) == 1
    assert vals[0] > 0


@pytest.mark.asyncio
async def test_interval_cron_null_expected_next_not_emitted(
    repo: SqliteRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Interval cron with NULL expected_next_at → expected_next_seconds not emitted."""
    monkeypatch.setenv("HM_HOST_PROC_DIR", "/nonexistent-proc")
    await _seed_cron(repo, fingerprint="fp-no-next", name="no-next-job", cadence_seconds=3600)
    await _seed_state(repo, fingerprint="fp-no-next", expected_next_at=None, current_streak=1)

    writer = MemoryRetainingMetricsWriter()
    await HeartbeatStateCollector().run(_ctx(writer, repo))

    assert "homelab_heartbeat_expected_next_seconds" not in _names(writer)


@pytest.mark.asyncio
async def test_reboot_cron_last_ok_before_boot_is_negative(
    repo: SqliteRepository,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """@reboot cron: last_ok_at before boot → negative expected_next_seconds."""
    boot_epoch = 1700000000
    stat_file = tmp_path / "stat"
    stat_file.write_text(f"btime {boot_epoch}\n")
    monkeypatch.setenv("HM_HOST_PROC_DIR", str(tmp_path))

    boot_dt = datetime.fromtimestamp(boot_epoch, tz=UTC)
    last_ok_before_boot = (boot_dt - timedelta(seconds=100)).isoformat()

    await _seed_cron(repo, fingerprint="fp-rb-ok-before", name="rb-ok-before", cadence_seconds=0)
    await _seed_state(repo, fingerprint="fp-rb-ok-before", last_ok_at=last_ok_before_boot)

    writer = MemoryRetainingMetricsWriter()
    await HeartbeatStateCollector().run(_ctx(writer, repo))

    vals = _values_by_name(writer, "homelab_heartbeat_expected_next_seconds")
    assert len(vals) == 1
    assert vals[0] < 0


@pytest.mark.asyncio
async def test_reboot_cron_last_ok_after_boot_is_zero(
    repo: SqliteRepository,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """@reboot cron: last_ok_at after boot → expected_next_seconds == 0.0."""
    boot_epoch = 1700000000
    stat_file = tmp_path / "stat"
    stat_file.write_text(f"btime {boot_epoch}\n")
    monkeypatch.setenv("HM_HOST_PROC_DIR", str(tmp_path))

    boot_dt = datetime.fromtimestamp(boot_epoch, tz=UTC)
    last_ok_after_boot = (boot_dt + timedelta(seconds=30)).isoformat()

    await _seed_cron(repo, fingerprint="fp-rb-ok-after", name="rb-ok-after", cadence_seconds=0)
    await _seed_state(repo, fingerprint="fp-rb-ok-after", last_ok_at=last_ok_after_boot)

    writer = MemoryRetainingMetricsWriter()
    await HeartbeatStateCollector().run(_ctx(writer, repo))

    vals = _values_by_name(writer, "homelab_heartbeat_expected_next_seconds")
    assert len(vals) == 1
    assert vals[0] == 0.0


@pytest.mark.asyncio
async def test_reboot_cron_null_ok_discovered_before_boot_is_negative(
    repo: SqliteRepository,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """@reboot cron: last_ok_at NULL, discovered before boot → negative."""
    boot_epoch = 1700000000
    stat_file = tmp_path / "stat"
    stat_file.write_text(f"btime {boot_epoch}\n")
    monkeypatch.setenv("HM_HOST_PROC_DIR", str(tmp_path))

    boot_dt = datetime.fromtimestamp(boot_epoch, tz=UTC)
    discovered_before_boot = (boot_dt - timedelta(seconds=500)).isoformat()

    await _seed_cron(
        repo,
        fingerprint="fp-rb-disc-before",
        name="rb-disc-before",
        cadence_seconds=0,
        last_discovered_at=discovered_before_boot,
    )
    # No state row — never pinged

    writer = MemoryRetainingMetricsWriter()
    await HeartbeatStateCollector().run(_ctx(writer, repo))

    vals = _values_by_name(writer, "homelab_heartbeat_expected_next_seconds")
    assert len(vals) == 1
    assert vals[0] < 0


@pytest.mark.asyncio
async def test_reboot_cron_null_ok_discovered_after_boot_is_zero(
    repo: SqliteRepository,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """@reboot cron: last_ok_at NULL, discovered after boot → 0.0 (guard)."""
    boot_epoch = 1700000000
    stat_file = tmp_path / "stat"
    stat_file.write_text(f"btime {boot_epoch}\n")
    monkeypatch.setenv("HM_HOST_PROC_DIR", str(tmp_path))

    boot_dt = datetime.fromtimestamp(boot_epoch, tz=UTC)
    discovered_after_boot = (boot_dt + timedelta(seconds=60)).isoformat()

    await _seed_cron(
        repo,
        fingerprint="fp-rb-disc-after",
        name="rb-disc-after",
        cadence_seconds=0,
        last_discovered_at=discovered_after_boot,
    )

    writer = MemoryRetainingMetricsWriter()
    await HeartbeatStateCollector().run(_ctx(writer, repo))

    vals = _values_by_name(writer, "homelab_heartbeat_expected_next_seconds")
    assert len(vals) == 1
    assert vals[0] == 0.0


@pytest.mark.asyncio
async def test_reboot_cron_no_stat_file_skips_expected_next(
    repo: SqliteRepository,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """@reboot cron with no /proc/stat → expected_next_seconds NOT emitted."""
    # Point to a directory with no stat file
    monkeypatch.setenv("HM_HOST_PROC_DIR", str(tmp_path / "no-proc"))

    await _seed_cron(repo, fingerprint="fp-rb-no-proc", name="rb-no-proc", cadence_seconds=0)

    writer = MemoryRetainingMetricsWriter()
    result = await HeartbeatStateCollector().run(_ctx(writer, repo))

    assert result.ok
    assert "homelab_heartbeat_expected_next_seconds" not in _names(writer)


@pytest.mark.asyncio
async def test_metrics_emitted_count_matches_writes(
    repo: SqliteRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """result.metrics_emitted matches the number of write_gauge calls."""
    monkeypatch.setenv("HM_HOST_PROC_DIR", "/nonexistent-proc")
    now_iso = utc_now_iso()
    future_iso = (datetime.now(UTC) + timedelta(seconds=120)).isoformat()

    await _seed_cron(repo, fingerprint="fp-count", name="count-job", cadence_seconds=3600)
    await _seed_state(
        repo,
        fingerprint="fp-count",
        last_ok_at=now_iso,
        current_streak=3,
        expected_next_at=future_iso,
        last_duration_seconds=5.0,
    )

    writer = MemoryRetainingMetricsWriter()
    result = await HeartbeatStateCollector().run(_ctx(writer, repo))

    assert result.ok
    assert result.metrics_emitted == len(writer.recorded)


@pytest.mark.asyncio
async def test_interval_cron_naive_expected_next_at_adds_utc(
    repo: SqliteRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Line 180: naive expected_next_at ISO (no tzinfo) → tzinfo replaced with UTC."""
    monkeypatch.setenv("HM_HOST_PROC_DIR", "/nonexistent-proc")
    # Naive ISO string (no +00:00) for expected_next_at in the future
    future_naive = (datetime.now(UTC) + timedelta(seconds=300)).replace(tzinfo=None).isoformat()
    await _seed_cron(repo, fingerprint="fp-naive-next", name="naive-next-job", cadence_seconds=3600)
    await _seed_state(
        repo,
        fingerprint="fp-naive-next",
        expected_next_at=future_naive,
        current_streak=1,
    )

    writer = MemoryRetainingMetricsWriter()
    result = await HeartbeatStateCollector().run(_ctx(writer, repo))

    assert result.ok
    vals = _values_by_name(writer, "homelab_heartbeat_expected_next_seconds")
    assert len(vals) == 1
    assert vals[0] > 0


@pytest.mark.asyncio
async def test_reboot_cron_no_ok_no_discovered_at_emits_zero(
    repo: SqliteRepository,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Branch 56->63: @reboot cron with last_ok_at=None and last_discovered_at=None
    → neither if-branch taken → should_emit_negative stays False → 0.0 emitted."""
    boot_epoch = 1700000000
    stat_file = tmp_path / "stat"
    stat_file.write_text(f"btime {boot_epoch}\n")
    monkeypatch.setenv("HM_HOST_PROC_DIR", str(tmp_path))

    # cadence_seconds=0 (@reboot), no state row, no last_discovered_at
    await _seed_cron(
        repo,
        fingerprint="fp-rb-no-disc",
        name="rb-no-disc",
        cadence_seconds=0,
        last_discovered_at=None,
    )
    # No state row inserted — last_ok_at will be NULL from LEFT JOIN

    writer = MemoryRetainingMetricsWriter()
    result = await HeartbeatStateCollector().run(_ctx(writer, repo))

    assert result.ok
    vals = _values_by_name(writer, "homelab_heartbeat_expected_next_seconds")
    assert len(vals) == 1
    assert vals[0] == 0.0


@pytest.mark.asyncio
async def test_seconds_since_naive_iso_adds_utc(
    repo: SqliteRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_seconds_since handles naive ISO timestamps (no tzinfo) — line 34 branch."""
    monkeypatch.setenv("HM_HOST_PROC_DIR", "/nonexistent-proc")
    # Use a naive ISO string (no +00:00 suffix) so tzinfo is None → line 34 executes
    naive_iso = datetime.now(UTC).replace(tzinfo=None).isoformat()
    await _seed_cron(repo, fingerprint="fp-naive-ok", name="naive-ok-job", cadence_seconds=3600)
    await _seed_state(
        repo,
        fingerprint="fp-naive-ok",
        last_ok_at=naive_iso,
        current_streak=1,
    )

    writer = MemoryRetainingMetricsWriter()
    result = await HeartbeatStateCollector().run(_ctx(writer, repo))

    assert result.ok
    assert "homelab_heartbeat_seconds_since_last_ok" in _names(writer)


@pytest.mark.asyncio
async def test_compute_reboot_naive_discovered_at_adds_utc(
    repo: SqliteRepository,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_compute_reboot_expected_next: naive discovered_at ISO → line 54 (tzinfo replace)."""
    boot_epoch = 1700000000
    stat_file = tmp_path / "stat"
    stat_file.write_text(f"btime {boot_epoch}\n")
    monkeypatch.setenv("HM_HOST_PROC_DIR", str(tmp_path))

    boot_dt = datetime.fromtimestamp(boot_epoch, tz=UTC)
    # Naive ISO, before boot → should_emit_negative = True
    discovered_before_boot_naive = (
        (boot_dt - timedelta(seconds=300)).replace(tzinfo=None).isoformat()
    )

    await _seed_cron(
        repo,
        fingerprint="fp-rb-naive-disc",
        name="rb-naive-disc",
        cadence_seconds=0,
        last_discovered_at=discovered_before_boot_naive,
    )
    # No state row — never pinged, so last_ok_at is None

    writer = MemoryRetainingMetricsWriter()
    result = await HeartbeatStateCollector().run(_ctx(writer, repo))

    assert result.ok
    vals = _values_by_name(writer, "homelab_heartbeat_expected_next_seconds")
    assert len(vals) == 1
    assert vals[0] < 0


@pytest.mark.asyncio
async def test_compute_reboot_naive_ok_at_adds_utc(
    repo: SqliteRepository,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_compute_reboot_expected_next: naive last_ok_at ISO → lines 59-60 (tzinfo replace)."""
    boot_epoch = 1700000000
    stat_file = tmp_path / "stat"
    stat_file.write_text(f"btime {boot_epoch}\n")
    monkeypatch.setenv("HM_HOST_PROC_DIR", str(tmp_path))

    boot_dt = datetime.fromtimestamp(boot_epoch, tz=UTC)
    # Naive ISO, before boot → ok_ts < host_boot_dt → should_emit_negative = True
    last_ok_before_boot_naive = (boot_dt - timedelta(seconds=60)).replace(tzinfo=None).isoformat()

    await _seed_cron(
        repo,
        fingerprint="fp-rb-naive-ok",
        name="rb-naive-ok",
        cadence_seconds=0,
    )
    await _seed_state(repo, fingerprint="fp-rb-naive-ok", last_ok_at=last_ok_before_boot_naive)

    writer = MemoryRetainingMetricsWriter()
    result = await HeartbeatStateCollector().run(_ctx(writer, repo))

    assert result.ok
    vals = _values_by_name(writer, "homelab_heartbeat_expected_next_seconds")
    assert len(vals) == 1
    assert vals[0] < 0


@pytest.mark.asyncio
async def test_emit_cron_metrics_exception_recorded_in_errors(
    repo: SqliteRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lines 213-214: ValueError/TypeError in _emit_cron_metrics → error appended, ok=False."""
    monkeypatch.setenv("HM_HOST_PROC_DIR", "/nonexistent-proc")
    # Seed a cron with cadence_seconds > 0 and an invalid (non-ISO) expected_next_at
    # so that datetime.fromisoformat() raises ValueError at line ~178.
    await _seed_cron(repo, fingerprint="fp-bad-ts", name="bad-ts-job", cadence_seconds=3600)
    await _seed_state(
        repo,
        fingerprint="fp-bad-ts",
        expected_next_at="not-a-date",
        current_streak=1,
    )

    writer = MemoryRetainingMetricsWriter()
    result = await HeartbeatStateCollector().run(_ctx(writer, repo))

    assert not result.ok
    assert any("fp-bad-ts" in e for e in result.errors)


@pytest.mark.asyncio
async def test_cron_hidden_after_emission_series_dropped(
    repo: SqliteRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: cron hidden between ticks → stale series cleared on tick 2.

    Uses ONE collector + ONE writer across TWO run() calls.  Without
    replace_family() the stale label-set would persist; with it the snapshot
    is clean after the cron is hidden.
    """
    monkeypatch.setenv("HM_HOST_PROC_DIR", "/nonexistent-proc")
    now_iso = utc_now_iso()

    await _seed_cron(repo, fingerprint="fp-vanish", name="vanishing-job", cadence_seconds=3600)
    await _seed_state(repo, fingerprint="fp-vanish", last_ok_at=now_iso, current_streak=1)

    writer = MemoryRetainingMetricsWriter()
    collector = HeartbeatStateCollector()
    ctx = _ctx(writer, repo)

    # Tick 1 — cron is active; series must be present.
    result1 = await collector.run(ctx)
    assert result1.ok
    all_labels_t1 = [e.labels for e in writer.snapshot()]
    assert any(lbl.get("name") == "vanishing-job" for lbl in all_labels_t1)

    # Between ticks: hide the cron.
    async with repo.engine.begin() as conn:
        await conn.execute(
            text("UPDATE crons SET hidden_at = :now WHERE fingerprint = 'fp-vanish'"),
            {"now": utc_now_iso()},
        )

    # Tick 2 — same collector, same writer; stale series must be gone.
    result2 = await collector.run(ctx)
    assert result2.ok
    all_labels_t2 = [e.labels for e in writer.snapshot()]
    assert all(lbl.get("name") != "vanishing-job" for lbl in all_labels_t2)
    assert all(lbl.get("fingerprint") != "fp-vanish" for lbl in all_labels_t2)


@pytest.mark.asyncio
async def test_cron_soft_deleted_after_emission_series_dropped(
    repo: SqliteRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: cron soft-deleted between ticks → stale series cleared on tick 2."""
    monkeypatch.setenv("HM_HOST_PROC_DIR", "/nonexistent-proc")
    now_iso = utc_now_iso()

    await _seed_cron(
        repo, fingerprint="fp-sda-vanish", name="sda-vanishing-job", cadence_seconds=3600
    )
    await _seed_state(repo, fingerprint="fp-sda-vanish", last_ok_at=now_iso, current_streak=1)

    writer = MemoryRetainingMetricsWriter()
    collector = HeartbeatStateCollector()
    ctx = _ctx(writer, repo)

    # Tick 1 — cron is active; series must be present.
    result1 = await collector.run(ctx)
    assert result1.ok
    all_labels_t1 = [e.labels for e in writer.snapshot()]
    assert any(lbl.get("name") == "sda-vanishing-job" for lbl in all_labels_t1)

    # Between ticks: soft-delete the cron.
    async with repo.engine.begin() as conn:
        await conn.execute(
            text("UPDATE crons SET soft_deleted_at = :now WHERE fingerprint = 'fp-sda-vanish'"),
            {"now": utc_now_iso()},
        )

    # Tick 2 — stale series must be gone.
    result2 = await collector.run(ctx)
    assert result2.ok
    all_labels_t2 = [e.labels for e in writer.snapshot()]
    assert all(lbl.get("name") != "sda-vanishing-job" for lbl in all_labels_t2)
    assert all(lbl.get("fingerprint") != "fp-sda-vanish" for lbl in all_labels_t2)


@pytest.mark.asyncio
async def test_all_families_cleared_when_no_crons(
    repo: SqliteRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: when all crons become hidden, every family is cleared (empty snapshot)."""
    monkeypatch.setenv("HM_HOST_PROC_DIR", "/nonexistent-proc")
    now_iso = utc_now_iso()

    await _seed_cron(repo, fingerprint="fp-only", name="only-job", cadence_seconds=3600)
    await _seed_state(repo, fingerprint="fp-only", last_ok_at=now_iso, current_streak=2)

    writer = MemoryRetainingMetricsWriter()
    collector = HeartbeatStateCollector()
    ctx = _ctx(writer, repo)

    # Tick 1 — snapshot has entries.
    result1 = await collector.run(ctx)
    assert result1.ok
    assert len(writer.snapshot()) > 0

    # Between ticks: hide the only cron → next query returns zero rows.
    async with repo.engine.begin() as conn:
        await conn.execute(
            text("UPDATE crons SET hidden_at = :now WHERE fingerprint = 'fp-only'"),
            {"now": utc_now_iso()},
        )

    # Tick 2 — every family replaced with empty list → snapshot must be empty.
    result2 = await collector.run(ctx)
    assert result2.ok
    assert writer.snapshot() == []


@pytest.mark.asyncio
async def test_run_with_non_retaining_writer_skips_family_replace(
    repo: SqliteRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """False branch of isinstance(ctx.vm, MemoryRetainingMetricsWriter) at line 270.

    When ctx.vm is a plain MetricsWriter (not MemoryRetainingMetricsWriter),
    the replace_family block is skipped.  The collector must still complete its
    run, return ok=True, and report a non-zero metrics_emitted count.
    """
    monkeypatch.setenv("HM_HOST_PROC_DIR", "/nonexistent-proc")

    class _PlainWriter:
        """Minimal MetricsWriter stub — NOT a MemoryRetainingMetricsWriter."""

        def __init__(self) -> None:
            self.writes: int = 0

        def write_gauge(  # pyright: ignore[reportUnusedParameter]
            self, name: str, value: float, labels: dict[str, str]
        ) -> None:
            self.writes += 1

        def write_counter(  # pyright: ignore[reportUnusedParameter]
            self, name: str, value: float, labels: dict[str, str]
        ) -> None:
            self.writes += 1

        def write_summary(  # pyright: ignore[reportUnusedParameter]
            self, name: str, value: float, labels: dict[str, str]
        ) -> None:
            self.writes += 1

    now_iso = utc_now_iso()
    await _seed_cron(repo, fingerprint="fp-plain", name="plain-job", cadence_seconds=3600)
    await _seed_state(repo, fingerprint="fp-plain", last_ok_at=now_iso, current_streak=1)

    plain_writer = _PlainWriter()
    ctx = CollectorContext(
        config=CollectorConfig(name="heartbeat_state"),
        db=repo,
        vm=plain_writer,  # pyright: ignore[reportArgumentType]
        vl=InMemoryLogsWriter(),
        http=None,  # pyright: ignore[reportArgumentType]
        ssh=None,  # pyright: ignore[reportArgumentType]
        secrets=None,  # pyright: ignore[reportArgumentType]
        log=structlog.get_logger().bind(collector="heartbeat_state"),  # pyright: ignore[reportArgumentType]
        ha=None,
    )

    result = await HeartbeatStateCollector().run(ctx)

    assert result.ok
    assert result.metrics_emitted > 0


@pytest.mark.asyncio
async def test_run_query_failure_returns_error_result(
    repo: SqliteRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lines 242-244: db query exception → ok=False, errors populated, metrics_emitted=0."""
    monkeypatch.setenv("HM_HOST_PROC_DIR", "/nonexistent-proc")

    async def _bad_fetch_all(_query: object) -> list[object]:
        raise RuntimeError("simulated db failure")

    writer = MemoryRetainingMetricsWriter()
    ctx = _ctx(writer, repo)
    monkeypatch.setattr(ctx.db, "fetch_all", _bad_fetch_all)

    result = await HeartbeatStateCollector().run(ctx)

    assert not result.ok
    assert result.metrics_emitted == 0
    assert any("query_failed" in e for e in result.errors)


@pytest.mark.asyncio
async def test_run_emits_metrics_via_multiplex_writer(
    repo: SqliteRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: production scenario — ctx.vm is MultiplexMetricsWriter.

    In production ctx.vm is NOT a bare MemoryRetainingMetricsWriter.

    The old isinstance-guarded code gated replace_family() on
    ``isinstance(ctx.vm, MemoryRetainingMetricsWriter)``.  In production,
    ctx.vm is a MultiplexMetricsWriter (NOT a subclass of
    MemoryRetainingMetricsWriter), so the guard was always False → zero metrics
    emitted.  The duck-typed fix (``getattr(ctx.vm, "replace_family", None)``)
    works because MultiplexMetricsWriter implements replace_family() and
    forwards it to its inner writers.

    This test FAILS against the old isinstance code (MultiplexMetricsWriter is
    not a MemoryRetainingMetricsWriter → guard False → no family replacement →
    inner writer snapshot empty after tick 2 series-clearing).  It passes with
    the duck-typed fix.
    """
    monkeypatch.setenv("HM_HOST_PROC_DIR", "/nonexistent-proc")
    now_iso = utc_now_iso()

    await _seed_cron(repo, fingerprint="fp-mplex", name="mplex-job", cadence_seconds=3600)
    await _seed_state(repo, fingerprint="fp-mplex", last_ok_at=now_iso, current_streak=1)

    # Mirror the production shape: MultiplexMetricsWriter wrapping a MemoryRetainingMetricsWriter.
    inner = MemoryRetainingMetricsWriter()
    multiplex = MultiplexMetricsWriter([inner])

    ctx = CollectorContext(
        config=CollectorConfig(name="heartbeat_state"),
        db=repo,
        vm=multiplex,  # pyright: ignore[reportArgumentType]
        vl=InMemoryLogsWriter(),
        http=None,  # pyright: ignore[reportArgumentType]
        ssh=None,  # pyright: ignore[reportArgumentType]
        secrets=None,  # pyright: ignore[reportArgumentType]
        log=structlog.get_logger().bind(collector="heartbeat_state"),  # pyright: ignore[reportArgumentType]
        ha=None,
    )

    result = await HeartbeatStateCollector().run(ctx)

    assert result.ok
    assert result.metrics_emitted > 0

    # The MultiplexMetricsWriter must have forwarded replace_family() to the inner
    # MemoryRetainingMetricsWriter, so metrics are visible in the inner writer's snapshot.
    inner_names = {e.name for e in inner.snapshot()}
    assert "homelab_heartbeat_seconds_since_last_ok" in inner_names
    assert "homelab_heartbeat_current_streak" in inner_names
    assert "homelab_heartbeat_logscrape_count_since_last_heartbeat" in inner_names
