"""Session-auth route tests for /api/crons CRUD + preview-runs."""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text

from homelab_monitor.kernel.cron.fingerprint import compute_fingerprint
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
    name: str,
    host: str = "host-a",
    command: str | None = None,
    schedule: str = "*/5 * * * *",
    schedule_canonical: str | None = "*/5 * * * *",
    cadence_seconds: int = 0,
    source_path: str | None = "/etc/crontab",
    last_seen_state: str = "unknown",
    hidden_at: str | None = None,
    fingerprint: str | None = None,
) -> str:
    """Insert a cron with a computed fingerprint (or the caller-supplied one).

    Returns the fingerprint so tests can use it for follow-up assertions.
    """
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
                "source_path, wrapper_installed_at) VALUES ("
                ":fp, :name, :host, :command, :schedule, :sched_canon, :cad, "
                ":grace, :enabled, :state, :created, :updated, :hidden, :sp, :wia)"
            ),
            {
                "fp": fp,
                "name": name,
                "host": host,
                "command": command,
                "schedule": schedule if schedule else None,
                "sched_canon": schedule_canonical,
                "cad": cadence_seconds,
                "grace": 300,
                "enabled": 1,
                "state": last_seen_state,
                "created": now,
                "updated": now,
                "hidden": hidden_at,
                "sp": source_path,
                "wia": None,
            },
        )
    return fp


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
    await _seed_cron(repo, name="alpha")
    await _seed_cron(repo, name="beta", host="host-b")
    resp = await authenticated_client.get("/api/crons")
    assert resp.status_code == 200  # noqa: PLR2004
    body = resp.json()
    assert body["total"] == 2  # noqa: PLR2004
    names = [it["name"] for it in body["items"]]
    assert names == ["alpha", "beta"]  # ORDER BY name ASC


@pytest.mark.asyncio
async def test_list_pagination(authenticated_client: AsyncClient, repo: SqliteRepository) -> None:
    for i in range(5):
        await _seed_cron(repo, name=f"cron-{i:02d}")
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
    await _seed_cron(repo, name="a", host="host-a")
    await _seed_cron(repo, name="b", host="host-b")
    resp = await authenticated_client.get("/api/crons?host=host-b")
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["host"] == "host-b"


@pytest.mark.asyncio
async def test_list_search_q_matches_name_case_insensitive(
    authenticated_client: AsyncClient, repo: SqliteRepository
) -> None:
    await _seed_cron(repo, name="DailyBackup")
    await _seed_cron(repo, name="weekly")
    resp = await authenticated_client.get("/api/crons?q=daily")
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["name"] == "DailyBackup"


@pytest.mark.asyncio
async def test_list_search_q_matches_command(
    authenticated_client: AsyncClient, repo: SqliteRepository
) -> None:
    await _seed_cron(repo, name="alpha", command="/opt/scripts/backup.sh")
    await _seed_cron(repo, name="beta", command="/usr/bin/true")
    resp = await authenticated_client.get("/api/crons?q=backup")
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["name"] == "alpha"


@pytest.mark.asyncio
async def test_list_excludes_hidden_by_default(
    authenticated_client: AsyncClient, repo: SqliteRepository
) -> None:
    await _seed_cron(repo, name="active")
    await _seed_cron(repo, name="hidden", hidden_at=utc_now_iso())
    resp = await authenticated_client.get("/api/crons")
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["name"] == "active"


@pytest.mark.asyncio
async def test_list_includes_hidden_with_flag(
    authenticated_client: AsyncClient, repo: SqliteRepository
) -> None:
    await _seed_cron(repo, name="active")
    await _seed_cron(repo, name="hidden", hidden_at=utc_now_iso())
    resp = await authenticated_client.get("/api/crons?include_hidden=true")
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
    resp = await authenticated_client.get("/api/crons/no-such-fingerprint")
    assert resp.status_code == 404  # noqa: PLR2004


@pytest.mark.asyncio
async def test_get_returns_cron_with_state(
    authenticated_client: AsyncClient, repo: SqliteRepository
) -> None:
    fp = await _seed_cron(repo, name="alpha")
    resp = await authenticated_client.get(f"/api/crons/{fp}")
    assert resp.status_code == 200  # noqa: PLR2004
    body = resp.json()
    assert body["cron"]["fingerprint"] == fp
    assert body["state"] is None  # no heartbeats yet


