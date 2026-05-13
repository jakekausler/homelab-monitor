"""Tests for POST /api/hb/{fingerprint}/register — wrapper handshake endpoint.

Project test conventions discovered:
- Framework: pytest-asyncio with @pytest.mark.asyncio
- Client: httpx AsyncClient via ASGITransport (api_token_client fixture from conftest)
- DB access: SqliteRepository (repo fixture from conftest) via raw SQL + text()
- Rate limiter: swap router_mod.cron_rate_limiter with CronRateLimiter(capacity=1, ...)
- Audit assertions: query audit_log via repo.fetch_one() with text()
- Token: "test-token" (set in api_token_client fixture)
- Wrong-scope tests: spin own app + monkeypatch env (self-contained pattern)
- No-auth tests: clear cookies on authenticated_client, or omit Authorization header
"""

from __future__ import annotations

import base64
import json
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from homelab_monitor.kernel.cron.fingerprint import compute_fingerprint
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.db.time import utc_now_iso
from homelab_monitor.kernel.heartbeat.rate_limiter import cron_rate_limiter

# ---------------------------------------------------------------------------
# Constants shared across all tests
# ---------------------------------------------------------------------------

_VALID_BODY: dict[str, Any] = {
    "host": "test-host",
    "source_path": "/etc/crontab",
    "schedule": "*/5 * * * *",
    "command": "/usr/bin/true",
    "wrapper": False,
}

_VALID_FP = compute_fingerprint(
    host="test-host",
    source_path="/etc/crontab",
    schedule="*/5 * * * *",
    command="/usr/bin/true",
)

# ---------------------------------------------------------------------------
# Test-local helpers
# ---------------------------------------------------------------------------


async def _insert_cron(  # noqa: PLR0913
    repo: SqliteRepository,
    *,
    fingerprint: str | None = None,
    name: str = "test-cron",
    host: str = "test-host",
    schedule: str = "*/5 * * * *",
    cadence_seconds: int = 300,
    command_str: str = "/usr/bin/true",
    source_path: str | None = "/etc/crontab",
    expected_grace_seconds: int = 300,
    enabled: int = 1,
    hidden_at: str | None = None,
    wrapper_last_seen_at: str | None = None,
) -> str:
    """Insert a cron row directly via raw SQL. Returns the fingerprint."""
    fp = fingerprint or compute_fingerprint(
        host=host, source_path=source_path, schedule=schedule, command=command_str
    )
    now = utc_now_iso()
    async with repo.engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO crons (fingerprint, name, host, command, schedule, "
                "schedule_canonical, cadence_seconds, expected_grace_seconds, "
                "enabled, last_seen_state, created_at, updated_at, hidden_at, "
                "source_path, wrapper_last_seen_at) VALUES ("
                ":fp, :name, :host, :command, :schedule, :sched_canon, :cadence, "
                ":grace, :enabled, :last_seen, :created, :updated, :hidden, :sp, :wlsa)"
            ),
            {
                "fp": fp,
                "name": name,
                "host": host,
                "command": command_str,
                "schedule": schedule,
                "sched_canon": schedule,
                "cadence": cadence_seconds,
                "grace": expected_grace_seconds,
                "enabled": enabled,
                "last_seen": "unknown",
                "created": now,
                "updated": now,
                "hidden": hidden_at,
                "sp": source_path,
                "wlsa": wrapper_last_seen_at,
            },
        )
    return fp


async def _audit_count_for_register(repo: SqliteRepository) -> int:
    row = await repo.fetch_one(text("SELECT COUNT(*) FROM audit_log WHERE what = 'crons.register'"))
    return int(row[0]) if row is not None else 0


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_rate_limiter() -> None:  # pyright: ignore[reportUnusedFunction]
    """Wipe the module-level rate limiter between tests for isolation."""
    cron_rate_limiter.reset()


