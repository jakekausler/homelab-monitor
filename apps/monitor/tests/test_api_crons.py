"""Session-auth route tests for /api/crons CRUD + preview-runs."""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text

from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.db.time import utc_now_iso

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _csrf(client: AsyncClient) -> dict[str, str]:
    """Return X-CSRF-Token header from the session cookie."""
    csrf: str = client.cookies.get("homelab_monitor_csrf") or ""
    return {"X-CSRF-Token": csrf}


async def _seed_cron(  # noqa: PLR0913 -- seed helpers benefit from explicit kwargs
    repo: SqliteRepository,
    *,
    id_: str,
    name: str,
    host: str = "host-a",
    command: str = "/usr/bin/true",
    schedule: str = "*/5 * * * *",
    schedule_canonical: str | None = "*/5 * * * *",
    cadence_seconds: int = 0,
    integration_mode: str = "observe",
    last_seen_state: str = "unknown",
    archived_at: str | None = None,
) -> None:
    now = utc_now_iso()
    async with repo.engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO crons ("
                "  id, name, host, command, schedule, schedule_canonical, cadence_seconds, "
                "  expected_grace_seconds, integration_mode, enabled, last_seen_state, "
                "  created_at, updated_at, archived_at"
                ") VALUES ("
                "  :id, :name, :host, :command, :schedule, :sched_canon, :cad, :grace, "
                "  :mode, :enabled, :state, :created, :updated, :archived"
                ")"
            ),
            {
                "id": id_,
                "name": name,
                "host": host,
                "command": command,
                "schedule": schedule,
                "sched_canon": schedule_canonical,
                "cad": cadence_seconds,
                "grace": 300,
                "mode": integration_mode,
                "enabled": 1,
                "state": last_seen_state,
                "created": now,
                "updated": now,
                "archived": archived_at,
            },
        )


# ---------------------------------------------------------------------------
# LIST
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_returns_empty_when_no_crons(authenticated_client: AsyncClient) -> None:
    resp = await authenticated_client.get("/api/crons")
    assert resp.status_code == 200  # noqa: PLR2004
    body = resp.json()
    assert body == {"items": [], "total": 0, "page": 1, "page_size": 100}


@pytest.mark.asyncio
async def test_list_returns_seeded_crons(
    authenticated_client: AsyncClient, repo: SqliteRepository
) -> None:
    await _seed_cron(repo, id_="c1", name="alpha")
    await _seed_cron(repo, id_="c2", name="beta", host="host-b", integration_mode="heartbeat")
    resp = await authenticated_client.get("/api/crons")
    assert resp.status_code == 200  # noqa: PLR2004
    body = resp.json()
    assert body["total"] == 2  # noqa: PLR2004
    names = [it["name"] for it in body["items"]]
    assert names == ["alpha", "beta"]  # ORDER BY name ASC


@pytest.mark.asyncio
async def test_list_pagination(authenticated_client: AsyncClient, repo: SqliteRepository) -> None:
    for i in range(5):
        await _seed_cron(repo, id_=f"c{i}", name=f"cron-{i:02d}")
    resp1 = await authenticated_client.get("/api/crons?page=1&page_size=2")
    body1 = resp1.json()
    assert body1["total"] == 5  # noqa: PLR2004
    assert len(body1["items"]) == 2  # noqa: PLR2004
    assert body1["page"] == 1
    assert body1["page_size"] == 2  # noqa: PLR2004
    assert [it["name"] for it in body1["items"]] == ["cron-00", "cron-01"]

    resp2 = await authenticated_client.get("/api/crons?page=3&page_size=2")
    body2 = resp2.json()
    assert [it["name"] for it in body2["items"]] == ["cron-04"]


@pytest.mark.asyncio
async def test_list_filter_by_host(
    authenticated_client: AsyncClient, repo: SqliteRepository
) -> None:
    await _seed_cron(repo, id_="c1", name="a", host="host-a")
    await _seed_cron(repo, id_="c2", name="b", host="host-b")
    resp = await authenticated_client.get("/api/crons?host=host-b")
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["host"] == "host-b"


