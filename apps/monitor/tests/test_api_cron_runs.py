"""Session-auth route tests for GET /api/crons/{fp}/runs and GET /api/crons/{fp}/runs/{run_id}/log.

Tests added in STAGE-002-014.

Project test conventions:
- @pytest.mark.asyncio for async tests
- noqa: PLR2004 for magic number assertions
- noqa: PLC0415 for function-scoped imports
- pyright: ignore[reportPrivateUsage] for private symbol access
- httpx_mock with is_reusable=True for shared mock responses
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pytest_httpx import HTTPXMock
from sqlalchemy import text

from homelab_monitor.kernel.cron.fingerprint import compute_fingerprint
from homelab_monitor.kernel.cron.run_repository import CronRunRepository
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.db.time import utc_now_iso

# ---------------------------------------------------------------------------
# Helpers — seed
# ---------------------------------------------------------------------------


async def _seed_cron(  # noqa: PLR0913
    repo: SqliteRepository,
    *,
    name: str,
    host: str = "host-a",
    command: str | None = None,
    schedule: str = "*/5 * * * *",
    source_path: str | None = "/etc/crontab",
    fingerprint: str | None = None,
) -> str:
    command = command if command is not None else f"/usr/bin/true-{name}"
    fp = fingerprint or compute_fingerprint(
        host=host, source_path=source_path, schedule=schedule, command=command
    )
    now = utc_now_iso()
    async with repo.engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO crons (fingerprint, name, host, command, schedule, "
                "schedule_canonical, cadence_seconds, expected_grace_seconds, "
                "enabled, last_seen_state, created_at, updated_at, hidden_at, "
                "source_path, wrapper_last_seen_at, soft_deleted_at) VALUES ("
                ":fp, :name, :host, :command, :schedule, :sched_canon, :cad, "
                ":grace, :enabled, :state, :created, :updated, :hidden, :sp, :wia, :sda)"
            ),
            {
                "fp": fp,
                "name": name,
                "host": host,
                "command": command,
                "schedule": schedule,
                "sched_canon": schedule,
                "cad": 300,
                "grace": 300,
                "enabled": 1,
                "state": "unknown",
                "created": now,
                "updated": now,
                "hidden": None,
                "sp": source_path,
                "wia": None,
                "sda": None,
            },
        )
    return fp


async def _seed_run(  # noqa: PLR0913
    repo: SqliteRepository,
    *,
    run_id: str,
    cron_fingerprint: str,
    source: str = "wrapper",
    state: str = "ok",
    started_at: str | None = None,
    ended_at: str | None = None,
    duration_seconds: float = 5.0,
    exit_code: int | None = 0,
    vl_window_start: str | None = None,
    vl_window_end: str | None = None,
    line_count: int | None = None,
    byte_count: int | None = None,
    anomaly_flags: str = "",
    enriched_at: str | None = None,
) -> None:
    run_repo = CronRunRepository(repo)
    _started = started_at or "2026-05-19T00:00:00+00:00"
    _ended = ended_at or "2026-05-19T00:00:05+00:00"
    _vl_start = vl_window_start or _started
    _vl_end = vl_window_end or _ended

    if state == "running":
        await run_repo.insert_run(
            run_id=run_id,
            cron_fingerprint=cron_fingerprint,
            source=source,
            started_at=_started,
            vl_window_start=_vl_start,
        )
    else:
        await run_repo.close_run(
            run_id=run_id,
            cron_fingerprint=cron_fingerprint,
            source=source,
            state=state,
            ended_at=_ended,
            duration_seconds=duration_seconds,
            exit_code=exit_code,
            vl_window_end=_vl_end,
        )
        if line_count is not None or anomaly_flags:
            async with repo.engine.begin() as conn:
                await conn.execute(
                    text(
                        "UPDATE cron_runs SET line_count=:lc, byte_count=:bc, "
                        "anomaly_flags=:af, enriched_at=:ea WHERE run_id=:rid"
                    ),
                    {
                        "lc": line_count,
                        "bc": byte_count,
                        "af": anomaly_flags,
                        "ea": enriched_at or utc_now_iso(),
                        "rid": run_id,
                    },
                )


def _make_ndjson(*msgs: str) -> str:
    lines: list[str] = []
    for msg in msgs:
        lines.append(
            json.dumps(
                {
                    "_stream_id": "svc.host",
                    "_msg": msg,
                    "_time": "2026-05-19T00:00:01+00:00",
                }
            )
        )
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# GET /api/crons/{fp}/runs — happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_cron_runs_happy_path(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
) -> None:
    """Happy path: returns CronRunListResponse with correct field shapes."""
    fp = await _seed_cron(repo, name="run-list-happy")
    await _seed_run(
        repo,
        run_id="rlh-run-1",
        cron_fingerprint=fp,
        line_count=10,
        byte_count=500,
        anomaly_flags="duration_outlier",
        enriched_at=utc_now_iso(),
    )

    resp = await authenticated_client.get(f"/api/crons/{fp}/runs")
    assert resp.status_code == 200  # noqa: PLR2004
    body = resp.json()
    assert "items" in body
    assert "next_cursor" in body
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item["run_id"] == "rlh-run-1"
    assert item["state"] == "ok"
    assert item["line_count"] == 10  # noqa: PLR2004
    assert item["anomaly_flags"] == "duration_outlier"
    assert item["enriched"] is True
    # Internal fields must NOT appear
    assert "content_digest" not in item
    assert "vl_window_start" not in item
    assert "enriched_at" not in item


@pytest.mark.asyncio
async def test_list_cron_runs_pagination(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
) -> None:
    """Pagination: seed N+2 runs, request limit=N, follow cursor, no overlap."""
    fp = await _seed_cron(repo, name="run-list-page")
    # Insert 5 runs with distinct timestamps
    for i in range(5):
        ts = f"2026-05-19T00:00:0{i}+00:00"
        await _seed_run(
            repo,
            run_id=f"rl-page-{i}",
            cron_fingerprint=fp,
            started_at=ts,
            ended_at=ts,
        )

    # Page 1 (limit=3)
    resp1 = await authenticated_client.get(f"/api/crons/{fp}/runs", params={"limit": 3})
    assert resp1.status_code == 200  # noqa: PLR2004
    body1 = resp1.json()
    assert len(body1["items"]) == 3  # noqa: PLR2004
    assert body1["next_cursor"] is not None
    ids_page1 = {it["run_id"] for it in body1["items"]}

    # Page 2
    resp2 = await authenticated_client.get(
        f"/api/crons/{fp}/runs",
        params={"limit": 3, "cursor": body1["next_cursor"]},
    )
    assert resp2.status_code == 200  # noqa: PLR2004
    body2 = resp2.json()
    assert len(body2["items"]) == 2  # noqa: PLR2004
    ids_page2 = {it["run_id"] for it in body2["items"]}

    # No overlap
    assert ids_page1.isdisjoint(ids_page2)
    # Together they cover all 5
    assert ids_page1 | ids_page2 == {f"rl-page-{i}" for i in range(5)}


@pytest.mark.asyncio
async def test_list_cron_runs_state_filter(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
) -> None:
    """state query param filters runs by state."""
    fp = await _seed_cron(repo, name="run-list-filter")
    await _seed_run(repo, run_id="rlf-ok", cron_fingerprint=fp, state="ok")
    await _seed_run(repo, run_id="rlf-fail", cron_fingerprint=fp, state="fail", exit_code=1)

    resp = await authenticated_client.get(f"/api/crons/{fp}/runs", params={"state": "ok"})
    assert resp.status_code == 200  # noqa: PLR2004
    body = resp.json()
    assert all(it["state"] == "ok" for it in body["items"])
    run_ids = {it["run_id"] for it in body["items"]}
    assert "rlf-ok" in run_ids
    assert "rlf-fail" not in run_ids


@pytest.mark.asyncio
async def test_list_cron_runs_404_unknown_fingerprint(
    authenticated_client: AsyncClient,
) -> None:
    """Unknown fingerprint returns 404."""
    resp = await authenticated_client.get("/api/crons/no-such-fp/runs")
    assert resp.status_code == 404  # noqa: PLR2004


@pytest.mark.asyncio
async def test_list_cron_runs_401_without_session(
    authenticated_client: AsyncClient,
) -> None:
    """Unauthenticated request returns 401."""
    app = cast(
        FastAPI,
        authenticated_client._transport.app,  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage, reportUnknownMemberType]
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as anon:
        resp = await anon.get("/api/crons/some-fp/runs")
    assert resp.status_code == 401  # noqa: PLR2004


@pytest.mark.asyncio
async def test_list_cron_runs_400_malformed_cursor(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
) -> None:
    """Malformed cursor returns 400 with code='invalid_cursor'."""
    fp = await _seed_cron(repo, name="run-list-badcur")
    resp = await authenticated_client.get(
        f"/api/crons/{fp}/runs", params={"cursor": "!!!garbage!!!"}
    )
    assert resp.status_code == 400  # noqa: PLR2004
    assert resp.json()["error"]["code"] == "invalid_cursor"


@pytest.mark.asyncio
async def test_list_cron_runs_422_invalid_state_value(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
) -> None:
    """Invalid state literal returns 422 (pydantic validation)."""
    fp = await _seed_cron(repo, name="run-list-badstate")
    resp = await authenticated_client.get(f"/api/crons/{fp}/runs", params={"state": "bogus"})
    assert resp.status_code == 422  # noqa: PLR2004


@pytest.mark.asyncio
async def test_list_cron_runs_enriched_boolean_from_enriched_at(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
) -> None:
    """enriched=True when enriched_at IS NOT NULL, False otherwise."""
    fp = await _seed_cron(repo, name="run-list-enriched")
    await _seed_run(
        repo,
        run_id="enriched-yes",
        cron_fingerprint=fp,
        line_count=5,
        enriched_at=utc_now_iso(),
    )
    await _seed_run(repo, run_id="enriched-no", cron_fingerprint=fp)

    resp = await authenticated_client.get(f"/api/crons/{fp}/runs")
    assert resp.status_code == 200  # noqa: PLR2004
    items = {it["run_id"]: it for it in resp.json()["items"]}
    assert items["enriched-yes"]["enriched"] is True
    assert items["enriched-no"]["enriched"] is False


# ---------------------------------------------------------------------------
# GET /api/crons/{fp}/runs/{run_id}/log — shape 1: available
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_run_log_available(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shape 1 (available): closed enriched run, VL returns 2 lines."""
    monkeypatch.setenv("HOMELAB_MONITOR_VL_URL", "http://vl-test:9428")
    fp = await _seed_cron(repo, name="log-avail")
    now_ts = "2026-05-19T00:00:00+00:00"
    await _seed_run(
        repo,
        run_id="log-avail-run",
        cron_fingerprint=fp,
        state="ok",
        started_at=now_ts,
        ended_at=now_ts,
        vl_window_start=now_ts,
        vl_window_end=now_ts,
        line_count=2,
        byte_count=20,
        anomaly_flags="",
        enriched_at=utc_now_iso(),
    )

    httpx_mock.add_response(
        method="GET",
        text=_make_ndjson("line one", "line two"),
        is_reusable=True,
    )

    resp = await authenticated_client.get(f"/api/crons/{fp}/runs/log-avail-run/log")
    assert resp.status_code == 200  # noqa: PLR2004
    body = resp.json()
    assert body["log_status"] == "available"
    assert len(body["lines"]) == 2  # noqa: PLR2004
    assert body["truncated"] is False
    assert body["state"] == "ok"
    assert body["line_count"] == 2  # noqa: PLR2004
    assert body["anomaly_flags"] == ""