# ---------------------------------------------------------------------------
# 1. Creates new cron — 201
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_creates_new_cron_returns_201(
    api_token_client: AsyncClient,
    repo: SqliteRepository,
) -> None:
    resp = await api_token_client.post(
        f"/api/hb/{_VALID_FP}/register",
        json=_VALID_BODY,
    )
    assert resp.status_code == 201  # noqa: PLR2004

    body = resp.json()
    assert body["fingerprint"] == _VALID_FP
    assert body["host"] == "test-host"
    assert body["command"] == "/usr/bin/true"
    assert body["schedule"] == "*/5 * * * *"
    assert body["name"] == "true"
    assert body["expected_grace_seconds"] == 300  # noqa: PLR2004
    assert body["enabled"] is True
    assert body["hidden_at"] is None
    assert body["source_path"] == "/etc/crontab"
    assert body["wrapper_last_seen_at"] is None
    assert body["cadence_seconds"] == 300  # noqa: PLR2004

    # DB: 1 cron row
    row = await repo.fetch_one(
        text("SELECT fingerprint FROM crons WHERE fingerprint = :fp"),
        {"fp": _VALID_FP},
    )
    assert row is not None

    # Audit row: what=crons.register, who=test-token, before=NULL, after contains fingerprint
    audit_row = await repo.fetch_one(
        text(
            "SELECT who, what, before_json, after_json "
            "FROM audit_log WHERE what = 'crons.register' "
            'ORDER BY "when" DESC LIMIT 1'
        )
    )
    assert audit_row is not None
    assert audit_row[0] == "test-token"
    assert audit_row[1] == "crons.register"
    assert audit_row[2] is None  # before_json IS NULL for first insert
    after = json.loads(audit_row[3])
    assert after["fingerprint"] == _VALID_FP


# ---------------------------------------------------------------------------
# 2. wrapper=True sets wrapper_last_seen_at
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_with_wrapper_true_sets_wrapper_last_seen_at(
    api_token_client: AsyncClient,
    repo: SqliteRepository,
) -> None:
    body = {**_VALID_BODY, "wrapper": True}
    resp = await api_token_client.post(f"/api/hb/{_VALID_FP}/register", json=body)
    assert resp.status_code == 201  # noqa: PLR2004

    response_body = resp.json()
    assert response_body["wrapper_last_seen_at"] is not None

    db_row = await repo.fetch_one(
        text("SELECT wrapper_last_seen_at FROM crons WHERE fingerprint = :fp"),
        {"fp": _VALID_FP},
    )
    assert db_row is not None
    assert db_row[0] is not None


# ---------------------------------------------------------------------------
# 3. wrapper=False leaves wrapper_last_seen_at null
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_without_wrapper_leaves_wrapper_last_seen_at_null(
    api_token_client: AsyncClient,
    repo: SqliteRepository,
) -> None:
    resp = await api_token_client.post(f"/api/hb/{_VALID_FP}/register", json=_VALID_BODY)
    assert resp.status_code == 201  # noqa: PLR2004

    body = resp.json()
    assert body["wrapper_last_seen_at"] is None

    db_row = await repo.fetch_one(
        text("SELECT wrapper_last_seen_at FROM crons WHERE fingerprint = :fp"),
        {"fp": _VALID_FP},
    )
    assert db_row is not None
    assert db_row[0] is None


# ---------------------------------------------------------------------------
# 4. Idempotent wrapper=False re-register → 200, no new audit row
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_idempotent_no_state_change_returns_200_no_audit(
    api_token_client: AsyncClient,
    repo: SqliteRepository,
) -> None:
    # First register
    r1 = await api_token_client.post(f"/api/hb/{_VALID_FP}/register", json=_VALID_BODY)
    assert r1.status_code == 201  # noqa: PLR2004
    first_body = r1.json()

    audit_after_first = await _audit_count_for_register(repo)
    assert audit_after_first == 1

    # Second register — identical body, wrapper=False
    r2 = await api_token_client.post(f"/api/hb/{_VALID_FP}/register", json=_VALID_BODY)
    assert r2.status_code == 200  # noqa: PLR2004
    second_body = r2.json()

    # created_at and updated_at must not change on true no-op
    assert second_body["created_at"] == first_body["created_at"]
    assert second_body["updated_at"] == first_body["updated_at"]

    # Still exactly 1 audit row (no second row written)
    audit_after_second = await _audit_count_for_register(repo)
    assert audit_after_second == 1