@pytest.mark.asyncio
async def test_list_filter_by_integration_mode(
    authenticated_client: AsyncClient, repo: SqliteRepository
) -> None:
    await _seed_cron(repo, id_="c1", name="a", integration_mode="observe")
    await _seed_cron(repo, id_="c2", name="b", integration_mode="heartbeat")
    resp = await authenticated_client.get("/api/crons?integration_mode=heartbeat")
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["integration_mode"] == "heartbeat"


@pytest.mark.asyncio
async def test_list_search_q_matches_name_case_insensitive(
    authenticated_client: AsyncClient, repo: SqliteRepository
) -> None:
    await _seed_cron(repo, id_="c1", name="DailyBackup")
    await _seed_cron(repo, id_="c2", name="weekly")
    resp = await authenticated_client.get("/api/crons?q=daily")
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["name"] == "DailyBackup"


@pytest.mark.asyncio
async def test_list_search_q_matches_command(
    authenticated_client: AsyncClient, repo: SqliteRepository
) -> None:
    await _seed_cron(repo, id_="c1", name="alpha", command="/opt/scripts/backup.sh")
    await _seed_cron(repo, id_="c2", name="beta", command="/usr/bin/true")
    resp = await authenticated_client.get("/api/crons?q=backup")
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["name"] == "alpha"


@pytest.mark.asyncio
async def test_list_excludes_archived_by_default(
    authenticated_client: AsyncClient, repo: SqliteRepository
) -> None:
    await _seed_cron(repo, id_="c1", name="active")
    await _seed_cron(repo, id_="c2", name="archived", archived_at=utc_now_iso())
    resp = await authenticated_client.get("/api/crons")
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["name"] == "active"


@pytest.mark.asyncio
async def test_list_includes_archived_with_flag(
    authenticated_client: AsyncClient, repo: SqliteRepository
) -> None:
    await _seed_cron(repo, id_="c1", name="active")
    await _seed_cron(repo, id_="c2", name="archived", archived_at=utc_now_iso())
    resp = await authenticated_client.get("/api/crons?include_archived=true")
    body = resp.json()
    assert body["total"] == 2  # noqa: PLR2004


@pytest.mark.asyncio
async def test_list_unknown_query_param_returns_422(authenticated_client: AsyncClient) -> None:
    resp = await authenticated_client.get("/api/crons?bogus=1")
    assert resp.status_code == 422  # noqa: PLR2004
    body = resp.json()
    assert body["error"]["code"] == "validation_error"
    assert any(e["type"] == "extra_forbidden" for e in body["error"]["details"]["errors"])


@pytest.mark.asyncio
async def test_list_requires_session(api_token_client: AsyncClient) -> None:
    """Token auth is rejected on /api/crons (session-only route)."""
    resp = await api_token_client.get("/api/crons")
    assert resp.status_code == 401  # noqa: PLR2004


# ---------------------------------------------------------------------------
# GET BY ID
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_returns_404_for_unknown(authenticated_client: AsyncClient) -> None:
    resp = await authenticated_client.get("/api/crons/no-such-id")
    assert resp.status_code == 404  # noqa: PLR2004


@pytest.mark.asyncio
async def test_get_returns_cron_with_state(
    authenticated_client: AsyncClient, repo: SqliteRepository
) -> None:
    await _seed_cron(repo, id_="c1", name="alpha")
    resp = await authenticated_client.get("/api/crons/c1")
    assert resp.status_code == 200  # noqa: PLR2004
    body = resp.json()
    assert body["cron"]["id"] == "c1"
    assert body["state"] is None  # no heartbeats yet


