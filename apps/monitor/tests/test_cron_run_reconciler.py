"""Unit tests for CronRunReconciler (STAGE-002-013).

Uses the `repo` fixture (real in-memory migrated DB), httpx_mock for VL HTTP
interception, and a minimal CollectorContext following the heartbeat_collector
test pattern (MemoryRetainingMetricsWriter + InMemoryLogsWriter + real AsyncClient).

Project test conventions:
- @pytest.mark.asyncio for async tests
- noqa: PLR2004 for magic number assertions
- noqa: PLC0415 for function-scoped imports
- pyright: ignore[reportPrivateUsage] for private symbol access
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import structlog
from pytest_httpx import HTTPXMock
from sqlalchemy import text

from homelab_monitor.kernel.cron.run_repository import CronRunRepository
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.db.time import utc_now_iso
from homelab_monitor.kernel.metrics.cron_run_reconciler import (
    CronRunReconciler,
    _normalize_for_digest,  # pyright: ignore[reportPrivateUsage]
    _parse_iso,  # pyright: ignore[reportPrivateUsage]
    compute_content_digest,
)
from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.io import InMemoryLogsWriter, MemoryRetainingMetricsWriter
from homelab_monitor.kernel.plugins.types import CollectorConfig

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ctx(repo: SqliteRepository, http: httpx.AsyncClient) -> CollectorContext:
    """Minimal CollectorContext for CronRunReconciler."""
    return CollectorContext(
        config=CollectorConfig(name="cron_run_reconciler"),
        db=repo,
        vm=MemoryRetainingMetricsWriter(),
        vl=InMemoryLogsWriter(),
        http=http,
        ssh=None,  # pyright: ignore[reportArgumentType]
        secrets=None,  # pyright: ignore[reportArgumentType]
        log=structlog.get_logger().bind(
            collector="cron_run_reconciler",
        ),  # pyright: ignore[reportArgumentType]
        ha=None,
    )


async def _insert_bmode_run(
    repo: SqliteRepository,
    *,
    run_id: str,
    cron_fingerprint: str,
    started_at: str,
) -> None:
    run_repo = CronRunRepository(repo)
    await run_repo.insert_run(
        run_id=run_id,
        cron_fingerprint=cron_fingerprint,
        source="logscrape",
        started_at=started_at,
        vl_window_start=started_at,
    )


async def _insert_closed_run(  # noqa: PLR0913
    repo: SqliteRepository,
    *,
    run_id: str,
    cron_fingerprint: str,
    source: str = "wrapper",
    state: str = "ok",
    ended_at: str,
) -> None:
    run_repo = CronRunRepository(repo)
    await run_repo.close_run(
        run_id=run_id,
        cron_fingerprint=cron_fingerprint,
        source=source,
        state=state,
        ended_at=ended_at,
        duration_seconds=10.0,
        exit_code=0,
        vl_window_end=ended_at,
    )


async def _insert_cron(
    repo: SqliteRepository,
    *,
    fingerprint: str,
    command: str = "/usr/bin/backup.sh",
    host: str = "h1",
) -> None:
    """Insert a minimal crons row for enrich B-mode path."""
    from homelab_monitor.kernel.cron.log_match import canonical_log_key  # noqa: PLC0415

    lmk = canonical_log_key(command)
    now = utc_now_iso()
    async with repo.engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT OR IGNORE INTO crons ("
                "  fingerprint, name, host, command, schedule, schedule_canonical,"
                "  cadence_seconds, expected_grace_seconds, enabled, last_seen_state,"
                "  created_at, updated_at, hidden_at, source_path, wrapper_last_seen_at,"
                "  last_discovered_at, soft_deleted_at, log_match_key"
                ") VALUES ("
                "  :fp, :name, :host, :cmd, '* * * * *', '* * * * *',"
                "  60, 300, 1, 'unknown',"
                "  :now, :now, NULL, '/etc/crontab', NULL,"
                "  :now, NULL, :lmk"
                ")"
            ),
            {
                "fp": fingerprint,
                "name": "test-cron",
                "host": host,
                "cmd": command,
                "now": now,
                "lmk": lmk,
            },
        )


def _ndjson_line(msg: str = "log line", ts: str = "2026-05-19T00:00:00+00:00") -> str:
    return json.dumps({"_stream_id": "s", "_msg": msg, "_time": ts})


# ---------------------------------------------------------------------------
# compute_content_digest unit tests
# ---------------------------------------------------------------------------


def test_compute_content_digest_empty_list() -> None:
    """compute_content_digest([]) returns the sha256 of the empty string."""
    import hashlib  # noqa: PLC0415

    result = compute_content_digest([])
    expected = hashlib.sha256(b"").hexdigest()
    assert result == expected
    assert len(result) == 64  # noqa: PLR2004


def test_compute_content_digest_same_shape_different_timestamps() -> None:
    """Messages differing only in timestamps produce the SAME digest."""
    msgs1 = ["Job started at 2026-05-19T00:00:00Z, processed 42 items"]
    msgs2 = ["Job started at 2026-05-20T12:34:56Z, processed 99 items"]
    assert compute_content_digest(msgs1) == compute_content_digest(msgs2)


def test_compute_content_digest_same_shape_different_pids() -> None:
    """Messages differing only in [pid] brackets produce the SAME digest."""
    msgs1 = ["crond[1234]: started"]
    msgs2 = ["crond[9999]: started"]
    assert compute_content_digest(msgs1) == compute_content_digest(msgs2)


def test_compute_content_digest_same_shape_different_integers() -> None:
    """Messages differing only in standalone integers produce the SAME digest."""
    msgs1 = ["processed 100 files in 5 seconds"]
    msgs2 = ["processed 999 files in 12 seconds"]
    assert compute_content_digest(msgs1) == compute_content_digest(msgs2)


def test_compute_content_digest_different_words_produce_different_digests() -> None:
    """Messages with different actual words produce DIFFERENT digests."""
    msgs1 = ["backup completed successfully"]
    msgs2 = ["backup FAILED with error"]
    assert compute_content_digest(msgs1) != compute_content_digest(msgs2)


def test_compute_content_digest_syslog_timestamp_normalized() -> None:
    """Syslog-style timestamps (May 19 00:00:00) are stripped before digesting."""
    msgs1 = ["May 19 00:00:00 host crond: started"]
    msgs2 = ["Jun  3 12:34:56 host crond: started"]
    assert compute_content_digest(msgs1) == compute_content_digest(msgs2)


def test_normalize_for_digest_iso_timestamp_stripped() -> None:
    """ISO timestamps are stripped from the message."""
    result = _normalize_for_digest("started at 2026-05-19T00:00:00+00:00 done")
    assert "2026" not in result
    assert "done" in result


def test_normalize_for_digest_pid_stripped() -> None:
    """[pid] brackets are stripped."""
    result = _normalize_for_digest("crond[1234]: message")
    assert "[1234]" not in result
    assert "crond" in result
    assert "message" in result


# ---------------------------------------------------------------------------
# window-finalize tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_window_finalize_next_cmd_closes_older_run(
    repo: SqliteRepository,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two B-mode runs: older run is closed at newer run's started_at (next-CMD rule)."""
    monkeypatch.setenv("HOMELAB_MONITOR_VL_URL", "http://vl-test:9428")
    # Anchor both runs relative to "now" and well within the 6h timeout cap so
    # the newest run is NOT timeout-closed (regression: fixed dates age past 6h).
    # No crons row is seeded for fp-nextcmd, so the enrich phase issues zero VL
    # requests (cron is None -> continue). Registering a response would trip
    # pytest_httpx's "mocked but not requested" assertion.
    fp = "fp-nextcmd"
    t1 = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
    t2 = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()  # 5 minutes after t1

    await _insert_bmode_run(repo, run_id="run-t1", cron_fingerprint=fp, started_at=t1)
    await _insert_bmode_run(repo, run_id="run-t2", cron_fingerprint=fp, started_at=t2)

    async with httpx.AsyncClient() as http:
        result = await CronRunReconciler().run(_ctx(repo, http))

    assert result.ok is True

    run_repo = CronRunRepository(repo)
    closed = await run_repo.get_run("run-t1")
    still_open = await run_repo.get_run("run-t2")

    assert closed is not None
    assert closed.state == "unknown"
    assert closed.ended_at == t2
    assert closed.vl_window_end == t2
    assert closed.duration_seconds is not None
    assert closed.duration_seconds == pytest.approx(300.0)  # pyright: ignore[reportUnknownMemberType]

    assert still_open is not None
    assert still_open.state == "running"  # latest run is left open