# ---------------------------------------------------------------------------
# 5. wrapper=True re-register → 200, wrapper_last_seen_at refreshed, new audit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_with_wrapper_refresh_emits_audit(
    api_token_client: AsyncClient,
    repo: SqliteRepository,
) -> None:
    wrapper_body = {**_VALID_BODY, "wrapper": True}

    # First register with wrapper=True
    r1 = await api_token_client.post(f"/api/hb/{_VALID_FP}/register", json=wrapper_body)
    assert r1.status_code == 201  # noqa: PLR2004
    ts1 = r1.json()["wrapper_last_seen_at"]
    assert ts1 is not None

    # Second register with wrapper=True
    r2 = await api_token_client.post(f"/api/hb/{_VALID_FP}/register", json=wrapper_body)
    assert r2.status_code == 200  # noqa: PLR2004
    ts2 = r2.json()["wrapper_last_seen_at"]
    assert ts2 is not None
    assert ts2 >= ts1

    # Exactly 2 audit rows
    assert await _audit_count_for_register(repo) == 2  # noqa: PLR2004

    # Second audit row: before=ts1, after=ts2
    second_audit = await repo.fetch_one(
        text(
            "SELECT before_json, after_json FROM audit_log WHERE what = 'crons.register' "
            'ORDER BY "when" DESC LIMIT 1'
        )
    )
    assert second_audit is not None
    before = json.loads(second_audit[0])
    after = json.loads(second_audit[1])
    assert before["wrapper_last_seen_at"] == ts1
    assert after["wrapper_last_seen_at"] == ts2


# ---------------------------------------------------------------------------
# 6. First wrapper install on a discovered row (wrapper_last_seen_at was NULL)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_first_wrapper_install_on_discovered_row_emits_audit(
    api_token_client: AsyncClient,
    repo: SqliteRepository,
) -> None:
    # Seed the row with wrapper_last_seen_at=NULL (simulates discovery)
    await _insert_cron(repo, wrapper_last_seen_at=None)

    wrapper_body = {**_VALID_BODY, "wrapper": True}
    resp = await api_token_client.post(f"/api/hb/{_VALID_FP}/register", json=wrapper_body)
    assert resp.status_code == 200  # noqa: PLR2004

    body = resp.json()
    assert body["wrapper_last_seen_at"] is not None

    # 1 audit row written (NULL → ts transition)
    assert await _audit_count_for_register(repo) == 1

    audit_row = await repo.fetch_one(
        text(
            "SELECT before_json, after_json FROM audit_log WHERE what = 'crons.register' "
            'ORDER BY "when" DESC LIMIT 1'
        )
    )
    assert audit_row is not None
    before = json.loads(audit_row[0])
    after = json.loads(audit_row[1])
    assert before["wrapper_last_seen_at"] is None
    assert after["wrapper_last_seen_at"] is not None


# ---------------------------------------------------------------------------
# 7. Fingerprint mismatch → 422 with fingerprint_mismatch flag
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_fingerprint_mismatch_returns_422(
    api_token_client: AsyncClient,
    repo: SqliteRepository,
) -> None:
    bad_fp = "deadbeef" * 8
    resp = await api_token_client.post(f"/api/hb/{bad_fp}/register", json=_VALID_BODY)
    assert resp.status_code == 422  # noqa: PLR2004

    body = resp.json()
    assert body["error"]["details"]["fingerprint_mismatch"] is True

    # No audit row written
    assert await _audit_count_for_register(repo) == 0


# ---------------------------------------------------------------------------
# 8. Invalid schedule → 422 with invalid_schedule flag
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_invalid_schedule_returns_422(
    api_token_client: AsyncClient,
    repo: SqliteRepository,
) -> None:
    bad_schedule_body = {**_VALID_BODY, "schedule": "not a cron"}
    # Recompute fingerprint so step-1 check doesn't trip
    bad_fp = compute_fingerprint(
        host=bad_schedule_body["host"],
        source_path=bad_schedule_body["source_path"],
        schedule=bad_schedule_body["schedule"],
        command=bad_schedule_body["command"],
    )
    resp = await api_token_client.post(f"/api/hb/{bad_fp}/register", json=bad_schedule_body)
    assert resp.status_code == 422  # noqa: PLR2004

    body = resp.json()
    assert body["error"]["details"]["invalid_schedule"] is True
    assert isinstance(body["error"]["details"]["reason"], str)
    assert len(body["error"]["details"]["reason"]) > 0


# ---------------------------------------------------------------------------
# 9. Extra field → 422 (Pydantic extra='forbid')
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_extra_field_returns_422(
    api_token_client: AsyncClient,
) -> None:
    body_with_extra = {**_VALID_BODY, "foo": "bar"}
    resp = await api_token_client.post(f"/api/hb/{_VALID_FP}/register", json=body_with_extra)
    assert resp.status_code == 422  # noqa: PLR2004


