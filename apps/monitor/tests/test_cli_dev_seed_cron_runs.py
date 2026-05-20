"""Tests for ``hm dev`` CLI — seed-cron-runs and clear-cron-runs (STAGE-002-015)."""

from __future__ import annotations

import argparse

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from homelab_monitor.cli import dev as dev_cli
from homelab_monitor.cli.dev import (
    _cmd_clear_cron_runs,  # pyright: ignore[reportPrivateUsage]
    _cmd_seed_cron_runs,  # pyright: ignore[reportPrivateUsage]
    _handle,  # pyright: ignore[reportPrivateUsage]
)
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.db.time import utc_now_iso

# ---------------------------------------------------------------------------
# DB seed helper (mirrors test_heartbeat_collector._seed_cron pattern)
# ---------------------------------------------------------------------------


async def _seed_cron(
    repo: SqliteRepository,
    *,
    fingerprint: str,
    host: str = "local-test-host",
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
                ":fp, 'test-job', :host, '/cmd', '* * * * *', '* * * * *', 3600, "
                "300, 1, 'unknown', :now, :now, NULL, NULL, NULL, 0, NULL, NULL)"
            ),
            {"fp": fingerprint, "host": host, "now": now},
        )


async def _count_runs(repo: SqliteRepository, fingerprint: str) -> int:
    async with repo.engine.connect() as conn:
        result = await conn.execute(
            text("SELECT COUNT(*) FROM cron_runs WHERE cron_fingerprint = :fp"),
            {"fp": fingerprint},
        )
        return int(result.scalar() or 0)


async def _count_by_state(repo: SqliteRepository, fingerprint: str, state: str) -> int:
    async with repo.engine.connect() as conn:
        result = await conn.execute(
            text("SELECT COUNT(*) FROM cron_runs WHERE cron_fingerprint = :fp AND state = :state"),
            {"fp": fingerprint, "state": state},
        )
        return int(result.scalar() or 0)


async def _count_by_source(repo: SqliteRepository, fingerprint: str, source: str) -> int:
    async with repo.engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT COUNT(*) FROM cron_runs WHERE cron_fingerprint = :fp AND source = :source"
            ),
            {"fp": fingerprint, "source": source},
        )
        return int(result.scalar() or 0)


async def _count_overlapping(repo: SqliteRepository, fingerprint: str) -> int:
    async with repo.engine.connect() as conn:
        result = await conn.execute(
            text("SELECT COUNT(*) FROM cron_runs WHERE cron_fingerprint = :fp AND overlapping = 1"),
            {"fp": fingerprint},
        )
        return int(result.scalar() or 0)


async def _count_with_anomaly_flags(repo: SqliteRepository, fingerprint: str) -> int:
    async with repo.engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT COUNT(*) FROM cron_runs "
                "WHERE cron_fingerprint = :fp AND anomaly_flags != ''"
            ),
            {"fp": fingerprint},
        )
        return int(result.scalar() or 0)


async def _count_multi_flag_anomalies(repo: SqliteRepository, fingerprint: str) -> int:
    async with repo.engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT COUNT(*) FROM cron_runs "
                "WHERE cron_fingerprint = :fp AND anomaly_flags LIKE '%,%'"
            ),
            {"fp": fingerprint},
        )
        return int(result.scalar() or 0)


# ---------------------------------------------------------------------------
# Dispatch tests (no DB required)
# ---------------------------------------------------------------------------