@pytest.mark.asyncio
async def test_get_archived_returns_404_by_default(
    authenticated_client: AsyncClient, repo: SqliteRepository
) -> None:
    await _seed_cron(repo, id_="c1", name="archived", archived_at=utc_now_iso())
    resp = await authenticated_client.get("/api/crons/c1")
    assert resp.status_code == 404  # noqa: PLR2004


@pytest.mark.asyncio
async def test_get_archived_returns_with_include_archived(
    authenticated_client: AsyncClient, repo: SqliteRepository
) -> None:
    await _seed_cron(repo, id_="c1", name="archived", archived_at=utc_now_iso())
    resp = await authenticated_client.get("/api/crons/c1?include_archived=true")
    assert resp.status_code == 200  # noqa: PLR2004
    body = resp.json()
    assert body["cron"]["archived_at"] is not None


# ---------------------------------------------------------------------------
# CREATE
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_returns_201_and_persists(
    authenticated_client: AsyncClient, repo: SqliteRepository
) -> None:
    payload = {
        "name": "myCron",
        "host": "host-x",
        "command": "/usr/bin/echo hello",
        "schedule": "*/10 * * * *",
        "cadence_seconds": 0,
        "integration_mode": "heartbeat",
    }
    resp = await authenticated_client.post(
        "/api/crons", json=payload, headers=_csrf(authenticated_client)
    )
    assert resp.status_code == 201  # noqa: PLR2004
    body = resp.json()
    assert body["name"] == "myCron"
    assert body["schedule"] == "*/10 * * * *"
    assert body["schedule_canonical"] == "*/10 * * * *"
    # Verify row in DB
    row = await repo.fetch_one(text("SELECT name FROM crons WHERE id = :id"), {"id": body["id"]})
    assert row is not None
    assert row[0] == "myCron"


@pytest.mark.asyncio
async def test_create_canonicalizes_schedule(authenticated_client: AsyncClient) -> None:
    payload: dict[str, Any] = {
        "name": "hourly-job",
        "host": "host-x",
        "command": "/opt/job",
        "schedule": "@hourly",
    }
    resp = await authenticated_client.post(
        "/api/crons", json=payload, headers=_csrf(authenticated_client)
    )
    assert resp.status_code == 201  # noqa: PLR2004
    body = resp.json()
    # @hourly canonicalizes to "0 * * * *"
    assert body["schedule_canonical"] == "0 * * * *"


@pytest.mark.asyncio
async def test_create_writes_audit_log_with_after_fields(
    authenticated_client: AsyncClient, repo: SqliteRepository
) -> None:
    payload: dict[str, Any] = {
        "name": "audit-test",
        "host": "host-x",
        "command": "/opt/job",
        "schedule": "* * * * *",
    }
    resp = await authenticated_client.post(
        "/api/crons", json=payload, headers=_csrf(authenticated_client)
    )
    assert resp.status_code == 201  # noqa: PLR2004
    row = await repo.fetch_one(
        text('SELECT what, after_json FROM audit_log WHERE what = :w ORDER BY "when" DESC LIMIT 1'),
        {"w": "crons.create"},
    )
    assert row is not None
    assert row[0] == "crons.create"
    import json  # noqa: PLC0415

    after = json.loads(row[1])
    assert after["name"] == "audit-test"
    assert after["host"] == "host-x"


@pytest.mark.asyncio
async def test_create_invalid_schedule_returns_422(authenticated_client: AsyncClient) -> None:
    payload: dict[str, Any] = {
        "name": "bad",
        "host": "h",
        "command": "/x",
        "schedule": "not a cron expression",
    }
    resp = await authenticated_client.post(
        "/api/crons", json=payload, headers=_csrf(authenticated_client)
    )
    assert resp.status_code == 422  # noqa: PLR2004