# ---------------------------------------------------------------------------
# Shape 2: running
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_run_log_running_shape(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shape 2 (running): in-flight run; VL queried with now() as upper bound."""
    monkeypatch.setenv("HOMELAB_MONITOR_VL_URL", "http://vl-test:9428")
    fp = await _seed_cron(repo, name="log-running")
    await _seed_run(
        repo,
        run_id="log-running-run",
        cron_fingerprint=fp,
        state="running",
        started_at="2026-05-19T00:00:00+00:00",
    )

    before = datetime.now(UTC)
    httpx_mock.add_response(
        method="GET",
        text=_make_ndjson("partial line"),
        is_reusable=True,
    )

    resp = await authenticated_client.get(f"/api/crons/{fp}/runs/log-running-run/log")
    after = datetime.now(UTC)
    assert resp.status_code == 200  # noqa: PLR2004
    body = resp.json()
    assert body["log_status"] == "running"
    assert len(body["lines"]) == 1

    # The VL request's 'end' param should be close to now()
    requests = httpx_mock.get_requests()
    assert len(requests) >= 1
    end_param = requests[-1].url.params.get("end", "")
    # end_param is a UTC timestamp string; parse and verify it's within a few seconds
    end_dt = datetime.fromisoformat(end_param.replace("Z", "+00:00"))
    assert before - timedelta(seconds=5) <= end_dt <= after + timedelta(seconds=5)


# ---------------------------------------------------------------------------
# Shape 3: expired
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_run_log_expired_old_window(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shape 3 (expired): vl_window_end older than retention; no VL request made."""
    monkeypatch.setenv("HOMELAB_MONITOR_VL_URL", "http://vl-test:9428")
    monkeypatch.setenv("HOMELAB_MONITOR_VL_RETENTION_DAYS", "30")
    fp = await _seed_cron(repo, name="log-expired")

    # Timestamp 60 days ago → past retention
    old_ts = (datetime.now(UTC) - timedelta(days=60)).isoformat()
    await _seed_run(
        repo,
        run_id="log-expired-run",
        cron_fingerprint=fp,
        state="ok",
        started_at=old_ts,
        ended_at=old_ts,
        vl_window_start=old_ts,
        vl_window_end=old_ts,
    )

    resp = await authenticated_client.get(f"/api/crons/{fp}/runs/log-expired-run/log")
    assert resp.status_code == 200  # noqa: PLR2004
    body = resp.json()
    assert body["log_status"] == "expired"
    assert body["lines"] == []

    # VL must not have been contacted
    assert len(httpx_mock.get_requests()) == 0


# ---------------------------------------------------------------------------
# Shape 4: 503 vl_unavailable
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_run_log_503_on_vl_error(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """VictoriaLogsClientError from VL → 503 with code='vl_unavailable'."""
    monkeypatch.setenv("HOMELAB_MONITOR_VL_URL", "http://vl-test:9428")
    fp = await _seed_cron(repo, name="log-503")
    now_ts = "2026-05-19T00:00:00+00:00"
    await _seed_run(
        repo,
        run_id="log-503-run",
        cron_fingerprint=fp,
        state="ok",
        started_at=now_ts,
        ended_at=now_ts,
        vl_window_start=now_ts,
        vl_window_end=now_ts,
    )

    httpx_mock.add_response(method="GET", status_code=500, text="vl error")

    resp = await authenticated_client.get(f"/api/crons/{fp}/runs/log-503-run/log")
    assert resp.status_code == 503  # noqa: PLR2004
    assert resp.json()["error"]["code"] == "vl_unavailable"


# ---------------------------------------------------------------------------
# 404 cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_run_log_404_run_not_found(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
) -> None:
    """404 when run_id does not exist."""
    fp = await _seed_cron(repo, name="log-404-run")
    resp = await authenticated_client.get(f"/api/crons/{fp}/runs/no-such-run/log")
    assert resp.status_code == 404  # noqa: PLR2004


@pytest.mark.asyncio
async def test_get_run_log_404_cross_cron_run_id(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
) -> None:
    """404 when run belongs to a different cron fingerprint (cross-cron leak prevention)."""
    fp_a = await _seed_cron(repo, name="log-cross-a")
    fp_b = await _seed_cron(repo, name="log-cross-b", command="/usr/bin/b")
    await _seed_run(repo, run_id="cross-run", cron_fingerprint=fp_a)

    # Request the run via fp_b — must return 404, not the run
    resp = await authenticated_client.get(f"/api/crons/{fp_b}/runs/cross-run/log")
    assert resp.status_code == 404  # noqa: PLR2004


@pytest.mark.asyncio
async def test_get_run_log_401_without_session(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
) -> None:
    """Unauthenticated request returns 401."""
    fp = await _seed_cron(repo, name="log-401")
    app = cast(
        FastAPI,
        authenticated_client._transport.app,  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage, reportUnknownMemberType]
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as anon:
        resp = await anon.get(f"/api/crons/{fp}/runs/some-run/log")
    assert resp.status_code == 401  # noqa: PLR2004


# ---------------------------------------------------------------------------
# Source-mode VL query construction (A-mode vs B-mode)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_run_log_amode_query_contains_run_id(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """source='wrapper' (A-mode): VL request URL contains run_id:"<uuid>"."""
    monkeypatch.setenv("HOMELAB_MONITOR_VL_URL", "http://vl-test:9428")
    fp = await _seed_cron(repo, name="log-amode")
    now_ts = "2026-05-19T00:00:00+00:00"
    await _seed_run(
        repo,
        run_id="amode-run-id",
        cron_fingerprint=fp,
        source="wrapper",
        state="ok",
        started_at=now_ts,
        ended_at=now_ts,
        vl_window_start=now_ts,
        vl_window_end=now_ts,
    )

    httpx_mock.add_response(method="GET", text="", is_reusable=True)

    await authenticated_client.get(f"/api/crons/{fp}/runs/amode-run-id/log")
    requests = httpx_mock.get_requests()
    assert len(requests) >= 1
    query_param = requests[-1].url.params.get("query", "")
    assert 'run_id:"amode-run-id"' in query_param


@pytest.mark.asyncio
async def test_get_run_log_bmode_query_contains_canonical_key(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """source='logscrape' (B-mode): VL request contains canonical-key phrase."""
    monkeypatch.setenv("HOMELAB_MONITOR_VL_URL", "http://vl-test:9428")

    from homelab_monitor.kernel.cron.log_match import canonical_log_key  # noqa: PLC0415

    command = "/usr/bin/bmode-test.sh"
    fp = await _seed_cron(repo, name="log-bmode", command=command)
    now_ts = "2026-05-19T00:00:00+00:00"
    await _seed_run(
        repo,
        run_id="bmode-run-id",
        cron_fingerprint=fp,
        source="logscrape",
        state="ok",
        started_at=now_ts,
        ended_at=now_ts,
        vl_window_start=now_ts,
        vl_window_end=now_ts,
    )

    httpx_mock.add_response(method="GET", text="", is_reusable=True)

    await authenticated_client.get(f"/api/crons/{fp}/runs/bmode-run-id/log")
    requests = httpx_mock.get_requests()
    assert len(requests) >= 1
    query_param = requests[-1].url.params.get("query", "")
    lmk = canonical_log_key(command)
    assert lmk in query_param


# ---------------------------------------------------------------------------
# 404 — unknown fingerprint on run-log endpoint (crons.py:385)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_run_log_404_unknown_fingerprint(
    authenticated_client: AsyncClient,
) -> None:
    """Unknown fingerprint on log endpoint returns 404 with code='not_found'."""
    resp = await authenticated_client.get(
        "/api/crons/nonexistent-fingerprint-xyz/runs/any-run-id/log"
    )
    assert resp.status_code == 404  # noqa: PLR2004
    body = resp.json()
    assert body["error"]["code"] == "not_found"


# ---------------------------------------------------------------------------
# expired — closed run with NULL vl_window_end (crons.py:411)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_run_log_expired_null_vl_window_end(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    httpx_mock: HTTPXMock,
) -> None:
    """Closed run with NULL vl_window_end → defensive expired; no VL call."""
    fp = await _seed_cron(repo, name="log-null-vl-end")
    await _seed_run(
        repo,
        run_id="log-null-vl-end-run",
        cron_fingerprint=fp,
        state="ok",
    )
    async with repo.engine.begin() as conn:
        await conn.execute(
            text("UPDATE cron_runs SET vl_window_end = NULL WHERE run_id = :rid"),
            {"rid": "log-null-vl-end-run"},
        )

    resp = await authenticated_client.get(f"/api/crons/{fp}/runs/log-null-vl-end-run/log")
    assert resp.status_code == 200  # noqa: PLR2004
    body = resp.json()
    assert body["log_status"] == "expired"
    assert body["lines"] == []
    assert len(httpx_mock.get_requests()) == 0