# ---------------------------------------------------------------------------
# 10. Missing required field → 422 (parametrized)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_field", ["host", "schedule", "command"])
async def test_register_missing_required_field_returns_422(
    api_token_client: AsyncClient,
    missing_field: str,
) -> None:
    body_missing = {k: v for k, v in _VALID_BODY.items() if k != missing_field}
    resp = await api_token_client.post(f"/api/hb/{_VALID_FP}/register", json=body_missing)
    assert resp.status_code == 422  # noqa: PLR2004

    body = resp.json()
    errors = body["detail"]
    assert any(missing_field in err.get("loc", []) for err in errors)


# ---------------------------------------------------------------------------
# 11. No token → 401
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_no_token_returns_401(
    authenticated_client: AsyncClient,
) -> None:
    authenticated_client.cookies.clear()
    # Also strip the Authorization header if present
    client_headers = dict(authenticated_client.headers)
    client_headers.pop("authorization", None)
    resp = await authenticated_client.post(
        f"/api/hb/{_VALID_FP}/register",
        json=_VALID_BODY,
        headers={k: v for k, v in client_headers.items() if k.lower() != "authorization"},
    )
    assert resp.status_code == 401  # noqa: PLR2004


# ---------------------------------------------------------------------------
# 12. Wrong scope → 403
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_wrong_scope_returns_403(
    db_url: str,
    master_key: bytes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A token without HEARTBEAT_WRITE scope returns 403.

    Self-contained: spins its own app + seeds a cron via the repo so the test
    is independent of the api_token_client fixture.
    """
    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    monkeypatch.setenv("HOMELAB_MONITOR_MASTER_KEY", base64.b64encode(master_key).decode())
    monkeypatch.setenv("HOMELAB_MONITOR_ALERTMANAGER_URL", "disabled")

    from homelab_monitor.kernel.api.app import create_app  # noqa: PLC0415
    from homelab_monitor.kernel.auth.api_tokens import make_api_token  # noqa: PLC0415
    from homelab_monitor.kernel.auth.scopes import Scope  # noqa: PLC0415

    app = create_app(lifespan_enabled=True)
    async with app.router.lifespan_context(app):
        plaintext, _ = make_api_token(prefix="test")
        await app.state.auth_repo.create_api_token(
            name="no-hb-token",
            scopes={Scope.READ_STATUS},
            plaintext_token=plaintext,
        )
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": f"Bearer {plaintext}"},
        ) as client:
            resp = await client.post(f"/api/hb/{_VALID_FP}/register", json=_VALID_BODY)
            assert resp.status_code == 403  # noqa: PLR2004


# ---------------------------------------------------------------------------
# 13. Rate limited → 429
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_rate_limited_returns_429(
    api_token_client: AsyncClient,
) -> None:
    """Swap in a capacity-1 limiter; first call succeeds, second → 429."""
    import homelab_monitor.kernel.api.routers.heartbeat as router_mod  # noqa: PLC0415
    from homelab_monitor.kernel.heartbeat import rate_limiter as rl_module  # noqa: PLC0415

    tiny = rl_module.CronRateLimiter(capacity=1, refill_per_second=1.0)
    original = router_mod.cron_rate_limiter
    router_mod.cron_rate_limiter = tiny
    try:
        r1 = await api_token_client.post(f"/api/hb/{_VALID_FP}/register", json=_VALID_BODY)
        assert r1.status_code == 201  # noqa: PLR2004
        r2 = await api_token_client.post(f"/api/hb/{_VALID_FP}/register", json=_VALID_BODY)
        assert r2.status_code == 429  # noqa: PLR2004
        assert "Retry-After" in r2.headers
        assert int(r2.headers["Retry-After"]) >= 1
    finally:
        router_mod.cron_rate_limiter = original


# ---------------------------------------------------------------------------
# 14. Hidden cron operates normally
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_hidden_cron_operates_normally(
    api_token_client: AsyncClient,
    repo: SqliteRepository,
) -> None:
    """hidden_at is display/notification suppression only; /register works normally."""
    hidden_ts = utc_now_iso()
    await _insert_cron(repo, hidden_at=hidden_ts, wrapper_last_seen_at=None)

    wrapper_body = {**_VALID_BODY, "wrapper": True}
    resp = await api_token_client.post(f"/api/hb/{_VALID_FP}/register", json=wrapper_body)
    assert resp.status_code == 200  # noqa: PLR2004

    response_body = resp.json()
    assert response_body["wrapper_last_seen_at"] is not None

    # hidden_at must NOT be cleared by re-register
    db_row = await repo.fetch_one(
        text("SELECT hidden_at FROM crons WHERE fingerprint = :fp"),
        {"fp": _VALID_FP},
    )
    assert db_row is not None
    assert db_row[0] == hidden_ts

    # Audit row written (NULL → ts transition)
    assert await _audit_count_for_register(repo) == 1


# ---------------------------------------------------------------------------
# 15. Re-register does not overwrite operator-edited name
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_does_not_overwrite_name_on_existing_row(
    api_token_client: AsyncClient,
    repo: SqliteRepository,
) -> None:
    # Seed with a custom operator-edited name
    await _insert_cron(repo, name="operator-edited-name")

    resp = await api_token_client.post(f"/api/hb/{_VALID_FP}/register", json=_VALID_BODY)
    assert resp.status_code == 200  # noqa: PLR2004

    body = resp.json()
    assert body["name"] == "operator-edited-name"


# ---------------------------------------------------------------------------
# 16. Re-register does not overwrite expected_grace_seconds
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_does_not_overwrite_expected_grace_seconds_on_existing_row(
    api_token_client: AsyncClient,
    repo: SqliteRepository,
) -> None:
    await _insert_cron(repo, expected_grace_seconds=900)

    wrapper_body = {**_VALID_BODY, "wrapper": True}
    resp = await api_token_client.post(f"/api/hb/{_VALID_FP}/register", json=wrapper_body)
    assert resp.status_code == 200  # noqa: PLR2004

    body = resp.json()
    assert body["expected_grace_seconds"] == 900  # noqa: PLR2004


# ---------------------------------------------------------------------------
# 17. Re-register does not overwrite enabled=False
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_does_not_overwrite_enabled_on_existing_row(
    api_token_client: AsyncClient,
    repo: SqliteRepository,
) -> None:
    await _insert_cron(repo, enabled=0)

    wrapper_body = {**_VALID_BODY, "wrapper": True}
    resp = await api_token_client.post(f"/api/hb/{_VALID_FP}/register", json=wrapper_body)
    assert resp.status_code == 200  # noqa: PLR2004

    body = resp.json()
    assert body["enabled"] is False


# ---------------------------------------------------------------------------
# 18. Re-register does not overwrite hidden_at
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_does_not_overwrite_hidden_at_on_existing_row(
    api_token_client: AsyncClient,
    repo: SqliteRepository,
) -> None:
    hidden_ts = "2026-05-01T00:00:00+00:00"
    await _insert_cron(repo, hidden_at=hidden_ts)

    wrapper_body = {**_VALID_BODY, "wrapper": True}
    resp = await api_token_client.post(f"/api/hb/{_VALID_FP}/register", json=wrapper_body)
    assert resp.status_code == 200  # noqa: PLR2004

    body = resp.json()
    assert body["hidden_at"] == hidden_ts


# ---------------------------------------------------------------------------
# 19. Audit who attribution = token name
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_emits_correct_audit_who_attribution(
    api_token_client: AsyncClient,
    repo: SqliteRepository,
) -> None:
    resp = await api_token_client.post(f"/api/hb/{_VALID_FP}/register", json=_VALID_BODY)
    assert resp.status_code == 201  # noqa: PLR2004

    audit_row = await repo.fetch_one(
        text(
            "SELECT who FROM audit_log WHERE what = 'crons.register' ORDER BY \"when\" DESC LIMIT 1"
        )
    )
    assert audit_row is not None
    assert audit_row[0] == "test-token"


# ---------------------------------------------------------------------------
# 20. source_path=None (remote cron) succeeds
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_with_source_path_none_succeeds(
    api_token_client: AsyncClient,
    repo: SqliteRepository,
) -> None:
    null_sp_body: dict[str, Any] = {
        "host": "test-host",
        "source_path": None,
        "schedule": "*/5 * * * *",
        "command": "/usr/bin/true",
        "wrapper": False,
    }
    fp = compute_fingerprint(
        host=null_sp_body["host"],
        source_path=null_sp_body["source_path"],
        schedule=null_sp_body["schedule"],
        command=null_sp_body["command"],
    )
    resp = await api_token_client.post(f"/api/hb/{fp}/register", json=null_sp_body)
    assert resp.status_code == 201  # noqa: PLR2004

    body = resp.json()
    assert body["source_path"] is None

    db_row = await repo.fetch_one(
        text("SELECT source_path FROM crons WHERE fingerprint = :fp"),
        {"fp": fp},
    )
    assert db_row is not None
    assert db_row[0] is None