@pytest.mark.asyncio
async def test_create_xor_violation_both_returns_422(authenticated_client: AsyncClient) -> None:
    payload: dict[str, Any] = {
        "name": "bad",
        "host": "h",
        "command": "/x",
        "schedule": "* * * * *",
        "cadence_seconds": 60,
    }
    resp = await authenticated_client.post(
        "/api/crons", json=payload, headers=_csrf(authenticated_client)
    )
    assert resp.status_code == 422  # noqa: PLR2004


@pytest.mark.asyncio
async def test_create_xor_violation_neither_returns_422(authenticated_client: AsyncClient) -> None:
    payload: dict[str, Any] = {
        "name": "bad",
        "host": "h",
        "command": "/x",
    }
    resp = await authenticated_client.post(
        "/api/crons", json=payload, headers=_csrf(authenticated_client)
    )
    assert resp.status_code == 422  # noqa: PLR2004


@pytest.mark.asyncio
async def test_create_unauthorized_returns_401() -> None:
    """No session, no token: 401."""
    from httpx import ASGITransport  # noqa: PLC0415
    from httpx import AsyncClient as HCli  # noqa: PLC0415

    from homelab_monitor.kernel.api.app import create_app  # noqa: PLC0415

    app = create_app(lifespan_enabled=False)
    async with HCli(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/crons", json={"name": "x", "host": "h", "command": "c"})
        assert resp.status_code == 401  # noqa: PLR2004


@pytest.mark.asyncio
async def test_create_no_csrf_returns_403(authenticated_client: AsyncClient) -> None:
    """Session cookie present but X-CSRF-Token header omitted -> 403."""
    payload = {"name": "x", "host": "h", "command": "/x", "schedule": "* * * * *"}
    resp = await authenticated_client.post("/api/crons", json=payload)  # no CSRF header
    assert resp.status_code == 403  # noqa: PLR2004


# ---------------------------------------------------------------------------
# PATCH
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_patch_updates_grace_seconds_and_audits_changed_fields_only(
    authenticated_client: AsyncClient, repo: SqliteRepository
) -> None:
    await _seed_cron(repo, id_="c1", name="p1")
    resp = await authenticated_client.patch(
        "/api/crons/c1",
        json={"expected_grace_seconds": 600},
        headers=_csrf(authenticated_client),
    )
    assert resp.status_code == 200  # noqa: PLR2004
    body = resp.json()
    assert body["expected_grace_seconds"] == 600  # noqa: PLR2004

    audit = await repo.fetch_one(
        text(
            "SELECT what, before_json, after_json FROM audit_log "
            'WHERE what = :w ORDER BY "when" DESC LIMIT 1'
        ),
        {"w": "crons.update"},
    )
    assert audit is not None
    import json  # noqa: PLC0415

    before = json.loads(audit[1])
    after = json.loads(audit[2])
    # Only the changed field should be in before/after (plus updated_at bookkeeping).
    assert "expected_grace_seconds" in before
    assert "expected_grace_seconds" in after
    assert "name" not in before  # name unchanged
    assert before["expected_grace_seconds"] == 300  # noqa: PLR2004
    assert after["expected_grace_seconds"] == 600  # noqa: PLR2004


@pytest.mark.asyncio
async def test_patch_empty_diff_returns_200_with_no_audit_row(
    authenticated_client: AsyncClient, repo: SqliteRepository
) -> None:
    await _seed_cron(repo, id_="c1", name="p1")
    # PATCH with the SAME grace value → no diff → no audit row
    resp = await authenticated_client.patch(
        "/api/crons/c1",
        json={"expected_grace_seconds": 300},
        headers=_csrf(authenticated_client),
    )
    assert resp.status_code == 200  # noqa: PLR2004

    audit_count = await repo.fetch_one(
        text("SELECT COUNT(*) FROM audit_log WHERE what LIKE 'crons.%'")
    )
    assert audit_count is not None
    assert int(audit_count[0]) == 0


@pytest.mark.asyncio
async def test_patch_recanonicalizes_schedule_on_change(
    authenticated_client: AsyncClient, repo: SqliteRepository
) -> None:
    await _seed_cron(
        repo, id_="c1", name="p1", schedule="* * * * *", schedule_canonical="* * * * *"
    )
    resp = await authenticated_client.patch(
        "/api/crons/c1",
        json={"schedule": "@hourly"},
        headers=_csrf(authenticated_client),
    )
    assert resp.status_code == 200  # noqa: PLR2004
    body = resp.json()
    assert body["schedule"] == "@hourly"
    assert body["schedule_canonical"] == "0 * * * *"


@pytest.mark.asyncio
async def test_patch_archived_at_emits_crons_delete_audit(
    authenticated_client: AsyncClient, repo: SqliteRepository
) -> None:
    await _seed_cron(repo, id_="c1", name="to-archive")
    resp = await authenticated_client.patch(
        "/api/crons/c1",
        json={"archived_at": utc_now_iso()},
        headers=_csrf(authenticated_client),
    )
    assert resp.status_code == 200  # noqa: PLR2004

    row = await repo.fetch_one(text('SELECT what FROM audit_log ORDER BY "when" DESC LIMIT 1'))
    assert row is not None
    assert row[0] == "crons.delete"


@pytest.mark.asyncio
async def test_patch_restore_emits_crons_restore_audit(
    authenticated_client: AsyncClient, repo: SqliteRepository
) -> None:
    await _seed_cron(repo, id_="c1", name="to-restore", archived_at=utc_now_iso())
    resp = await authenticated_client.patch(
        "/api/crons/c1",
        json={"archived_at": None},
        headers=_csrf(authenticated_client),
    )
    assert resp.status_code == 200  # noqa: PLR2004

    row = await repo.fetch_one(text('SELECT what FROM audit_log ORDER BY "when" DESC LIMIT 1'))
    assert row is not None
    assert row[0] == "crons.restore"


# ---------------------------------------------------------------------------
# DELETE (soft)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_soft_deletes_and_emits_crons_delete_audit(
    authenticated_client: AsyncClient, repo: SqliteRepository
) -> None:
    await _seed_cron(repo, id_="c1", name="to-soft-delete")
    resp = await authenticated_client.delete("/api/crons/c1", headers=_csrf(authenticated_client))
    assert resp.status_code == 204  # noqa: PLR2004

    row = await repo.fetch_one(text("SELECT archived_at FROM crons WHERE id = :id"), {"id": "c1"})
    assert row is not None
    assert row[0] is not None  # archived_at set

    audit = await repo.fetch_one(text('SELECT what FROM audit_log ORDER BY "when" DESC LIMIT 1'))
    assert audit is not None
    assert audit[0] == "crons.delete"