# ---------------------------------------------------------------------------
# available — naive vl_window_end within retention (crons.py:417)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_run_log_naive_vl_window_end_within_retention(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Naive (no tz) vl_window_end within retention → UTC-attached, log available."""
    from datetime import UTC as _UTC  # noqa: PLC0415

    monkeypatch.setenv("HOMELAB_MONITOR_VL_URL", "http://vl-test:9428")
    monkeypatch.setenv("HOMELAB_MONITOR_VL_RETENTION_DAYS", "30")

    fp = await _seed_cron(repo, name="log-naive-vl")
    await _seed_run(
        repo,
        run_id="log-naive-vl-run",
        cron_fingerprint=fp,
        state="ok",
    )

    # Overwrite both window fields with a naive ISO string (no +00:00)
    naive_ts = datetime.now(_UTC).strftime("%Y-%m-%dT%H:%M:%S")
    async with repo.engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE cron_runs SET vl_window_start = :ts, vl_window_end = :ts"
                " WHERE run_id = :rid"
            ),
            {"ts": naive_ts, "rid": "log-naive-vl-run"},
        )

    httpx_mock.add_response(
        method="GET",
        text=_make_ndjson("a log line"),
        is_reusable=True,
    )

    resp = await authenticated_client.get(f"/api/crons/{fp}/runs/log-naive-vl-run/log")
    assert resp.status_code == 200  # noqa: PLR2004
    body = resp.json()
    assert body["log_status"] == "available"
    assert len(httpx_mock.get_requests()) >= 1