@pytest.mark.asyncio
async def test_window_finalize_timeout_closes_old_run_with_overlapping(
    repo: SqliteRepository,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A B-mode run started 7h ago is closed by 6h timeout cap; overlapping=True."""
    monkeypatch.setenv("HOMELAB_MONITOR_VL_URL", "http://vl-test:9428")
    monkeypatch.setenv("HOMELAB_MONITOR_CRON_RUN_BMODE_TIMEOUT_HOURS", "6")

    fp = "fp-timeout"
    # started_at 7 hours before "now" (reconciler uses datetime.now(UTC))
    started_at = (datetime.now(UTC) - timedelta(hours=7)).isoformat()

    await _insert_bmode_run(repo, run_id="run-old", cron_fingerprint=fp, started_at=started_at)

    async with httpx.AsyncClient() as http:
        result = await CronRunReconciler().run(_ctx(repo, http))

    assert result.ok is True

    run_repo = CronRunRepository(repo)
    row = await run_repo.get_run("run-old")
    assert row is not None
    assert row.state == "unknown"
    assert row.overlapping is True
    assert row.ended_at is not None
    assert row.duration_seconds == pytest.approx(6 * 3600)  # pyright: ignore[reportUnknownMemberType]


@pytest.mark.asyncio
async def test_window_finalize_no_op_when_within_timeout(
    repo: SqliteRepository,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A B-mode run started 1h ago (within 6h timeout) is left running."""
    monkeypatch.setenv("HOMELAB_MONITOR_VL_URL", "http://vl-test:9428")
    monkeypatch.setenv("HOMELAB_MONITOR_CRON_RUN_BMODE_TIMEOUT_HOURS", "6")

    fp = "fp-within"
    started_at = (datetime.now(UTC) - timedelta(hours=1)).isoformat()

    await _insert_bmode_run(repo, run_id="run-recent", cron_fingerprint=fp, started_at=started_at)

    async with httpx.AsyncClient() as http:
        result = await CronRunReconciler().run(_ctx(repo, http))

    assert result.ok is True

    run_repo = CronRunRepository(repo)
    row = await run_repo.get_run("run-recent")
    assert row is not None
    assert row.state == "running"


@pytest.mark.asyncio
async def test_window_finalize_ignores_amode_runs(
    repo: SqliteRepository,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wrapper (A-mode) running run is NOT touched by window-finalize."""
    monkeypatch.setenv("HOMELAB_MONITOR_VL_URL", "http://vl-test:9428")

    # Insert an A-mode run started 10h ago
    run_repo = CronRunRepository(repo)
    started_at = (datetime.now(UTC) - timedelta(hours=10)).isoformat()
    await run_repo.insert_run(
        run_id="amode-run",
        cron_fingerprint="fp-amode",
        source="wrapper",
        started_at=started_at,
        vl_window_start=started_at,
    )

    async with httpx.AsyncClient() as http:
        result = await CronRunReconciler().run(_ctx(repo, http))

    assert result.ok is True

    row = await run_repo.get_run("amode-run")
    assert row is not None
    assert row.state == "running"  # untouched


# ---------------------------------------------------------------------------
# enrich tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enrich_amode_run_sets_vl_fields(
    repo: SqliteRepository,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Enrich: closed wrapper run gets line_count/byte_count/content_digest/enriched_at."""
    monkeypatch.setenv("HOMELAB_MONITOR_VL_URL", "http://vl-test:9428")
    monkeypatch.setenv("HOMELAB_MONITOR_CRON_RUN_ENRICH_GRACE_SECONDS", "5")

    run_id = "amode-enrich-1"
    ended_at = (datetime.now(UTC) - timedelta(seconds=60)).isoformat()

    await _insert_closed_run(repo, run_id=run_id, cron_fingerprint="fp-ae", ended_at=ended_at)

    # Mock VL to return 3 lines
    lines = "\n".join([_ndjson_line(f"msg-{i}") for i in range(3)])
    httpx_mock.add_response(method="GET", text=lines + "\n")

    async with httpx.AsyncClient() as http:
        result = await CronRunReconciler().run(_ctx(repo, http))

    assert result.ok is True

    run_repo = CronRunRepository(repo)
    row = await run_repo.get_run(run_id)
    assert row is not None
    assert row.line_count == 3  # noqa: PLR2004
    assert row.byte_count is not None and row.byte_count > 0
    assert row.content_digest is not None
    assert len(row.content_digest) == 64  # noqa: PLR2004  sha256 hex
    assert row.enriched_at is not None
    # anomaly_flags must remain empty
    assert row.anomaly_flags == ""


@pytest.mark.asyncio
async def test_enrich_amode_query_contains_run_id_and_syslog_identifier(
    repo: SqliteRepository,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Enrich A-mode: the VL request URL contains the correct LogsQL query."""
    monkeypatch.setenv("HOMELAB_MONITOR_VL_URL", "http://vl-test:9428")
    monkeypatch.setenv("HOMELAB_MONITOR_CRON_RUN_ENRICH_GRACE_SECONDS", "5")

    run_id = "amode-query-check"
    ended_at = (datetime.now(UTC) - timedelta(seconds=60)).isoformat()

    await _insert_closed_run(repo, run_id=run_id, cron_fingerprint="fp-aq", ended_at=ended_at)

    httpx_mock.add_response(method="GET", text="")

    async with httpx.AsyncClient() as http:
        await CronRunReconciler().run(_ctx(repo, http))

    requests = httpx_mock.get_requests()
    assert len(requests) >= 1
    query_param = requests[0].url.params.get("query", "")
    assert "SYSLOG_IDENTIFIER:hmrun" in query_param
    assert f'run_id:"{run_id}"' in query_param


@pytest.mark.asyncio
async def test_enrich_bmode_run_uses_canonical_key_query(
    repo: SqliteRepository,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Enrich B-mode: the VL request uses the canonical-key phrase query."""
    from homelab_monitor.kernel.cron.log_match import canonical_log_key  # noqa: PLC0415

    monkeypatch.setenv("HOMELAB_MONITOR_VL_URL", "http://vl-test:9428")
    monkeypatch.setenv("HOMELAB_MONITOR_CRON_RUN_ENRICH_GRACE_SECONDS", "5")

    command = "/usr/bin/backup.sh"
    fp = "fp-bmode-enrich"
    await _insert_cron(repo, fingerprint=fp, command=command)

    ended_at = (datetime.now(UTC) - timedelta(seconds=60)).isoformat()
    await _insert_closed_run(
        repo, run_id="bmode-enrich-1", cron_fingerprint=fp, source="logscrape", ended_at=ended_at
    )

    httpx_mock.add_response(method="GET", text=_ndjson_line("output") + "\n")

    async with httpx.AsyncClient() as http:
        await CronRunReconciler().run(_ctx(repo, http))

    requests = httpx_mock.get_requests()
    assert len(requests) >= 1
    query_param = requests[0].url.params.get("query", "")
    expected_key = canonical_log_key(command)
    assert expected_key in query_param


@pytest.mark.asyncio
async def test_enrich_grace_gate_skips_recently_ended_run(
    repo: SqliteRepository,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Enrich grace: a run ended 5s ago with 15s grace is NOT enriched this tick."""
    monkeypatch.setenv("HOMELAB_MONITOR_VL_URL", "http://vl-test:9428")
    monkeypatch.setenv("HOMELAB_MONITOR_CRON_RUN_ENRICH_GRACE_SECONDS", "15")

    run_id = "grace-run"
    ended_at = (datetime.now(UTC) - timedelta(seconds=5)).isoformat()
    await _insert_closed_run(repo, run_id=run_id, cron_fingerprint="fp-grace", ended_at=ended_at)

    async with httpx.AsyncClient() as http:
        result = await CronRunReconciler().run(_ctx(repo, http))

    assert result.ok is True

    run_repo = CronRunRepository(repo)
    row = await run_repo.get_run(run_id)
    assert row is not None
    assert row.enriched_at is None  # not enriched yet


@pytest.mark.asyncio
async def test_enrich_skipped_when_cron_deleted(
    repo: SqliteRepository,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Enrich B-mode: if the cron row is gone, the run is skipped (no crash)."""
    monkeypatch.setenv("HOMELAB_MONITOR_VL_URL", "http://vl-test:9428")
    monkeypatch.setenv("HOMELAB_MONITOR_CRON_RUN_ENRICH_GRACE_SECONDS", "5")

    fp = "fp-deleted-cron"
    ended_at = (datetime.now(UTC) - timedelta(seconds=60)).isoformat()
    # Insert a closed logscrape run with NO corresponding cron row
    await _insert_closed_run(
        repo, run_id="orphan-run", cron_fingerprint=fp, source="logscrape", ended_at=ended_at
    )

    async with httpx.AsyncClient() as http:
        result = await CronRunReconciler().run(_ctx(repo, http))

    assert result.ok is True

    run_repo = CronRunRepository(repo)
    row = await run_repo.get_run("orphan-run")
    assert row is not None
    assert row.enriched_at is None  # skipped, not enriched


@pytest.mark.asyncio
async def test_enrich_queue_includes_non_ok_states(
    repo: SqliteRepository,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Enrich queue processes runs with state='unknown' and state='fail', not only 'ok'."""
    monkeypatch.setenv("HOMELAB_MONITOR_VL_URL", "http://vl-test:9428")
    monkeypatch.setenv("HOMELAB_MONITOR_CRON_RUN_ENRICH_GRACE_SECONDS", "5")

    # Insert two closed un-enriched runs: one with state='fail', one with state='unknown'
    ended_at = (datetime.now(UTC) - timedelta(seconds=60)).isoformat()

    fail_run_id = "run-fail-state"
    await _insert_closed_run(
        repo, run_id=fail_run_id, cron_fingerprint="fp-fail", state="fail", ended_at=ended_at
    )

    unknown_run_id = "run-unknown-state"
    await _insert_closed_run(
        repo,
        run_id=unknown_run_id,
        cron_fingerprint="fp-unknown",
        state="unknown",
        ended_at=ended_at,
    )

    # Mock VL to return empty NDJSON (valid response for both runs)
    httpx_mock.add_response(method="GET", text="", is_reusable=True)

    async with httpx.AsyncClient() as http:
        result = await CronRunReconciler().run(_ctx(repo, http))

    assert result.ok is True

    run_repo = CronRunRepository(repo)

    # Both runs must be enriched (enriched_at IS NOT NULL)
    fail_row = await run_repo.get_run(fail_run_id)
    assert fail_row is not None
    assert fail_row.enriched_at is not None

    unknown_row = await run_repo.get_run(unknown_run_id)
    assert unknown_row is not None
    assert unknown_row.enriched_at is not None


# ---------------------------------------------------------------------------
# VL-down behavior
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_vl_down_enrich_skipped_but_finalize_and_prune_run(
    repo: SqliteRepository,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """VL down: enrich is skipped; window-finalize and prune still run; result.ok is True."""
    monkeypatch.setenv("HOMELAB_MONITOR_VL_URL", "http://vl-test:9428")
    monkeypatch.setenv("HOMELAB_MONITOR_CRON_RUN_ENRICH_GRACE_SECONDS", "5")
    monkeypatch.setenv("HOMELAB_MONITOR_CRON_RUN_BMODE_TIMEOUT_HOURS", "6")
    monkeypatch.setenv("HOMELAB_MONITOR_CRON_RUN_RETENTION_DAYS", "30")

    # Seed a closed run needing enrich
    ended_at = (datetime.now(UTC) - timedelta(seconds=60)).isoformat()
    await _insert_closed_run(
        repo, run_id="enrich-target", cron_fingerprint="fp-vl-down", ended_at=ended_at
    )

    # Seed an old B-mode run past timeout (window-finalize should close it)
    old_started = (datetime.now(UTC) - timedelta(hours=7)).isoformat()
    await _insert_bmode_run(
        repo, run_id="timeout-run", cron_fingerprint="fp-timeout2", started_at=old_started
    )

    # Seed a prunable old row
    run_repo = CronRunRepository(repo)
    await run_repo.close_run(
        run_id="old-to-prune",
        cron_fingerprint="fp-prune2",
        source="wrapper",
        state="ok",
        ended_at="2020-01-01T00:00:00+00:00",
        duration_seconds=None,
        exit_code=0,
        vl_window_end="2020-01-01T00:00:00+00:00",
    )

    # VL is DOWN — every HTTP request raises ConnectError
    httpx_mock.add_exception(httpx.ConnectError("refused"), is_reusable=True)

    async with httpx.AsyncClient() as http:
        result = await CronRunReconciler().run(_ctx(repo, http))

    # result.ok must be True (VL down is not an error)
    assert result.ok is True

    # Enrich must be skipped
    enrich_row = await run_repo.get_run("enrich-target")
    assert enrich_row is not None
    assert enrich_row.enriched_at is None

    # Window-finalize must have run: timeout-run should be closed
    timeout_row = await run_repo.get_run("timeout-run")
    assert timeout_row is not None
    assert timeout_row.state == "unknown"

    # Prune must have run: old-to-prune should be deleted
    assert await run_repo.get_run("old-to-prune") is None


# ---------------------------------------------------------------------------
# Prune tests via reconciler tick
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prune_by_age_via_reconciler(
    repo: SqliteRepository,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """prune phase: runs older than retention_days are deleted on each tick."""
    monkeypatch.setenv("HOMELAB_MONITOR_VL_URL", "http://vl-test:9428")
    monkeypatch.setenv("HOMELAB_MONITOR_CRON_RUN_RETENTION_DAYS", "30")
    monkeypatch.setenv("HOMELAB_MONITOR_CRON_RUN_ENRICH_GRACE_SECONDS", "5")

    # The closed run also satisfies the enrich queue; mock VL so the enrich
    # phase succeeds harmlessly instead of issuing an unexpected request.
    httpx_mock.add_response(method="GET", text="")

    run_repo = CronRunRepository(repo)
    # Insert run that is 60 days old
    await run_repo.close_run(
        run_id="ancient-run",
        cron_fingerprint="fp-age",
        source="wrapper",
        state="ok",
        ended_at="2026-01-01T00:00:00+00:00",
        duration_seconds=None,
        exit_code=0,
        vl_window_end="2026-01-01T00:00:00+00:00",
    )

    async with httpx.AsyncClient() as http:
        result = await CronRunReconciler().run(_ctx(repo, http))

    assert result.ok is True
    assert await run_repo.get_run("ancient-run") is None


@pytest.mark.asyncio
async def test_prune_by_count_via_reconciler(
    repo: SqliteRepository,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """prune phase: per-cron count cap is enforced; only newest 3 kept."""
    monkeypatch.setenv("HOMELAB_MONITOR_VL_URL", "http://vl-test:9428")
    monkeypatch.setenv("HOMELAB_MONITOR_CRON_RUN_MAX_ROWS_PER_CRON", "3")
    monkeypatch.setenv("HOMELAB_MONITOR_CRON_RUN_RETENTION_DAYS", "3650")
    monkeypatch.setenv("HOMELAB_MONITOR_CRON_RUN_ENRICH_GRACE_SECONDS", "5")

    # The closed runs also satisfy the enrich queue; mock VL so the enrich
    # phase succeeds harmlessly instead of issuing unexpected requests.
    httpx_mock.add_response(method="GET", text="", is_reusable=True)

    fp = "fp-count-prune"
    run_repo = CronRunRepository(repo)
    for i in range(5):
        started = f"2026-05-19T00:00:{i:02d}+00:00"
        await run_repo.close_run(
            run_id=f"count-{i}",
            cron_fingerprint=fp,
            source="wrapper",
            state="ok",
            ended_at=started,
            duration_seconds=None,
            exit_code=0,
            vl_window_end=started,
        )

    async with httpx.AsyncClient() as http:
        result = await CronRunReconciler().run(_ctx(repo, http))

    assert result.ok is True
    assert await run_repo.get_run("count-0") is None
    assert await run_repo.get_run("count-1") is None
    assert await run_repo.get_run("count-2") is not None
    assert await run_repo.get_run("count-3") is not None
    assert await run_repo.get_run("count-4") is not None


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_ticks_are_idempotent(
    repo: SqliteRepository,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Running two reconciler ticks back-to-back is idempotent (no double-close/enrich)."""
    monkeypatch.setenv("HOMELAB_MONITOR_VL_URL", "http://vl-test:9428")
    monkeypatch.setenv("HOMELAB_MONITOR_CRON_RUN_ENRICH_GRACE_SECONDS", "5")

    run_id = "idem-run"
    ended_at = (datetime.now(UTC) - timedelta(seconds=60)).isoformat()
    await _insert_closed_run(repo, run_id=run_id, cron_fingerprint="fp-idem-e", ended_at=ended_at)

    # First tick enriches the run (mock returns 1 line)
    httpx_mock.add_response(method="GET", text=_ndjson_line("line") + "\n")

    async with httpx.AsyncClient() as http:
        r1 = await CronRunReconciler().run(_ctx(repo, http))

    # Second tick: run is already enriched, no VL request should be issued for it
    # (no more httpx_mock responses added — if it tries to call VL it will raise)
    async with httpx.AsyncClient() as http:
        r2 = await CronRunReconciler().run(_ctx(repo, http))

    assert r1.ok is True
    assert r2.ok is True

    run_repo = CronRunRepository(repo)
    row = await run_repo.get_run(run_id)
    assert row is not None
    assert row.enriched_at is not None  # enriched once


# ---------------------------------------------------------------------------
# Exception-handler tests (Group E1) — generic exception in each phase
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_window_finalize_exception_recorded_in_errors(
    repo: SqliteRepository,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Generic exception in _window_finalize sets result.ok=False and records error."""
    monkeypatch.setenv("HOMELAB_MONITOR_VL_URL", "http://vl-test:9428")

    async def _raising_stub(
        self: object,
        *args: object,
        **kwargs: object,
    ) -> None:  # pyright: ignore[reportPrivateUsage]
        raise RuntimeError("boom")

    monkeypatch.setattr(
        CronRunReconciler,
        "_window_finalize",
        _raising_stub,
    )  # pyright: ignore[reportPrivateUsage]

    async with httpx.AsyncClient() as http:
        result = await CronRunReconciler().run(_ctx(repo, http))

    assert result.ok is False
    assert any("window_finalize" in e and "boom" in e for e in result.errors)


@pytest.mark.asyncio
async def test_enrich_exception_recorded_in_errors(
    repo: SqliteRepository,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Generic (non-VL) exception in _enrich sets result.ok=False and records error."""
    monkeypatch.setenv("HOMELAB_MONITOR_VL_URL", "http://vl-test:9428")
    monkeypatch.setenv("HOMELAB_MONITOR_CRON_RUN_ENRICH_GRACE_SECONDS", "5")

    # Seed a closed run so the enrich phase has work and will call _enrich.
    ended_at = (datetime.now(UTC) - timedelta(seconds=60)).isoformat()
    await _insert_closed_run(
        repo, run_id="enrich-exc-run", cron_fingerprint="fp-enrich-exc", ended_at=ended_at
    )

    async def _raising_stub(
        self: object,
        *args: object,
        **kwargs: object,
    ) -> None:  # pyright: ignore[reportPrivateUsage]
        raise RuntimeError("boom")

    monkeypatch.setattr(
        CronRunReconciler,
        "_enrich",
        _raising_stub,
    )  # pyright: ignore[reportPrivateUsage]

    async with httpx.AsyncClient() as http:
        result = await CronRunReconciler().run(_ctx(repo, http))

    assert result.ok is False
    assert any("enrich" in e and "boom" in e for e in result.errors)


@pytest.mark.asyncio
async def test_prune_exception_recorded_in_errors(
    repo: SqliteRepository,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Generic exception in _prune sets result.ok=False and records error."""
    monkeypatch.setenv("HOMELAB_MONITOR_VL_URL", "http://vl-test:9428")

    async def _raising_stub(
        self: object,
        *args: object,
        **kwargs: object,
    ) -> None:  # pyright: ignore[reportPrivateUsage]
        raise RuntimeError("boom")

    monkeypatch.setattr(
        CronRunReconciler,
        "_prune",
        _raising_stub,
    )  # pyright: ignore[reportPrivateUsage]

    async with httpx.AsyncClient() as http:
        result = await CronRunReconciler().run(_ctx(repo, http))

    assert result.ok is False
    assert any("prune" in e and "boom" in e for e in result.errors)


# ---------------------------------------------------------------------------
# BUG-2 regression: per-run VL query failure must not abort the enrich phase
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enrich_one_bad_query_does_not_abort_other_runs(
    repo: SqliteRepository,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression BUG-2: a single run's VL 400 must not abort the enrich phase.

    Seed TWO closed wrapper runs. The one with the older ended_at is queried
    first (list_runs_needing_enrich orders by ended_at ASC). Register a 400
    for the first request and a valid NDJSON response for the second.
    After one tick: the first run stays un-enriched (retryable), the second
    run is enriched, and result.ok is True.
    """
    monkeypatch.setenv("HOMELAB_MONITOR_VL_URL", "http://vl-test:9428")
    monkeypatch.setenv("HOMELAB_MONITOR_CRON_RUN_ENRICH_GRACE_SECONDS", "5")

    # Older run (queried first by ended_at ASC) → will get the 400
    ended_first = (datetime.now(UTC) - timedelta(seconds=120)).isoformat()
    # Newer run (queried second) → will get the valid response
    ended_second = (datetime.now(UTC) - timedelta(seconds=60)).isoformat()

    await _insert_closed_run(
        repo, run_id="bad-query-run", cron_fingerprint="fp-bad", ended_at=ended_first
    )
    await _insert_closed_run(
        repo, run_id="good-query-run", cron_fingerprint="fp-good", ended_at=ended_second
    )

    # First VL request → 400 (simulates malformed query / VL parse error)
    httpx_mock.add_response(
        status_code=400,
        text="cannot parse query: improperly quoted string",
    )
    # Second VL request → valid single-line NDJSON
    httpx_mock.add_response(method="GET", text=_ndjson_line("success line") + "\n")

    async with httpx.AsyncClient() as http:
        result = await CronRunReconciler().run(_ctx(repo, http))

    assert result.ok is True  # tick-level success even though one run's query failed

    run_repo = CronRunRepository(repo)

    bad_row = await run_repo.get_run("bad-query-run")
    assert bad_row is not None
    assert bad_row.enriched_at is None  # skipped — will retry next tick

    good_row = await run_repo.get_run("good-query-run")
    assert good_row is not None
    assert good_row.enriched_at is not None  # enriched successfully
    assert good_row.line_count == 1


@pytest.mark.asyncio
async def test_enrich_bad_query_retried_next_tick(
    repo: SqliteRepository,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression BUG-2: a run skipped due to VL 400 is retried on the next tick.

    Tick 1: VL returns 400 → run stays un-enriched, result.ok is True.
    Tick 2: VL returns a valid NDJSON line → run is now enriched.
    """
    monkeypatch.setenv("HOMELAB_MONITOR_VL_URL", "http://vl-test:9428")
    monkeypatch.setenv("HOMELAB_MONITOR_CRON_RUN_ENRICH_GRACE_SECONDS", "5")

    run_id = "retry-run"
    ended_at = (datetime.now(UTC) - timedelta(seconds=60)).isoformat()
    await _insert_closed_run(repo, run_id=run_id, cron_fingerprint="fp-retry", ended_at=ended_at)

    # Tick 1: VL is broken for this run
    httpx_mock.add_response(
        status_code=400,
        text="cannot parse query: improperly quoted string",
    )

    async with httpx.AsyncClient() as http:
        r1 = await CronRunReconciler().run(_ctx(repo, http))

    assert r1.ok is True

    run_repo = CronRunRepository(repo)
    row_after_tick1 = await run_repo.get_run(run_id)
    assert row_after_tick1 is not None
    assert row_after_tick1.enriched_at is None  # still pending

    # Tick 2: VL is fixed — returns a valid line
    httpx_mock.add_response(method="GET", text=_ndjson_line("retry line") + "\n")

    async with httpx.AsyncClient() as http:
        r2 = await CronRunReconciler().run(_ctx(repo, http))

    assert r2.ok is True

    row_after_tick2 = await run_repo.get_run(run_id)
    assert row_after_tick2 is not None
    assert row_after_tick2.enriched_at is not None  # now enriched
    assert row_after_tick2.line_count == 1


# ---------------------------------------------------------------------------
# BUG-2 regression (broad except): non-VL exception must not abort enrich phase
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enrich_one_runs_non_vl_exception_does_not_abort_other_runs(
    repo: SqliteRepository,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Broad-except per-run isolation: a non-VictoriaLogsClientError inside the
    per-run try block must not abort the enrich phase for other runs.

    The except clause was widened from VictoriaLogsClientError to Exception
    (STAGE-013 BUG-2).  The existing BUG-2 tests only cover VL HTTP 400.
    This test covers the widened branch by raising a plain RuntimeError from
    CronRunRepository.set_enrichment on the first call, while the second call
    (for the other run) succeeds normally.

    After one tick:
    - result.ok is True (per-run failure is NOT a tick failure)
    - first run's enriched_at is NULL (skipped, retryable)
    - second run's enriched_at is NOT NULL (loop continued past the error)
    """
    monkeypatch.setenv("HOMELAB_MONITOR_VL_URL", "http://vl-test:9428")
    monkeypatch.setenv("HOMELAB_MONITOR_CRON_RUN_ENRICH_GRACE_SECONDS", "5")

    # Older run (queried first by ended_at ASC) → set_enrichment will raise RuntimeError
    ended_first = (datetime.now(UTC) - timedelta(seconds=120)).isoformat()
    # Newer run (queried second) → set_enrichment succeeds
    ended_second = (datetime.now(UTC) - timedelta(seconds=60)).isoformat()

    await _insert_closed_run(
        repo, run_id="non-vl-bad-run", cron_fingerprint="fp-nonvl-bad", ended_at=ended_first
    )
    await _insert_closed_run(
        repo, run_id="non-vl-good-run", cron_fingerprint="fp-nonvl-good", ended_at=ended_second
    )

    # Both VL requests succeed (the failure comes from set_enrichment, not VL)
    httpx_mock.add_response(method="GET", text="", is_reusable=True)

    call_count: dict[str, int] = {"n": 0}
    original_set_enrichment = CronRunRepository.set_enrichment  # pyright: ignore[reportPrivateUsage]

    async def _flaky_set_enrichment(
        self: CronRunRepository, *args: object, **kwargs: object
    ) -> None:
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("boom — non-VL failure to cover broad except branch")
        await original_set_enrichment(self, *args, **kwargs)  # pyright: ignore[reportArgumentType]

    monkeypatch.setattr(CronRunRepository, "set_enrichment", _flaky_set_enrichment)

    async with httpx.AsyncClient() as http:
        result = await CronRunReconciler().run(_ctx(repo, http))

    assert result.ok is True  # tick-level success despite per-run RuntimeError

    run_repo = CronRunRepository(repo)

    bad_row = await run_repo.get_run("non-vl-bad-run")
    assert bad_row is not None
    assert bad_row.enriched_at is None  # skipped due to RuntimeError — will retry next tick

    good_row = await run_repo.get_run("non-vl-good-run")
    assert good_row is not None
    assert good_row.enriched_at is not None  # broad except let loop continue; second run enriched


# ---------------------------------------------------------------------------
# Defensive guard: ended_at=None in enrich loop (lines 236-240)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enrich_skips_run_with_null_ended_at(
    repo: SqliteRepository,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defensive guard: a CronRunRecord with ended_at=None returned by the repo
    is logged and skipped without crashing the tick and without issuing any VL
    request.  The guard at lines 236-240 of cron_run_reconciler.py is normally
    unreachable (the SQL enforces ended_at IS NOT NULL); we exercise it by
    monkeypatching list_runs_needing_enrich to inject a hand-crafted bad record.
    """
    monkeypatch.setenv("HOMELAB_MONITOR_VL_URL", "http://vl-test:9428")
    monkeypatch.setenv("HOMELAB_MONITOR_CRON_RUN_ENRICH_GRACE_SECONDS", "5")

    from homelab_monitor.kernel.cron.run_repository import CronRunRecord  # noqa: PLC0415

    bad_record = CronRunRecord(
        run_id="null-ended-run",
        cron_fingerprint="fp-null-ended",
        source="logscrape",
        state="unknown",
        started_at="2026-05-19T00:00:00+00:00",
        ended_at=None,  # invariant violation — triggers the defensive guard
        duration_seconds=None,
        exit_code=None,
        vl_window_start="2026-05-19T00:00:00+00:00",
        vl_window_end=None,
        overlapping=False,
        enriched_at=None,
        line_count=None,
        byte_count=None,
        content_digest=None,
        anomaly_flags="",
    )

    async def _stub_list_runs(
        self: CronRunRepository, *args: object, **kwargs: object
    ) -> list[CronRunRecord]:
        return [bad_record]

    monkeypatch.setattr(CronRunRepository, "list_runs_needing_enrich", _stub_list_runs)

    async with httpx.AsyncClient() as http:
        result = await CronRunReconciler().run(_ctx(repo, http))

    # The guard logs + continues — not a tick failure
    assert result.ok is True
    # No VL request should have been made (httpx_mock has no responses registered;
    # any attempt to call VL would raise an unregistered-request error from pytest_httpx)


# ---------------------------------------------------------------------------
# STAGE-002-014: anomaly evaluation wiring in _enrich
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enrich_sets_anomaly_flags_when_rule_trips(
    repo: SqliteRepository,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After enrich, anomaly_flags is written when a rule fires.

    Strategy: seed 10 ok history runs with line_count=100, then enrich a run
    with line_count=0 (VL returns empty body). The unexpected_empty rule trips.
    """
    monkeypatch.setenv("HOMELAB_MONITOR_VL_URL", "http://vl-test:9428")
    monkeypatch.setenv("HOMELAB_MONITOR_CRON_RUN_ENRICH_GRACE_SECONDS", "5")
    # Set min_history=10 (default) — we'll seed exactly 10 history rows.
    monkeypatch.setenv("HOMELAB_MONITOR_CRON_ANOMALY_MIN_HISTORY", "10")

    fp = "fp-anomaly-wire"
    await _insert_cron(repo, fingerprint=fp, command="/usr/bin/backup.sh")

    # Seed 10 already-enriched history runs with line_count=100 each
    for i in range(10):
        ts = f"2026-05-01T00:00:0{i}+00:00"
        hist_id = f"hist-run-{i}"
        run_repo_hist = CronRunRepository(repo)
        await run_repo_hist.close_run(
            run_id=hist_id,
            cron_fingerprint=fp,
            source="wrapper",
            state="ok",
            ended_at=ts,
            duration_seconds=5.0,
            exit_code=0,
            vl_window_end=ts,
        )
        # Mark as enriched with line_count=100
        async with repo.engine.begin() as conn:
            from sqlalchemy import text as _text  # noqa: PLC0415

            await conn.execute(
                _text(
                    "UPDATE cron_runs SET line_count=100, byte_count=500, "
                    "anomaly_flags='', enriched_at=:ea WHERE run_id=:rid"
                ),
                {"ea": utc_now_iso(), "rid": hist_id},
            )

    # Seed the run-under-test: closed, not enriched, old enough to pass grace
    run_id = "anomaly-test-run"
    ended_at = (datetime.now(UTC) - timedelta(seconds=60)).isoformat()
    run_repo = CronRunRepository(repo)
    await run_repo.close_run(
        run_id=run_id,
        cron_fingerprint=fp,
        source="wrapper",
        state="ok",
        ended_at=ended_at,
        duration_seconds=5.0,
        exit_code=0,
        vl_window_end=ended_at,
    )

    # VL returns empty body → line_count=0 → unexpected_empty fires
    httpx_mock.add_response(method="GET", text="", is_reusable=True)

    async with httpx.AsyncClient() as http:
        result = await CronRunReconciler().run(_ctx(repo, http))

    assert result.ok is True

    row = await run_repo.get_run(run_id)
    assert row is not None
    assert row.enriched_at is not None
    assert "unexpected_empty" in row.anomaly_flags


@pytest.mark.asyncio
async def test_enrich_anomaly_flags_empty_with_insufficient_history(
    repo: SqliteRepository,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With < min_history completed runs, anomaly_flags stays empty (min-history gate)."""
    monkeypatch.setenv("HOMELAB_MONITOR_VL_URL", "http://vl-test:9428")
    monkeypatch.setenv("HOMELAB_MONITOR_CRON_RUN_ENRICH_GRACE_SECONDS", "5")
    monkeypatch.setenv("HOMELAB_MONITOR_CRON_ANOMALY_MIN_HISTORY", "10")

    fp = "fp-no-history"
    # Only 2 history runs (< min_history=10)
    run_repo = CronRunRepository(repo)
    for i in range(2):
        ts = f"2026-05-01T00:00:0{i}+00:00"
        await run_repo.close_run(
            run_id=f"nh-hist-{i}",
            cron_fingerprint=fp,
            source="wrapper",
            state="ok",
            ended_at=ts,
            duration_seconds=5.0,
            exit_code=0,
            vl_window_end=ts,
        )
        async with repo.engine.begin() as conn:
            from sqlalchemy import text as _text  # noqa: PLC0415

            await conn.execute(
                _text("UPDATE cron_runs SET line_count=100, enriched_at=:ea WHERE run_id=:rid"),
                {"ea": utc_now_iso(), "rid": f"nh-hist-{i}"},
            )

    run_id = "nh-test-run"
    ended_at = (datetime.now(UTC) - timedelta(seconds=60)).isoformat()
    await run_repo.close_run(
        run_id=run_id,
        cron_fingerprint=fp,
        source="wrapper",
        state="ok",
        ended_at=ended_at,
        duration_seconds=5.0,
        exit_code=0,
        vl_window_end=ended_at,
    )

    # VL returns empty → line_count=0, but gate fires first
    httpx_mock.add_response(method="GET", text="", is_reusable=True)

    async with httpx.AsyncClient() as http:
        result = await CronRunReconciler().run(_ctx(repo, http))

    assert result.ok is True
    row = await run_repo.get_run(run_id)
    assert row is not None
    assert row.enriched_at is not None
    assert row.anomaly_flags == ""  # min_history gate suppressed all flags


@pytest.mark.asyncio
async def test_enrich_evaluator_exception_is_isolated(
    repo: SqliteRepository,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If evaluate_run raises, the per-run try/except catches it; other runs process."""
    monkeypatch.setenv("HOMELAB_MONITOR_VL_URL", "http://vl-test:9428")
    monkeypatch.setenv("HOMELAB_MONITOR_CRON_RUN_ENRICH_GRACE_SECONDS", "5")

    fp_bad = "fp-eval-exc"
    fp_good = "fp-eval-good"

    ended_at = (datetime.now(UTC) - timedelta(seconds=60)).isoformat()

    run_repo = CronRunRepository(repo)

    # Run that will trigger the mocked evaluate_run failure
    await run_repo.close_run(
        run_id="eval-bad-run",
        cron_fingerprint=fp_bad,
        source="wrapper",
        state="ok",
        ended_at=ended_at,
        duration_seconds=5.0,
        exit_code=0,
        vl_window_end=ended_at,
    )
    # Second run (different fingerprint) that should still succeed
    await run_repo.close_run(
        run_id="eval-good-run",
        cron_fingerprint=fp_good,
        source="wrapper",
        state="ok",
        ended_at=ended_at,
        duration_seconds=5.0,
        exit_code=0,
        vl_window_end=ended_at,
    )

    # Monkeypatch evaluate_run to raise on the bad fingerprint's run
    from homelab_monitor.kernel.config import CronAnomalyConfig as _CAC  # noqa: PLC0415
    from homelab_monitor.kernel.cron import run_anomaly as _ra  # noqa: PLC0415
    from homelab_monitor.kernel.cron.run_repository import CronRunRecord as _CRR  # noqa: PLC0415

    original_evaluate = _ra.evaluate_run

    def _patched_evaluate(run: _CRR, history: object, config: _CAC) -> str:
        if run.run_id == "eval-bad-run":
            raise RuntimeError("simulated evaluator crash")
        return original_evaluate(run, history, config)  # type: ignore[arg-type]

    monkeypatch.setattr(_ra, "evaluate_run", _patched_evaluate)

    # Also patch it in the reconciler module namespace
    from homelab_monitor.kernel.metrics import cron_run_reconciler as _rcr  # noqa: PLC0415

    monkeypatch.setattr(_rcr, "evaluate_run", _patched_evaluate)

    httpx_mock.add_response(method="GET", text="line one\n", is_reusable=True)

    async with httpx.AsyncClient() as http:
        result = await CronRunReconciler().run(_ctx(repo, http))

    # Tick must still be ok
    assert result.ok is True

    # The good run must have been enriched
    good_row = await run_repo.get_run("eval-good-run")
    assert good_row is not None
    assert good_row.enriched_at is not None


@pytest.mark.asyncio
async def test_enrich_respects_max_per_tick_bound(
    repo: SqliteRepository,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Enrich phase limits work per tick to enrich_max_per_tick."""
    monkeypatch.setenv("HOMELAB_MONITOR_VL_URL", "http://vl-test:9428")
    monkeypatch.setenv("HOMELAB_MONITOR_CRON_RUN_ENRICH_MAX_PER_TICK", "2")
    monkeypatch.setenv("HOMELAB_MONITOR_CRON_RUN_ENRICH_GRACE_SECONDS", "0")

    fp = "fp-max-per-tick"
    base_time = datetime.now(UTC) - timedelta(hours=1)

    # Seed 5 closed, un-enriched runs (all well past grace cutoff).
    for i in range(5):
        ended_at = (base_time - timedelta(minutes=i)).isoformat()
        await _insert_closed_run(
            repo,
            run_id=f"run-{i}",
            cron_fingerprint=fp,
            source="wrapper",
            state="ok",
            ended_at=ended_at,
        )

    # Mock VL to return one line for every query.
    httpx_mock.add_response(method="GET", text="log line\n", is_reusable=True)

    async with httpx.AsyncClient() as http:
        result = await CronRunReconciler().run(_ctx(repo, http))

    assert result.ok is True

    run_repo = CronRunRepository(repo)

    # Only 2 runs should have been enriched (max_per_tick=2).
    enriched_count = 0
    for i in range(5):
        row = await run_repo.get_run(f"run-{i}")
        assert row is not None
        if row.enriched_at is not None:
            enriched_count += 1

    assert enriched_count == 2  # noqa: PLR2004


@pytest.mark.asyncio
async def test_enrich_query_window_includes_slack(
    repo: SqliteRepository,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Enrich: VL query end time is extended by enrich_window_slack_seconds.

    Seed a closed A-mode run with ended_at = T. Set enrich_window_slack_seconds
    to 60. After running one reconciler tick, inspect the actual VL request URL
    and assert the end query param is approximately T + 60s (not T).
    """
    monkeypatch.setenv("HOMELAB_MONITOR_VL_URL", "http://vl-test:9428")
    monkeypatch.setenv("HOMELAB_MONITOR_CRON_RUN_ENRICH_GRACE_SECONDS", "5")
    monkeypatch.setenv("HOMELAB_MONITOR_CRON_RUN_ENRICH_WINDOW_SLACK_SECONDS", "60")

    run_id = "slack-test-run"
    ended_at = (datetime.now(UTC) - timedelta(seconds=60)).isoformat()

    await _insert_closed_run(repo, run_id=run_id, cron_fingerprint="fp-slack", ended_at=ended_at)

    # Mock VL to return one line
    httpx_mock.add_response(method="GET", text=_ndjson_line("test line") + "\n")

    async with httpx.AsyncClient() as http:
        result = await CronRunReconciler().run(_ctx(repo, http))

    assert result.ok is True

    # Inspect the VL request to verify the end time includes slack
    requests = httpx_mock.get_requests()
    assert len(requests) >= 1

    end_param = requests[0].url.params.get("end", "")
    assert end_param, "end query parameter must be present"

    # Parse the end time and the original ended_at to compare
    expected_end_dt = _parse_iso(ended_at) + timedelta(seconds=60)
    actual_end_dt = _parse_iso(end_param)

    # Allow small tolerance for timing (up to 2 seconds)
    delta = abs((actual_end_dt - expected_end_dt).total_seconds())
    assert delta < 2, f"end param should be approximately {expected_end_dt}, got {actual_end_dt}"  # noqa: PLR2004


@pytest.mark.asyncio
async def test_enrich_query_no_slack_when_disabled(
    repo: SqliteRepository,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Enrich: VL query end time is NOT extended when slack is 0.

    Set enrich_window_slack_seconds to 0 and verify the VL query end time
    remains unchanged (equals ended_at, not ended_at + slack).
    """
    monkeypatch.setenv("HOMELAB_MONITOR_VL_URL", "http://vl-test:9428")
    monkeypatch.setenv("HOMELAB_MONITOR_CRON_RUN_ENRICH_GRACE_SECONDS", "5")
    monkeypatch.setenv("HOMELAB_MONITOR_CRON_RUN_ENRICH_WINDOW_SLACK_SECONDS", "0")

    run_id = "no-slack-test-run"
    ended_at = (datetime.now(UTC) - timedelta(seconds=60)).isoformat()

    await _insert_closed_run(repo, run_id=run_id, cron_fingerprint="fp-noslack", ended_at=ended_at)

    # Mock VL to return one line
    httpx_mock.add_response(method="GET", text=_ndjson_line("test line") + "\n")

    async with httpx.AsyncClient() as http:
        result = await CronRunReconciler().run(_ctx(repo, http))

    assert result.ok is True

    # Inspect the VL request to verify the end time does NOT include slack
    requests = httpx_mock.get_requests()
    assert len(requests) >= 1

    end_param = requests[0].url.params.get("end", "")
    assert end_param, "end query parameter must be present"

    # Parse the end time — should equal ended_at (no slack added)
    expected_end_dt = _parse_iso(ended_at)
    actual_end_dt = _parse_iso(end_param)

    # Allow small tolerance for timing (up to 2 seconds)
    delta = abs((actual_end_dt - expected_end_dt).total_seconds())
    assert delta < 2, f"end param should be approximately {expected_end_dt}, got {actual_end_dt}"  # noqa: PLR2004


@pytest.mark.asyncio
async def test_enrich_query_no_slack_for_logscrape(
    repo: SqliteRepository,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Enrich B-mode: VL query end time is NOT extended by slack even when slack > 0.

    B-mode (logscrape) runs should NOT have slack applied to the query window,
    regardless of the enrich_window_slack_seconds setting. Slack is a wrapper-only
    feature to account for clock skew in A-mode.

    Seed a closed B-mode run with ended_at = T. Set enrich_window_slack_seconds
    to 60. After running one reconciler tick, inspect the actual VL request URL
    and assert the end query param is approximately T (not T + 60s).
    """
    monkeypatch.setenv("HOMELAB_MONITOR_VL_URL", "http://vl-test:9428")
    monkeypatch.setenv("HOMELAB_MONITOR_CRON_RUN_ENRICH_GRACE_SECONDS", "5")
    monkeypatch.setenv("HOMELAB_MONITOR_CRON_RUN_ENRICH_WINDOW_SLACK_SECONDS", "60")

    command = "/usr/bin/backup.sh"
    fp = "fp-bmode-noslack"
    await _insert_cron(repo, fingerprint=fp, command=command)

    run_id = "bmode-slack-test-run"
    ended_at = (datetime.now(UTC) - timedelta(seconds=60)).isoformat()

    await _insert_closed_run(
        repo, run_id=run_id, cron_fingerprint=fp, source="logscrape", ended_at=ended_at
    )

    # Mock VL to return one line
    httpx_mock.add_response(method="GET", text=_ndjson_line("test line") + "\n")

    async with httpx.AsyncClient() as http:
        result = await CronRunReconciler().run(_ctx(repo, http))

    assert result.ok is True

    # Inspect the VL request to verify the end time does NOT include slack
    requests = httpx_mock.get_requests()
    assert len(requests) >= 1

    end_param = requests[0].url.params.get("end", "")
    assert end_param, "end query parameter must be present"

    # Parse the end time — should equal ended_at (no slack added for B-mode)
    expected_end_dt = _parse_iso(ended_at)
    actual_end_dt = _parse_iso(end_param)

    # Allow small tolerance for timing (up to 2 seconds)
    delta = abs((actual_end_dt - expected_end_dt).total_seconds())
    assert delta < 2, (  # noqa: PLR2004
        f"end param should be approximately {expected_end_dt} (no slack for B-mode), "
        f"got {actual_end_dt}"
    )


# ---------------------------------------------------------------------------
# STAGE-004-034: failure-enrich tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_failure_enrich_fail_run_inserts_row_and_emits_counter(
    repo: SqliteRepository,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A state='fail' run gets a failure_enrichments row AND emits homelab_cron_run_failure_total.

    The reconciler makes TWO VL requests per failed run (main enrich + failure enrich).
    Both succeed (reusable mock). After one tick the failure enrichment row exists and
    the failure counter appears in the metrics writer.
    """
    monkeypatch.setenv("HOMELAB_MONITOR_VL_URL", "http://vl-test:9428")
    monkeypatch.setenv("HOMELAB_MONITOR_CRON_RUN_ENRICH_GRACE_SECONDS", "5")

    from homelab_monitor.kernel.cron.run_repository import CronRunRepository as _RR  # noqa: PLC0415
    from homelab_monitor.kernel.logs.cron_run_failure_enrichments_repo import (  # noqa: PLC0415
        CronRunFailureEnrichmentsRepository,
    )

    fp: str = "fp-fail-enrich"
    run_id: str = "run-fail-001"
    ended_at: str = (datetime.now(UTC) - timedelta(seconds=60)).isoformat()

    run_repo = _RR(repo)
    await run_repo.close_run(
        run_id=run_id,
        cron_fingerprint=fp,
        source="wrapper",
        state="fail",
        ended_at=ended_at,
        duration_seconds=10.0,
        exit_code=1,
        vl_window_end=ended_at,
    )

    # Both VL queries (main enrich + failure enrich) succeed
    httpx_mock.add_response(method="GET", text=_ndjson_line("fail output") + "\n", is_reusable=True)

    vm = MemoryRetainingMetricsWriter()

    import dataclasses  # noqa: PLC0415

    async with httpx.AsyncClient() as http:
        ctx_with_vm = dataclasses.replace(_ctx(repo, http), vm=vm)
        result = await CronRunReconciler().run(ctx_with_vm)

    assert result.ok is True

    # Failure enrichment row must exist
    failure_repo = CronRunFailureEnrichmentsRepository(repo)
    row = await failure_repo.get_by_run(fp, run_id)
    assert row is not None
    assert row.run_id == run_id
    assert row.cron_fingerprint == fp
    assert row.degraded is False

    # Counter must be emitted
    counter_entries = [e for e in vm.recorded if e.name == "homelab_cron_run_failure_total"]
    assert len(counter_entries) >= 1
    assert counter_entries[0].labels["cron_fingerprint"] == fp
    assert counter_entries[0].labels["run_id"] == run_id


@pytest.mark.asyncio
async def test_failure_enrich_ok_run_produces_no_row_no_counter(
    repo: SqliteRepository,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A state='ok' run does NOT get a failure enrichment row and emits NO counter."""
    monkeypatch.setenv("HOMELAB_MONITOR_VL_URL", "http://vl-test:9428")
    monkeypatch.setenv("HOMELAB_MONITOR_CRON_RUN_ENRICH_GRACE_SECONDS", "5")

    from homelab_monitor.kernel.logs.cron_run_failure_enrichments_repo import (  # noqa: PLC0415
        CronRunFailureEnrichmentsRepository,
    )

    fp: str = "fp-ok-no-fail-enrich"
    run_id: str = "run-ok-002"
    ended_at: str = (datetime.now(UTC) - timedelta(seconds=60)).isoformat()

    await _insert_closed_run(
        repo, run_id=run_id, cron_fingerprint=fp, state="ok", ended_at=ended_at
    )

    # Only ONE VL call (main enrich; no failure-enrich call for state='ok')
    httpx_mock.add_response(method="GET", text=_ndjson_line("ok output") + "\n")

    vm = MemoryRetainingMetricsWriter()

    import dataclasses  # noqa: PLC0415

    async with httpx.AsyncClient() as http:
        ctx = dataclasses.replace(_ctx(repo, http), vm=vm)
        result = await CronRunReconciler().run(ctx)

    assert result.ok is True

    failure_repo = CronRunFailureEnrichmentsRepository(repo)
    row = await failure_repo.get_by_run(fp, run_id)
    assert row is None

    counter_entries = [e for e in vm.recorded if e.name == "homelab_cron_run_failure_total"]
    assert counter_entries == []


@pytest.mark.asyncio
async def test_failure_enrich_second_tick_deduplicates_row_and_counter(
    repo: SqliteRepository,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Second tick on the same failed run: still ONE row, counter NOT re-emitted.

    INSERT OR IGNORE ensures idempotency — the failure enrichment row is created
    once; subsequent ticks see inserted=False and skip the counter emit.
    The run is already enriched after tick 1 so tick 2 has no pending runs to
    process at all (no extra VL call registered).
    """
    monkeypatch.setenv("HOMELAB_MONITOR_VL_URL", "http://vl-test:9428")
    monkeypatch.setenv("HOMELAB_MONITOR_CRON_RUN_ENRICH_GRACE_SECONDS", "5")

    from homelab_monitor.kernel.cron.run_repository import CronRunRepository as _RR  # noqa: PLC0415
    from homelab_monitor.kernel.logs.cron_run_failure_enrichments_repo import (  # noqa: PLC0415
        CronRunFailureEnrichmentsRepository,
    )

    fp: str = "fp-dedup-fail"
    run_id: str = "run-fail-dedup"
    ended_at: str = (datetime.now(UTC) - timedelta(seconds=60)).isoformat()

    run_repo = _RR(repo)
    await run_repo.close_run(
        run_id=run_id,
        cron_fingerprint=fp,
        source="wrapper",
        state="fail",
        ended_at=ended_at,
        duration_seconds=10.0,
        exit_code=2,
        vl_window_end=ended_at,
    )

    # Tick 1: both main enrich + failure enrich VL calls succeed
    httpx_mock.add_response(method="GET", text=_ndjson_line("fail log") + "\n", is_reusable=True)

    vm = MemoryRetainingMetricsWriter()

    import dataclasses  # noqa: PLC0415

    async with httpx.AsyncClient() as http:
        ctx1 = dataclasses.replace(_ctx(repo, http), vm=vm)
        r1 = await CronRunReconciler().run(ctx1)

    assert r1.ok is True
    counters_after_tick1 = [e for e in vm.recorded if e.name == "homelab_cron_run_failure_total"]
    assert len(counters_after_tick1) == 1

    # Tick 2: run already enriched — no pending runs, no VL call, no counter
    # (httpx_mock still has is_reusable response registered but should not be called)
    vm2 = MemoryRetainingMetricsWriter()
    async with httpx.AsyncClient() as http:
        ctx2 = dataclasses.replace(_ctx(repo, http), vm=vm2)
        r2 = await CronRunReconciler().run(ctx2)

    assert r2.ok is True
    counters_after_tick2 = [e for e in vm2.recorded if e.name == "homelab_cron_run_failure_total"]
    assert counters_after_tick2 == []

    # Only one row exists
    failure_repo = CronRunFailureEnrichmentsRepository(repo)
    row = await failure_repo.get_by_run(fp, run_id)
    assert row is not None


@pytest.mark.asyncio
async def test_failure_enrich_degraded_vl_inserts_degraded_row_and_counter(
    repo: SqliteRepository,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """VL failure during failure-fetch: degraded=True row inserted + counter STILL emitted.

    The main enrich VL call succeeds. The failure-enrich VL call (failure_client)
    raises VictoriaLogsClientError. The degraded=1 row must exist and the
    homelab_cron_run_failure_total counter must still be emitted.

    Strategy: register TWO mock responses — first succeeds (main enrich),
    second raises ConnectError (failure enrich).
    """
    monkeypatch.setenv("HOMELAB_MONITOR_VL_URL", "http://vl-test:9428")
    monkeypatch.setenv("HOMELAB_MONITOR_CRON_RUN_ENRICH_GRACE_SECONDS", "5")

    from homelab_monitor.kernel.cron.run_repository import CronRunRepository as _RR  # noqa: PLC0415
    from homelab_monitor.kernel.logs.cron_run_failure_enrichments_repo import (  # noqa: PLC0415
        CronRunFailureEnrichmentsRepository,
    )

    fp: str = "fp-degraded-fail"
    run_id: str = "run-fail-degraded"
    ended_at: str = (datetime.now(UTC) - timedelta(seconds=60)).isoformat()

    run_repo = _RR(repo)
    await run_repo.close_run(
        run_id=run_id,
        cron_fingerprint=fp,
        source="wrapper",
        state="fail",
        ended_at=ended_at,
        duration_seconds=10.0,
        exit_code=1,
        vl_window_end=ended_at,
    )

    # First request → main enrich succeeds
    httpx_mock.add_response(method="GET", text=_ndjson_line("main enrich") + "\n")
    # Second request (failure_client.query) → ConnectError → VictoriaLogsClientError
    httpx_mock.add_exception(httpx.ConnectError("VL down for failure fetch"))

    vm = MemoryRetainingMetricsWriter()

    import dataclasses  # noqa: PLC0415

    async with httpx.AsyncClient() as http:
        ctx = dataclasses.replace(_ctx(repo, http), vm=vm)
        result = await CronRunReconciler().run(ctx)

    assert result.ok is True

    failure_repo = CronRunFailureEnrichmentsRepository(repo)
    row = await failure_repo.get_by_run(fp, run_id)
    assert row is not None
    assert row.degraded is True
    assert row.line_count == 0

    # Counter still emitted despite VL failure
    counter_entries = [e for e in vm.recorded if e.name == "homelab_cron_run_failure_total"]
    assert len(counter_entries) >= 1


@pytest.mark.asyncio
async def test_failure_enrich_unexpected_exception_isolates_from_enrich_loop(
    repo: SqliteRepository,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unexpected exception in failure-enrich does NOT break the main enrich loop.

    Seed two failed runs. The first run's _failure_enrich_run raises an unexpected
    RuntimeError (via monkeypatching). The second run's failure-enrich should still
    succeed — demonstrating per-run try/except isolation (the outer except in _enrich).

    After one tick:
    - result.ok is True
    - first run IS enriched (main enrich succeeded; only failure-enrich raised)
    - second run's failure enrichment row exists
    """
    monkeypatch.setenv("HOMELAB_MONITOR_VL_URL", "http://vl-test:9428")
    monkeypatch.setenv("HOMELAB_MONITOR_CRON_RUN_ENRICH_GRACE_SECONDS", "5")

    from homelab_monitor.kernel.cron.run_repository import CronRunRepository as _RR  # noqa: PLC0415
    from homelab_monitor.kernel.logs.cron_run_failure_enrichments_repo import (  # noqa: PLC0415
        CronRunFailureEnrichmentsRepository,
    )

    # Older run (queried first) — _failure_enrich_run will raise for this one
    ended_first: str = (datetime.now(UTC) - timedelta(seconds=120)).isoformat()
    # Newer run (queried second) — failure-enrich succeeds
    ended_second: str = (datetime.now(UTC) - timedelta(seconds=60)).isoformat()

    run_repo = _RR(repo)
    fp_bad: str = "fp-fail-exc-bad"
    fp_good: str = "fp-fail-exc-good"

    await run_repo.close_run(
        run_id="fail-exc-bad-run",
        cron_fingerprint=fp_bad,
        source="wrapper",
        state="fail",
        ended_at=ended_first,
        duration_seconds=5.0,
        exit_code=1,
        vl_window_end=ended_first,
    )
    await run_repo.close_run(
        run_id="fail-exc-good-run",
        cron_fingerprint=fp_good,
        source="wrapper",
        state="fail",
        ended_at=ended_second,
        duration_seconds=5.0,
        exit_code=1,
        vl_window_end=ended_second,
    )

    # All VL requests succeed (main enrich + failure enrich for good run)
    httpx_mock.add_response(method="GET", text="", is_reusable=True)

    # Monkeypatch _failure_enrich_run to raise for the first run only
    original_failure_enrich = CronRunReconciler._failure_enrich_run  # pyright: ignore[reportPrivateUsage]
    call_count: dict[str, int] = {"n": 0}

    async def _flaky_failure_enrich(
        self: CronRunReconciler, *args: object, **kwargs: object
    ) -> int:
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("simulated unexpected failure-enrich error")
        return await original_failure_enrich(self, *args, **kwargs)  # pyright: ignore[reportArgumentType]

    monkeypatch.setattr(CronRunReconciler, "_failure_enrich_run", _flaky_failure_enrich)  # pyright: ignore[reportPrivateUsage]

    vm = MemoryRetainingMetricsWriter()
    import dataclasses  # noqa: PLC0415

    async with httpx.AsyncClient() as http:
        ctx = dataclasses.replace(_ctx(repo, http), vm=vm)
        result = await CronRunReconciler().run(ctx)

    assert result.ok is True

    # First run: main enrich completed (enriched_at set); failure-enrich row absent
    bad_row = await run_repo.get_run("fail-exc-bad-run")
    assert bad_row is not None
    assert bad_row.enriched_at is not None  # main enrich succeeded

    # Second run: failure enrichment row exists
    failure_repo = CronRunFailureEnrichmentsRepository(repo)
    good_fe_row = await failure_repo.get_by_run(fp_good, "fail-exc-good-run")
    assert good_fe_row is not None


@pytest.mark.asyncio
async def test_failure_enrich_dedup_skip_when_row_already_exists(
    repo: SqliteRepository,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When failure-enrich row already exists, INSERT OR IGNORE returns False → return 0.

    Strategy: seed a failed run that needs enrich (enriched_at=NULL) AND a
    pre-existing failure enrichment row for that (fp, run_id). The reconciler
    processes the run normally (main enrich succeeds), then calls _failure_enrich_run
    which finds the pre-existing row (inserted=False) and returns 0 — no counter emitted.
    """
    monkeypatch.setenv("HOMELAB_MONITOR_VL_URL", "http://vl-test:9428")
    monkeypatch.setenv("HOMELAB_MONITOR_CRON_RUN_ENRICH_GRACE_SECONDS", "5")

    from homelab_monitor.kernel.cron.run_repository import CronRunRepository as _RR  # noqa: PLC0415
    from homelab_monitor.kernel.logs.cron_run_failure_enrichments_repo import (  # noqa: PLC0415
        CronRunFailureEnrichmentsRepository,
    )

    fp: str = "fp-dedup-skip"
    run_id: str = "run-dedup-skip-001"
    ended_at: str = (datetime.now(UTC) - timedelta(seconds=60)).isoformat()

    run_repo = _RR(repo)
    await run_repo.close_run(
        run_id=run_id,
        cron_fingerprint=fp,
        source="wrapper",
        state="fail",
        ended_at=ended_at,
        duration_seconds=5.0,
        exit_code=1,
        vl_window_end=ended_at,
    )

    # Pre-seed failure enrichment row so INSERT OR IGNORE will be a no-op
    failure_repo = CronRunFailureEnrichmentsRepository(repo)
    await failure_repo.insert(
        failure_id="pre-existing-failure-id",
        cron_fingerprint=fp,
        run_id=run_id,
        exit_code=1,
        started_at=ended_at,
        ended_at=ended_at,
        lines=[],
        truncated=False,
        degraded=False,
        window_start=ended_at,
        window_end=ended_at,
    )

    # Main enrich VL call succeeds; failure-enrich VL call also succeeds (is_reusable)
    httpx_mock.add_response(method="GET", text="", is_reusable=True)

    vm = MemoryRetainingMetricsWriter()
    import dataclasses  # noqa: PLC0415

    async with httpx.AsyncClient() as http:
        ctx = dataclasses.replace(_ctx(repo, http), vm=vm)
        result = await CronRunReconciler().run(ctx)

    assert result.ok is True

    # Counter must NOT be emitted (dedup — inserted=False → return 0)
    counter_entries = [e for e in vm.recorded if e.name == "homelab_cron_run_failure_total"]
    assert counter_entries == []


@pytest.mark.asyncio
async def test_prune_removes_old_failure_enrichment_rows_via_reconciler_tick(
    repo: SqliteRepository,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_prune also prunes cron_run_failure_enrichments, not only cron_runs.

    This is the wiring-guard test: it ensures the reconciler's _prune phase
    actually calls CronRunFailureEnrichmentsRepository.prune().

    Seed two failure enrichment rows:
    - old_row: created_at set to 40 days ago (older than the 30-day retention)
    - fresh_row: created_at set to 1 day ago (within retention)

    Run a full reconciler tick (no pending cron_runs, VL never called).
    Assert the old row is deleted and the fresh row survives.
    """
    monkeypatch.setenv("HOMELAB_MONITOR_VL_URL", "http://vl-test:9428")
    monkeypatch.setenv("HOMELAB_MONITOR_CRON_RUN_RETENTION_DAYS", "30")
    # Use the default cron_failure_enrich_retention_days (30) by not setting it,
    # so we rely on the config default.  Set a high max_rows_per_cron so the
    # count cap never triggers.
    monkeypatch.setenv("HOMELAB_MONITOR_CRON_FAILURE_ENRICH_MAX_ROWS_PER_CRON", "1000")

    from homelab_monitor.kernel.logs.cron_run_failure_enrichments_repo import (  # noqa: PLC0415
        CronRunFailureEnrichmentsRepository,
    )

    failure_repo: CronRunFailureEnrichmentsRepository = CronRunFailureEnrichmentsRepository(repo)

    fp_old: str = "fp-prune-old-failure"
    run_id_old: str = "run-old-failure-001"
    fp_fresh: str = "fp-prune-fresh-failure"
    run_id_fresh: str = "run-fresh-failure-001"

    # Insert both rows via the normal insert path (creates NOW as created_at),
    # then back-date the old row via direct SQL.
    ts_ref: str = "2026-01-01T00:00:00+00:00"  # just needs a valid ISO string
    await failure_repo.insert(
        failure_id="old-failure-id",
        cron_fingerprint=fp_old,
        run_id=run_id_old,
        exit_code=1,
        started_at=ts_ref,
        ended_at=ts_ref,
        lines=[],
        truncated=False,
        degraded=False,
        window_start=ts_ref,
        window_end=ts_ref,
    )
    await failure_repo.insert(
        failure_id="fresh-failure-id",
        cron_fingerprint=fp_fresh,
        run_id=run_id_fresh,
        exit_code=1,
        started_at=ts_ref,
        ended_at=ts_ref,
        lines=[],
        truncated=False,
        degraded=False,
        window_start=ts_ref,
        window_end=ts_ref,
    )

    # Back-date the old row to 40 days ago so it is past the 30-day retention cutoff.
    old_created_at: str = (datetime.now(UTC) - timedelta(days=40)).isoformat()
    fresh_created_at: str = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    async with repo.engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE cron_run_failure_enrichments SET created_at = :ca WHERE failure_id = :fid"
            ),
            {"ca": old_created_at, "fid": "old-failure-id"},
        )
        await conn.execute(
            text(
                "UPDATE cron_run_failure_enrichments SET created_at = :ca WHERE failure_id = :fid"
            ),
            {"ca": fresh_created_at, "fid": "fresh-failure-id"},
        )

    # No cron_runs seeded → no VL calls; httpx_mock has no registered responses
    # and would raise if called — that acts as an implicit assertion.
    async with httpx.AsyncClient() as http:
        result = await CronRunReconciler().run(_ctx(repo, http))

    assert result.ok is True

    # Old row must be gone.
    old: CronRunFailureEnrichmentRow | None = await failure_repo.get_by_run(fp_old, run_id_old)
    assert old is None

    # Fresh row must survive.
    from homelab_monitor.kernel.logs.cron_run_failure_enrichments_repo import (  # noqa: PLC0415
        CronRunFailureEnrichmentRow,
    )

    fresh: CronRunFailureEnrichmentRow | None = await failure_repo.get_by_run(
        fp_fresh, run_id_fresh
    )
    assert fresh is not None


@pytest.mark.asyncio
async def test_failure_enrich_emits_counter_with_cron_name_host_labels(
    repo: SqliteRepository,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the cron row exists, counter labels include 'name' and 'host'.

    Covers lines 464-465 (the cron is not None branch in _failure_enrich_run).
    """
    monkeypatch.setenv("HOMELAB_MONITOR_VL_URL", "http://vl-test:9428")
    monkeypatch.setenv("HOMELAB_MONITOR_CRON_RUN_ENRICH_GRACE_SECONDS", "5")

    from homelab_monitor.kernel.cron.run_repository import CronRunRepository as _RR  # noqa: PLC0415
    from homelab_monitor.kernel.logs.cron_run_failure_enrichments_repo import (  # noqa: PLC0415
        CronRunFailureEnrichmentsRepository,
    )

    fp: str = "fp-labels-test"
    run_id: str = "run-labels-001"
    ended_at: str = (datetime.now(UTC) - timedelta(seconds=60)).isoformat()

    # Insert a cron row so cron_repo.get_cron returns non-None
    await _insert_cron(repo, fingerprint=fp, command="/usr/bin/labels-test.sh", host="labels-host")

    run_repo = _RR(repo)
    await run_repo.close_run(
        run_id=run_id,
        cron_fingerprint=fp,
        source="wrapper",
        state="fail",
        ended_at=ended_at,
        duration_seconds=5.0,
        exit_code=1,
        vl_window_end=ended_at,
    )

    # Both VL calls succeed (main enrich + failure enrich)
    httpx_mock.add_response(method="GET", text="", is_reusable=True)

    vm = MemoryRetainingMetricsWriter()
    import dataclasses  # noqa: PLC0415

    async with httpx.AsyncClient() as http:
        ctx = dataclasses.replace(_ctx(repo, http), vm=vm)
        result = await CronRunReconciler().run(ctx)

    assert result.ok is True

    counter_entries = [e for e in vm.recorded if e.name == "homelab_cron_run_failure_total"]
    assert len(counter_entries) >= 1
    labels = counter_entries[0].labels
    assert labels["cron_fingerprint"] == fp
    assert labels["run_id"] == run_id
    assert "name" in labels
    assert labels["host"] == "labels-host"

    # Failure row exists
    failure_repo = CronRunFailureEnrichmentsRepository(repo)
    assert await failure_repo.get_by_run(fp, run_id) is not None