@pytest.mark.asyncio
async def test_delete_unknown_returns_404(authenticated_client: AsyncClient) -> None:
    resp = await authenticated_client.delete(
        "/api/crons/no-such-id", headers=_csrf(authenticated_client)
    )
    assert resp.status_code == 404  # noqa: PLR2004


@pytest.mark.asyncio
async def test_delete_already_archived_returns_404(
    authenticated_client: AsyncClient, repo: SqliteRepository
) -> None:
    await _seed_cron(repo, id_="c1", name="x", archived_at=utc_now_iso())
    resp = await authenticated_client.delete("/api/crons/c1", headers=_csrf(authenticated_client))
    assert resp.status_code == 404  # noqa: PLR2004


# ---------------------------------------------------------------------------
# PREVIEW RUNS
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_preview_runs_for_saved_cron(
    authenticated_client: AsyncClient, repo: SqliteRepository
) -> None:
    await _seed_cron(repo, id_="c1", name="preview", schedule="0 * * * *")
    resp = await authenticated_client.get("/api/crons/c1/preview-runs?count=3")
    assert resp.status_code == 200  # noqa: PLR2004
    body = resp.json()
    assert len(body["runs"]) == 3  # noqa: PLR2004


@pytest.mark.asyncio
async def test_get_preview_runs_for_cadence_only_cron_returns_404(
    authenticated_client: AsyncClient, repo: SqliteRepository
) -> None:
    await _seed_cron(
        repo,
        id_="c1",
        name="cadence-only",
        schedule="",
        schedule_canonical=None,
        cadence_seconds=60,
    )
    resp = await authenticated_client.get("/api/crons/c1/preview-runs")
    assert resp.status_code == 404  # noqa: PLR2004