# ---------------------------------------------------------------------------
# 404 — soft-deleted cron on run-log endpoint (crons.py:391)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_run_log_404_soft_deleted_cron(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
) -> None:
    """Run-log endpoint returns 404 when the cron is soft-deleted."""
    fp = await _seed_cron(repo, name="soft-deleted-log-cron")
    await _seed_run(repo, run_id="soft-deleted-log-run", cron_fingerprint=fp, state="ok")
    async with repo.engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE crons SET soft_deleted_at = '2026-01-01T00:00:00+00:00' "
                "WHERE fingerprint = :fp"
            ),
            {"fp": fp},
        )
    resp = await authenticated_client.get(f"/api/crons/{fp}/runs/soft-deleted-log-run/log")
    assert resp.status_code == 404  # noqa: PLR2004
    body = resp.json()
    assert body["error"]["code"] == "not_found"


# ---------------------------------------------------------------------------
# 404 — soft-deleted cron on list-runs endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_cron_runs_404_soft_deleted_cron(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
) -> None:
    """list_cron_runs returns 404 when the cron is soft-deleted."""
    fp = await _seed_cron(repo, name="soft-deleted-list-cron")
    await _seed_run(repo, run_id="soft-deleted-list-run", cron_fingerprint=fp)
    async with repo.engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE crons SET soft_deleted_at = '2026-01-01T00:00:00+00:00' "
                "WHERE fingerprint = :fp"
            ),
            {"fp": fp},
        )
    resp = await authenticated_client.get(f"/api/crons/{fp}/runs")
    assert resp.status_code == 404  # noqa: PLR2004
    body = resp.json()
    assert body["error"]["code"] == "not_found"


