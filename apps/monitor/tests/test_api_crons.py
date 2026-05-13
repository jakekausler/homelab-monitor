"""Session-auth route tests for /api/crons CRUD + preview-runs."""

from __future__ import annotations

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
                "source_path, wrapper_last_seen_at) VALUES ("
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


@pytest.mark.asyncio
async def test_post_crons_returns_405_method_not_allowed(
    authenticated_client: AsyncClient,
) -> None:
    """POST /api/crons returns 405 with `Allow: GET` (STAGE-002-004: manual create removed)."""
    csrf = _csrf(authenticated_client)
    response = await authenticated_client.post(
        "/api/crons",
        json={},
        headers=csrf,
    )
    assert response.status_code == 405  # noqa: PLR2004
    allow_header = response.headers.get("allow", "").upper()
    assert "GET" in allow_header


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


# ---------------------------------------------------------------------------
# LIST — wrapper_installed filter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_crons_filters_wrapper_installed_true(
    authenticated_client: AsyncClient, repo: SqliteRepository
) -> None:
    """GET /api/crons?wrapper_installed=true returns only crons with wrapper_last_seen_at set."""
    # 2 crons without wrapper (wia=None via _seed_cron default)
    await _seed_cron(repo, name="no-wrap-a", command="/bin/no-wrap-a")
    await _seed_cron(repo, name="no-wrap-b", command="/bin/no-wrap-b")
    # 2 crons with wrapper — insert directly with wrapper_last_seen_at set
    now = utc_now_iso()
    async with repo.engine.begin() as conn:
        for suffix in ("c", "d"):
            fp = compute_fingerprint(
                host="host-a",
                source_path="/etc/crontab",
                schedule="*/5 * * * *",
                command=f"/bin/wrap-{suffix}",
            )
            await conn.execute(
                text(
                    "INSERT INTO crons (fingerprint, name, host, command, schedule, "
                    "schedule_canonical, cadence_seconds, expected_grace_seconds, "
                    "enabled, last_seen_state, created_at, updated_at, hidden_at, "
                    "source_path, wrapper_last_seen_at) VALUES ("
                    ":fp, :name, :host, :command, :schedule, :sched_canon, :cad, "
                    ":grace, :enabled, :state, :created, :updated, :hidden, :sp, :wia)"
                ),
                {
                    "fp": fp,
                    "name": f"wrap-{suffix}",
                    "host": "host-a",
                    "command": f"/bin/wrap-{suffix}",
                    "schedule": "*/5 * * * *",
                    "sched_canon": "*/5 * * * *",
                    "cad": 0,
                    "grace": 300,
                    "enabled": 1,
                    "state": "unknown",
                    "created": now,
                    "updated": now,
                    "hidden": None,
                    "sp": "/etc/crontab",
                    "wia": now,
                },
            )
    resp = await authenticated_client.get("/api/crons?wrapper_installed=true")
    assert resp.status_code == 200  # noqa: PLR2004
    body = resp.json()
    assert body["total"] == 2  # noqa: PLR2004
    names = {it["name"] for it in body["items"]}
    assert names == {"wrap-c", "wrap-d"}


@pytest.mark.asyncio
async def test_list_crons_filters_wrapper_installed_false(
    authenticated_client: AsyncClient, repo: SqliteRepository
) -> None:
    """GET /api/crons?wrapper_installed=false returns only crons without wrapper_last_seen_at."""
    await _seed_cron(repo, name="no-wrap-a", command="/bin/no-wrap-a")
    await _seed_cron(repo, name="no-wrap-b", command="/bin/no-wrap-b")
    now = utc_now_iso()
    async with repo.engine.begin() as conn:
        for suffix in ("c", "d"):
            fp = compute_fingerprint(
                host="host-a",
                source_path="/etc/crontab",
                schedule="*/5 * * * *",
                command=f"/bin/wrap-{suffix}",
            )
            await conn.execute(
                text(
                    "INSERT INTO crons (fingerprint, name, host, command, schedule, "
                    "schedule_canonical, cadence_seconds, expected_grace_seconds, "
                    "enabled, last_seen_state, created_at, updated_at, hidden_at, "
                    "source_path, wrapper_last_seen_at) VALUES ("
                    ":fp, :name, :host, :command, :schedule, :sched_canon, :cad, "
                    ":grace, :enabled, :state, :created, :updated, :hidden, :sp, :wia)"
                ),
                {
                    "fp": fp,
                    "name": f"wrap-{suffix}",
                    "host": "host-a",
                    "command": f"/bin/wrap-{suffix}",
                    "schedule": "*/5 * * * *",
                    "sched_canon": "*/5 * * * *",
                    "cad": 0,
                    "grace": 300,
                    "enabled": 1,
                    "state": "unknown",
                    "created": now,
                    "updated": now,
                    "hidden": None,
                    "sp": "/etc/crontab",
                    "wia": now,
                },
            )
    resp = await authenticated_client.get("/api/crons?wrapper_installed=false")
    assert resp.status_code == 200  # noqa: PLR2004
    body = resp.json()
    assert body["total"] == 2  # noqa: PLR2004
    names = {it["name"] for it in body["items"]}
    assert names == {"no-wrap-a", "no-wrap-b"}


@pytest.mark.asyncio
async def test_list_crons_no_wrapper_filter_returns_all(
    authenticated_client: AsyncClient, repo: SqliteRepository
) -> None:
    """GET /api/crons (no wrapper_installed param) returns all 4 crons."""
    await _seed_cron(repo, name="no-wrap-a", command="/bin/no-wrap-a")
    await _seed_cron(repo, name="no-wrap-b", command="/bin/no-wrap-b")
    now = utc_now_iso()
    async with repo.engine.begin() as conn:
        for suffix in ("c", "d"):
            fp = compute_fingerprint(
                host="host-a",
                source_path="/etc/crontab",
                schedule="*/5 * * * *",
                command=f"/bin/wrap-{suffix}",
            )
            await conn.execute(
                text(
                    "INSERT INTO crons (fingerprint, name, host, command, schedule, "
                    "schedule_canonical, cadence_seconds, expected_grace_seconds, "
                    "enabled, last_seen_state, created_at, updated_at, hidden_at, "
                    "source_path, wrapper_last_seen_at) VALUES ("
                    ":fp, :name, :host, :command, :schedule, :sched_canon, :cad, "
                    ":grace, :enabled, :state, :created, :updated, :hidden, :sp, :wia)"
                ),
                {
                    "fp": fp,
                    "name": f"wrap-{suffix}",
                    "host": "host-a",
                    "command": f"/bin/wrap-{suffix}",
                    "schedule": "*/5 * * * *",
                    "sched_canon": "*/5 * * * *",
                    "cad": 0,
                    "grace": 300,
                    "enabled": 1,
                    "state": "unknown",
                    "created": now,
                    "updated": now,
                    "hidden": None,
                    "sp": "/etc/crontab",
                    "wia": now,
                },
            )
    resp = await authenticated_client.get("/api/crons")
    assert resp.status_code == 200  # noqa: PLR2004
    body = resp.json()
    assert body["total"] == 4  # noqa: PLR2004