class TestHandleDispatch:
    def test_dispatch_seed_routes_to_cmd_seed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """_handle dispatches dev_cmd='seed-cron-runs' to _cmd_seed_cron_runs."""
        called: list[str] = []

        async def fake_seed(fingerprint: str, *, force: bool) -> int:
            called.append(fingerprint)
            return 0

        monkeypatch.setattr(dev_cli, "_cmd_seed_cron_runs", fake_seed)
        args = argparse.Namespace(dev_cmd="seed-cron-runs", fingerprint="fp-test", force=False)
        rc = _handle(args)
        assert rc == 0
        assert called == ["fp-test"]

    def test_dispatch_clear_routes_to_cmd_clear(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """_handle dispatches dev_cmd='clear-cron-runs' to _cmd_clear_cron_runs."""
        called: list[str] = []

        async def fake_clear(fingerprint: str, *, force: bool) -> int:
            called.append(fingerprint)
            return 0

        monkeypatch.setattr(dev_cli, "_cmd_clear_cron_runs", fake_clear)
        args = argparse.Namespace(dev_cmd="clear-cron-runs", fingerprint="fp-test", force=False)
        rc = _handle(args)
        assert rc == 0
        assert called == ["fp-test"]

    def test_missing_subcommand_returns_2(self, capsys: pytest.CaptureFixture[str]) -> None:
        """_handle without dev_cmd attribute prints usage and returns 2."""
        args = argparse.Namespace()
        rc = _handle(args)
        assert rc == 2  # noqa: PLR2004
        captured = capsys.readouterr()
        assert "usage: hm dev" in captured.err


# ---------------------------------------------------------------------------
# Integration tests (use real migrated DB via `repo` fixture)
# ---------------------------------------------------------------------------

LOCAL_HOST = "local-test-host"
FP = "seed-test-fp-001"


@pytest.fixture
def local_dev_env(repo: SqliteRepository, monkeypatch: pytest.MonkeyPatch) -> None:
    def _hostname() -> str:
        return LOCAL_HOST

    def _engine() -> AsyncEngine:
        return repo.engine

    monkeypatch.setattr("homelab_monitor.cli.dev.resolve_hostname", _hostname)
    monkeypatch.setattr("homelab_monitor.cli.dev.get_engine", _engine)


@pytest.mark.asyncio
async def test_seed_inserts_expected_rows(
    repo: SqliteRepository, monkeypatch: pytest.MonkeyPatch, local_dev_env: None
) -> None:
    """seed-cron-runs inserts 59 closed rows + 1 running row = 59+ total."""
    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", str(repo.engine.url))
    await _seed_cron(repo, fingerprint=FP, host=LOCAL_HOST)

    rc = await _cmd_seed_cron_runs(FP, force=False)
    assert rc == 0
    count = await _count_runs(repo, FP)
    # 50 ok + 5 fail + 3 unknown + 1 running = 59
    assert count >= 59  # noqa: PLR2004


@pytest.mark.asyncio
async def test_seed_state_mix(repo: SqliteRepository, local_dev_env: None) -> None:
    """seed-cron-runs creates exactly 50 ok, 5 fail, 3 unknown, 1 running rows."""
    await _seed_cron(repo, fingerprint=FP, host=LOCAL_HOST)

    rc = await _cmd_seed_cron_runs(FP, force=False)
    assert rc == 0

    assert await _count_by_state(repo, FP, "ok") == 50  # noqa: PLR2004
    assert await _count_by_state(repo, FP, "fail") == 5  # noqa: PLR2004
    assert await _count_by_state(repo, FP, "unknown") == 3  # noqa: PLR2004
    assert await _count_by_state(repo, FP, "running") == 1


@pytest.mark.asyncio
async def test_seed_source_mix(repo: SqliteRepository, local_dev_env: None) -> None:
    """seed-cron-runs creates both wrapper and logscrape source rows."""
    await _seed_cron(repo, fingerprint=FP, host=LOCAL_HOST)

    await _cmd_seed_cron_runs(FP, force=False)

    wrapper_count = await _count_by_source(repo, FP, "wrapper")
    logscrape_count = await _count_by_source(repo, FP, "logscrape")
    assert wrapper_count >= 45  # noqa: PLR2004
    assert logscrape_count >= 5  # noqa: PLR2004


@pytest.mark.asyncio
async def test_seed_overlapping_count(repo: SqliteRepository, local_dev_env: None) -> None:
    """seed-cron-runs marks exactly 2 rows as overlapping."""
    await _seed_cron(repo, fingerprint=FP, host=LOCAL_HOST)

    await _cmd_seed_cron_runs(FP, force=False)

    assert await _count_overlapping(repo, FP) >= 2  # noqa: PLR2004


@pytest.mark.asyncio
async def test_seed_anomaly_flags_count(repo: SqliteRepository, local_dev_env: None) -> None:
    """seed-cron-runs sets anomaly_flags on >= 5 rows; at least 1 has multiple flags."""
    await _seed_cron(repo, fingerprint=FP, host=LOCAL_HOST)

    await _cmd_seed_cron_runs(FP, force=False)

    with_flags = await _count_with_anomaly_flags(repo, FP)
    assert with_flags >= 5  # noqa: PLR2004

    multi_flag = await _count_multi_flag_anomalies(repo, FP)
    assert multi_flag >= 1


@pytest.mark.asyncio
async def test_seed_idempotent_on_rerun(repo: SqliteRepository, local_dev_env: None) -> None:
    """Running seed-cron-runs twice keeps the same row count (INSERT OR IGNORE)."""
    await _seed_cron(repo, fingerprint=FP, host=LOCAL_HOST)

    await _cmd_seed_cron_runs(FP, force=False)
    count_first = await _count_runs(repo, FP)

    await _cmd_seed_cron_runs(FP, force=False)
    count_second = await _count_runs(repo, FP)

    assert count_first == count_second


@pytest.mark.asyncio
async def test_seed_refuses_unknown_fingerprint(
    repo: SqliteRepository,
    local_dev_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """seed-cron-runs exits 1 with 'cron not found' for unknown fingerprint."""

    rc = await _cmd_seed_cron_runs("nonexistent-fp", force=False)
    assert rc == 1
    captured = capsys.readouterr()
    assert "cron not found" in captured.err


@pytest.mark.asyncio
async def test_seed_refuses_remote_host_without_force(
    repo: SqliteRepository,
    local_dev_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """seed-cron-runs exits 1 with 'not local' message for remote-host cron."""
    remote_fp = "remote-fp-001"
    await _seed_cron(repo, fingerprint=remote_fp, host="other-host")

    rc = await _cmd_seed_cron_runs(remote_fp, force=False)
    assert rc == 1
    captured = capsys.readouterr()
    assert "not local" in captured.err or "Pass --force" in captured.err


@pytest.mark.asyncio
async def test_seed_accepts_remote_with_force(repo: SqliteRepository, local_dev_env: None) -> None:
    """seed-cron-runs with force=True succeeds for a non-local cron."""
    remote_fp = "remote-fp-002"
    await _seed_cron(repo, fingerprint=remote_fp, host="other-host")

    rc = await _cmd_seed_cron_runs(remote_fp, force=True)
    assert rc == 0
    count = await _count_runs(repo, remote_fp)
    assert count >= 59  # noqa: PLR2004


@pytest.mark.asyncio
async def test_clear_removes_all_rows_for_fingerprint(
    repo: SqliteRepository, local_dev_env: None
) -> None:
    """clear-cron-runs removes all cron_runs rows seeded for a fingerprint."""
    await _seed_cron(repo, fingerprint=FP, host=LOCAL_HOST)

    await _cmd_seed_cron_runs(FP, force=False)
    assert await _count_runs(repo, FP) > 0

    rc = await _cmd_clear_cron_runs(FP, force=False)
    assert rc == 0
    assert await _count_runs(repo, FP) == 0


@pytest.mark.asyncio
async def test_clear_refuses_remote_without_force(
    repo: SqliteRepository,
    local_dev_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """clear-cron-runs exits 1 for a non-local cron without --force."""
    remote_fp = "remote-fp-003"
    await _seed_cron(repo, fingerprint=remote_fp, host="other-host")

    rc = await _cmd_clear_cron_runs(remote_fp, force=False)
    assert rc == 1
    captured = capsys.readouterr()
    assert "not local" in captured.err or "Pass --force" in captured.err


@pytest.mark.asyncio
async def test_seed_skips_out_of_range_anomaly_assignment(
    repo: SqliteRepository, local_dev_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """seed-cron-runs silently skips anomaly assignments with closed_idx >= len(closed)."""
    # Replace _ANOMALY_ASSIGNMENTS with a single entry whose index is guaranteed
    # to exceed len(closed) (50+5+3=58), exercising the False branch of the guard.
    monkeypatch.setattr(
        "homelab_monitor.cli.dev._ANOMALY_ASSIGNMENTS",
        [(999, "duration_outlier")],
    )
    await _seed_cron(repo, fingerprint=FP, host=LOCAL_HOST)

    rc = await _cmd_seed_cron_runs(FP, force=False)
    assert rc == 0

    # Because the only assignment (index 999) was out of range, no run should
    # carry the anomaly flag.
    with_flags = await _count_with_anomaly_flags(repo, FP)
    assert with_flags == 0


@pytest.mark.asyncio
async def test_seed_unknown_rows_have_null_duration(
    repo: SqliteRepository, local_dev_env: None
) -> None:
    """unknown-state seeded rows must have duration_seconds IS NULL (not 0.0)."""
    await _seed_cron(repo, fingerprint=FP, host=LOCAL_HOST)
    rc = await _cmd_seed_cron_runs(FP, force=False)
    assert rc == 0

    async with repo.engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT COUNT(*) FROM cron_runs "
                "WHERE cron_fingerprint = :fp AND state = 'unknown' "
                "AND duration_seconds IS NOT NULL"
            ),
            {"fp": FP},
        )
        non_null_count = int(result.scalar() or 0)
    assert non_null_count == 0