@pytest.mark.asyncio
async def test_get_hidden_returns_404_by_default(
    authenticated_client: AsyncClient, repo: SqliteRepository
) -> None:
    fp = await _seed_cron(repo, name="hidden", hidden_at=utc_now_iso())
    resp = await authenticated_client.get(f"/api/crons/{fp}")
    assert resp.status_code == 404  # noqa: PLR2004


@pytest.mark.asyncio
async def test_get_hidden_returns_with_include_hidden(
    authenticated_client: AsyncClient, repo: SqliteRepository
) -> None:
    fp = await _seed_cron(repo, name="hidden", hidden_at=utc_now_iso())
    resp = await authenticated_client.get(f"/api/crons/{fp}?include_hidden=true")
    assert resp.status_code == 200  # noqa: PLR2004
    body = resp.json()
    assert body["cron"]["hidden_at"] is not None


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
        "source_path": "/etc/crontab",
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
    fp = body["fingerprint"]
    row = await repo.fetch_one(text("SELECT name FROM crons WHERE fingerprint = :fp"), {"fp": fp})
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
    # fingerprint is 64-char hex
    assert len(body["fingerprint"]) == 64  # noqa: PLR2004
    assert all(c in "0123456789abcdef" for c in body["fingerprint"])


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
    assert "fingerprint" in after


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


@pytest.mark.asyncio
async def test_create_duplicate_fingerprint_returns_409(
    authenticated_client: AsyncClient,
) -> None:
    payload = {
        "name": "dup",
        "host": "h",
        "command": "/x",
        "schedule": "* * * * *",
        "source_path": "/etc/crontab",
    }
    first = await authenticated_client.post(
        "/api/crons",
        json=payload,
        headers=_csrf(authenticated_client),
    )
    assert first.status_code == 201  # noqa: PLR2004
    second = await authenticated_client.post(
        "/api/crons",
        json=payload,
        headers=_csrf(authenticated_client),
    )
    assert second.status_code == 409  # noqa: PLR2004