# ---------------------------------------------------------------------------
# VL query window slack widening (STAGE-002-015)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_run_log_widens_vl_query_with_slack(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Closed wrapper run: VL query end is widened by enrich_window_slack_seconds.

    Validates that the run-log endpoint applies the same slack widening as the
    reconciler's _enrich, ensuring the query window includes log lines that
    arrive after the wrapper's ended_at due to journald → Vector → VL ingest
    latency.
    """
    monkeypatch.setenv("HOMELAB_MONITOR_VL_URL", "http://vl-test:9428")
    monkeypatch.setenv("HOMELAB_MONITOR_CRON_RUN_ENRICH_WINDOW_SLACK_SECONDS", "60")

    fp = await _seed_cron(repo, name="log-slack-test")
    base_ts = "2026-05-19T00:00:00+00:00"
    await _seed_run(
        repo,
        run_id="slack-test-run",
        cron_fingerprint=fp,
        source="wrapper",
        state="ok",
        started_at=base_ts,
        ended_at=base_ts,
        vl_window_start=base_ts,
        vl_window_end=base_ts,
        line_count=1,
        byte_count=10,
        enriched_at=utc_now_iso(),
    )

    httpx_mock.add_response(
        method="GET",
        text=_make_ndjson("a log line"),
        is_reusable=True,
    )

    resp = await authenticated_client.get(f"/api/crons/{fp}/runs/slack-test-run/log")
    assert resp.status_code == 200  # noqa: PLR2004
    body = resp.json()
    assert body["log_status"] == "available"

    # Verify the VL request's 'end' param includes the slack
    requests = httpx_mock.get_requests()
    assert len(requests) >= 1
    end_param = requests[-1].url.params.get("end", "")

    # Parse the end timestamp and verify it's approximately base_ts + 60s
    end_dt = datetime.fromisoformat(end_param.replace("Z", "+00:00"))
    base_dt = datetime.fromisoformat(base_ts)
    slack_seconds = (end_dt - base_dt).total_seconds()

    # Allow ±2s tolerance for parsing/serialization precision
    assert 58 <= slack_seconds <= 62, f"Expected slack ~60s, got {slack_seconds}s"  # noqa: PLR2004


@pytest.mark.asyncio
async def test_get_run_log_no_slack_when_disabled(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Closed wrapper run: VL query end is NOT widened when slack is 0.

    Validates that when HOMELAB_MONITOR_CRON_RUN_ENRICH_WINDOW_SLACK_SECONDS=0
    the run-log endpoint issues a VL query whose 'end' param equals the stored
    vl_window_end (no slack added), mirroring the reconciler behaviour tested
    in test_enrich_query_no_slack_when_disabled.
    """
    monkeypatch.setenv("HOMELAB_MONITOR_VL_URL", "http://vl-test:9428")
    monkeypatch.setenv("HOMELAB_MONITOR_CRON_RUN_ENRICH_WINDOW_SLACK_SECONDS", "0")

    fp = await _seed_cron(repo, name="log-noslack-test")
    base_ts = "2026-05-19T00:00:00+00:00"
    await _seed_run(
        repo,
        run_id="noslack-test-run",
        cron_fingerprint=fp,
        source="wrapper",
        state="ok",
        started_at=base_ts,
        ended_at=base_ts,
        vl_window_start=base_ts,
        vl_window_end=base_ts,
        line_count=1,
        byte_count=10,
        enriched_at=utc_now_iso(),
    )

    httpx_mock.add_response(
        method="GET",
        text=_make_ndjson("a log line"),
        is_reusable=True,
    )

    resp = await authenticated_client.get(f"/api/crons/{fp}/runs/noslack-test-run/log")
    assert resp.status_code == 200  # noqa: PLR2004
    body = resp.json()
    assert body["log_status"] == "available"

    # Verify the VL request's 'end' param equals base_ts (no slack widening)
    requests = httpx_mock.get_requests()
    assert len(requests) >= 1
    end_param = requests[-1].url.params.get("end", "")
    assert end_param, "end query parameter must be present"

    end_dt = datetime.fromisoformat(end_param.replace("Z", "+00:00"))
    base_dt = datetime.fromisoformat(base_ts)
    delta = abs((end_dt - base_dt).total_seconds())

    # Allow ±2s tolerance for parsing/serialization precision; slack must be ~0
    assert delta < 2, f"Expected no slack, but end param differs from base_ts by {delta}s"  # noqa: PLR2004