@pytest.mark.asyncio
async def test_get_preview_runs_unknown_cron_returns_404(
    authenticated_client: AsyncClient,
) -> None:
    resp = await authenticated_client.get("/api/crons/missing/preview-runs")
    assert resp.status_code == 404  # noqa: PLR2004


@pytest.mark.asyncio
async def test_get_preview_runs_unsaved_input(authenticated_client: AsyncClient) -> None:
    resp = await authenticated_client.get("/api/crons/preview-runs?expr=*+*+*+*+*&count=2")
    assert resp.status_code == 200  # noqa: PLR2004
    body = resp.json()
    assert len(body["runs"]) == 2  # noqa: PLR2004


@pytest.mark.asyncio
async def test_get_preview_runs_unsaved_invalid_expr_returns_422(
    authenticated_client: AsyncClient,
) -> None:
    resp = await authenticated_client.get("/api/crons/preview-runs?expr=garbage")
    assert resp.status_code == 422  # noqa: PLR2004


@pytest.mark.asyncio
async def test_get_preview_runs_unsaved_count_above_limit_returns_422(
    authenticated_client: AsyncClient,
) -> None:
    resp = await authenticated_client.get("/api/crons/preview-runs?expr=*+*+*+*+*&count=11")
    assert resp.status_code == 422  # noqa: PLR2004


@pytest.mark.asyncio
async def test_create_with_invalid_cron_expr_in_payload_returns_422(
    authenticated_client: AsyncClient,
) -> None:
    """POST /api/crons with invalid cron expression in schedule field returns 422."""
    csrf = _csrf(authenticated_client)
    resp = await authenticated_client.post(
        "/api/crons",
        json={
            "name": "bad-cron",
            "host": "h1",
            "command": "/bin/true",
            "schedule": "not a real cron expression",
            "integration_mode": "observe",
        },
        headers=csrf,
    )
    assert resp.status_code == 422  # noqa: PLR2004


@pytest.mark.asyncio
async def test_create_with_neither_schedule_nor_cadence_returns_422(
    authenticated_client: AsyncClient,
) -> None:
    """POST /api/crons with neither schedule nor cadence returns 422 (xor)."""
    csrf = _csrf(authenticated_client)
    resp = await authenticated_client.post(
        "/api/crons",
        json={
            "name": "no-schedule-cron",
            "host": "h1",
            "command": "/bin/true",
            "schedule": None,
            "cadence_seconds": 0,
            "integration_mode": "observe",
        },
        headers=csrf,
    )
    assert resp.status_code == 422  # noqa: PLR2004


@pytest.mark.asyncio
async def test_preview_runs_unsaved_missing_expr_returns_404(
    authenticated_client: AsyncClient,
) -> None:
    """GET /api/crons/preview-runs without ?expr= returns 404 (NotFoundProblem)."""
    resp = await authenticated_client.get("/api/crons/preview-runs?count=3")
    assert resp.status_code == 404  # noqa: PLR2004


@pytest.mark.asyncio
async def test_update_cron_not_found_returns_404(
    authenticated_client: AsyncClient,
) -> None:
    """PATCH /api/crons/{unknown-id} returns 404."""
    csrf = _csrf(authenticated_client)
    resp = await authenticated_client.patch(
        "/api/crons/nonexistent-cron-id",
        json={"expected_grace_seconds": 600},
        headers=csrf,
    )
    assert resp.status_code == 404  # noqa: PLR2004