# ---------------------------------------------------------------------------
# PATCH
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_patch_updates_grace_seconds_and_audits_changed_fields_only(
    authenticated_client: AsyncClient, repo: SqliteRepository
) -> None:
    fp = await _seed_cron(repo, name="p1")
    resp = await authenticated_client.patch(
        f"/api/crons/{fp}",
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
    fp = await _seed_cron(repo, name="p1")
    # PATCH with the SAME grace value → no diff → no audit row
    resp = await authenticated_client.patch(
        f"/api/crons/{fp}",
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
async def test_patch_rejects_host_field_with_422(
    authenticated_client: AsyncClient, repo: SqliteRepository
) -> None:
    """PATCH with read-only ``host`` field returns 422 (extra='forbid')."""
    fp = await _seed_cron(repo, name="c")
    resp = await authenticated_client.patch(
        f"/api/crons/{fp}",
        json={"host": "new-host"},
        headers=_csrf(authenticated_client),
    )
    assert resp.status_code == 422  # noqa: PLR2004


@pytest.mark.asyncio
async def test_patch_rejects_command_field_with_422(
    authenticated_client: AsyncClient, repo: SqliteRepository
) -> None:
    fp = await _seed_cron(repo, name="c")
    resp = await authenticated_client.patch(
        f"/api/crons/{fp}",
        json={"command": "/new/cmd"},
        headers=_csrf(authenticated_client),
    )
    assert resp.status_code == 422  # noqa: PLR2004


@pytest.mark.asyncio
async def test_patch_rejects_schedule_field_with_422(
    authenticated_client: AsyncClient, repo: SqliteRepository
) -> None:
    fp = await _seed_cron(repo, name="c")
    resp = await authenticated_client.patch(
        f"/api/crons/{fp}",
        json={"schedule": "*/10 * * * *"},
        headers=_csrf(authenticated_client),
    )
    assert resp.status_code == 422  # noqa: PLR2004


@pytest.mark.asyncio
async def test_patch_hidden_at_emits_crons_hide_audit(
    authenticated_client: AsyncClient, repo: SqliteRepository
) -> None:
    fp = await _seed_cron(repo, name="to-hide")
    resp = await authenticated_client.patch(
        f"/api/crons/{fp}",
        json={"hidden_at": utc_now_iso()},
        headers=_csrf(authenticated_client),
    )
    assert resp.status_code == 200  # noqa: PLR2004

    row = await repo.fetch_one(text('SELECT what FROM audit_log ORDER BY "when" DESC LIMIT 1'))
    assert row is not None
    assert row[0] == "crons.hide"


@pytest.mark.asyncio
async def test_patch_unhide_emits_crons_unhide_audit(
    authenticated_client: AsyncClient, repo: SqliteRepository
) -> None:
    fp = await _seed_cron(repo, name="to-unhide", hidden_at=utc_now_iso())
    resp = await authenticated_client.patch(
        f"/api/crons/{fp}",
        json={"hidden_at": None},
        headers=_csrf(authenticated_client),
    )
    assert resp.status_code == 200  # noqa: PLR2004

    row = await repo.fetch_one(text('SELECT what FROM audit_log ORDER BY "when" DESC LIMIT 1'))
    assert row is not None
    assert row[0] == "crons.unhide"


# ---------------------------------------------------------------------------
# DELETE (soft)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_hides_and_emits_crons_hide_audit(
    authenticated_client: AsyncClient, repo: SqliteRepository
) -> None:
    fp = await _seed_cron(repo, name="to-hide")
    resp = await authenticated_client.delete(
        f"/api/crons/{fp}", headers=_csrf(authenticated_client)
    )
    assert resp.status_code == 204  # noqa: PLR2004

    row = await repo.fetch_one(
        text("SELECT hidden_at FROM crons WHERE fingerprint = :fp"),
        {"fp": fp},
    )
    assert row is not None
    assert row[0] is not None  # hidden_at set

    audit = await repo.fetch_one(text('SELECT what FROM audit_log ORDER BY "when" DESC LIMIT 1'))
    assert audit is not None
    assert audit[0] == "crons.hide"


@pytest.mark.asyncio
async def test_delete_unknown_returns_404(authenticated_client: AsyncClient) -> None:
    resp = await authenticated_client.delete(
        "/api/crons/no-such-fingerprint", headers=_csrf(authenticated_client)
    )
    assert resp.status_code == 404  # noqa: PLR2004


@pytest.mark.asyncio
async def test_delete_already_hidden_returns_404(
    authenticated_client: AsyncClient, repo: SqliteRepository
) -> None:
    fp = await _seed_cron(repo, name="x", hidden_at=utc_now_iso())
    resp = await authenticated_client.delete(
        f"/api/crons/{fp}", headers=_csrf(authenticated_client)
    )
    assert resp.status_code == 404  # noqa: PLR2004


# ---------------------------------------------------------------------------
# PREVIEW RUNS
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_preview_runs_for_saved_cron(
    authenticated_client: AsyncClient, repo: SqliteRepository
) -> None:
    fp = await _seed_cron(repo, name="preview", schedule="0 * * * *")
    resp = await authenticated_client.get(f"/api/crons/{fp}/preview-runs?count=3")
    assert resp.status_code == 200  # noqa: PLR2004
    body = resp.json()
    assert len(body["runs"]) == 3  # noqa: PLR2004


@pytest.mark.asyncio
async def test_get_preview_runs_for_cadence_only_cron_returns_404(
    authenticated_client: AsyncClient, repo: SqliteRepository
) -> None:
    fp = await _seed_cron(
        repo,
        name="cadence-only",
        schedule="",
        schedule_canonical=None,
        cadence_seconds=60,
    )
    resp = await authenticated_client.get(f"/api/crons/{fp}/preview-runs")
    assert resp.status_code == 404  # noqa: PLR2004


@pytest.mark.asyncio
async def test_get_preview_runs_unknown_cron_returns_404(
    authenticated_client: AsyncClient,
) -> None:
    resp = await authenticated_client.get("/api/crons/no-such-fp/preview-runs")
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
        },
        headers=csrf,
    )
    assert resp.status_code == 422  # noqa: PLR2004


@pytest.mark.asyncio
async def test_create_rejects_empty_source_path_with_422(
    authenticated_client: AsyncClient,
) -> None:
    """POST /api/crons with source_path='' returns 422 (CronCreate min_length=1)."""
    csrf = _csrf(authenticated_client)
    resp = await authenticated_client.post(
        "/api/crons",
        json={
            "host": "host-a",
            "name": "test",
            "command": "/bin/true",
            "schedule": "*/5 * * * *",
            "cadence_seconds": 0,
            "expected_grace_seconds": 300,
            "enabled": True,
            "source_path": "",
        },
        headers=csrf,
    )
    assert resp.status_code == 422  # noqa: PLR2004
    body = resp.json()
    assert "source_path" in str(body)


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
    """PATCH /api/crons/{unknown-fingerprint} returns 404."""
    csrf = _csrf(authenticated_client)
    resp = await authenticated_client.patch(
        "/api/crons/no" + "a" * 62,
        json={"expected_grace_seconds": 600},
        headers=csrf,
    )
    assert resp.status_code == 404  # noqa: PLR2004
