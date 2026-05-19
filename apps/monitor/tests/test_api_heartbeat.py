"""Unit tests for /api/hb/{fingerprint}/{start|ok|fail} heartbeat receiver endpoints."""

from __future__ import annotations

import base64
from dataclasses import dataclass

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from homelab_monitor.kernel.cron.fingerprint import compute_fingerprint
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.db.time import utc_now_iso
from homelab_monitor.kernel.heartbeat.rate_limiter import cron_rate_limiter


@dataclass(frozen=True, slots=True)
class _SeededCron:
    fingerprint: str
    name: str
    host: str


@dataclass(frozen=True, slots=True)
class SeededCrons:
    first: _SeededCron
    second: _SeededCron
    third: _SeededCron


async def _insert_cron(  # noqa: PLR0913
    repo: SqliteRepository,
    *,
    fingerprint: str | None = None,
    name: str = "test-cron",
    host: str = "test-host",
    schedule: str = "* * * * *",
    cadence_seconds: int = 60,
    command_str: str = "/usr/bin/true",
    source_path: str | None = "/etc/crontab",
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
                "grace": 300,
                "enabled": 1,
                "last_seen": "unknown",
                "created": now,
                "updated": now,
                "hidden": None,
                "sp": source_path,
                "wlsa": None,
            },
        )
    return fp


@pytest.fixture(autouse=True)
def _reset_rate_limiter() -> None:  # pyright: ignore[reportUnusedFunction]
    """Wipe the module-level rate limiter between tests for isolation."""
    cron_rate_limiter.reset()


@pytest.fixture
async def seeded_crons(
    api_token_client: AsyncClient,
    repo: SqliteRepository,
) -> SeededCrons:
    """Seed three crons with different commands into the api_token_client app."""
    # api_token_client and repo share the same aiosqlite engine via get_engine singleton.
    # Writes via repo land on the same engine the route handler will read from.
    seeds = [
        ("test-observe", "/opt/observe.sh"),
        ("test-heartbeat", "/opt/heartbeat.sh"),
        ("test-both", "/opt/both.sh"),
    ]
    crons: list[_SeededCron] = []
    for name, cmd in seeds:
        fp = await _insert_cron(
            repo,
            name=name,
            host="test-host",
            command_str=cmd,
        )
        crons.append(_SeededCron(fingerprint=fp, name=name, host="test-host"))

    return SeededCrons(first=crons[0], second=crons[1], third=crons[2])


# ----- existing auth tests (rewritten to use seeded_crons) -----


@pytest.mark.asyncio
async def test_heartbeat_returns_401_without_auth(
    authenticated_client: AsyncClient,
    seeded_crons: SeededCrons,
) -> None:
    authenticated_client.cookies.clear()
    resp = await authenticated_client.post(f"/api/hb/{seeded_crons.second.fingerprint}/ok")
    assert resp.status_code == 401  # noqa: PLR2004


@pytest.mark.asyncio
async def test_heartbeat_returns_401_with_session_only(
    authenticated_client: AsyncClient,
    seeded_crons: SeededCrons,
) -> None:
    csrf_cookie = authenticated_client.cookies.get("homelab_monitor_csrf", "")
    headers: dict[str, str] = {}
    if csrf_cookie:
        headers["X-CSRF-Token"] = csrf_cookie
    resp = await authenticated_client.post(
        f"/api/hb/{seeded_crons.second.fingerprint}/ok",
        headers=headers,
    )
    assert resp.status_code == 401  # noqa: PLR2004


@pytest.mark.asyncio
async def test_heartbeat_returns_403_with_wrong_scope_token(
    db_url: str,
    master_key: bytes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A token without HEARTBEAT_WRITE scope returns 403 insufficient_scope.

    Self-contained: spins its own app + seeds a cron via the repo so the test
    is independent of the seeded_crons fixture (which is bound to the
    api_token_client / authenticated_client app instances).
    """
    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    monkeypatch.setenv("HOMELAB_MONITOR_MASTER_KEY", base64.b64encode(master_key).decode())
    monkeypatch.setenv("HOMELAB_MONITOR_ALERTMANAGER_URL", "disabled")

    from homelab_monitor.kernel.api.app import create_app  # noqa: PLC0415
    from homelab_monitor.kernel.auth.api_tokens import make_api_token  # noqa: PLC0415
    from homelab_monitor.kernel.auth.scopes import Scope  # noqa: PLC0415

    app = create_app(lifespan_enabled=True)
    async with app.router.lifespan_context(app):
        fp = await _insert_cron(
            app.state.repo,
            name="t-403",
            host="th",
            command_str="/opt/test.sh",
        )
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
            resp = await client.post(f"/api/hb/{fp}/ok")
            assert resp.status_code == 403  # noqa: PLR2004


# ----- new tests: receiver behavior -----


@pytest.mark.asyncio
async def test_start_returns_204_for_registered_cron(
    api_token_client: AsyncClient, seeded_crons: SeededCrons
) -> None:
    resp = await api_token_client.post(f"/api/hb/{seeded_crons.second.fingerprint}/start")
    assert resp.status_code == 204  # noqa: PLR2004


@pytest.mark.asyncio
async def test_ok_returns_204_for_registered_cron_and_records_state(
    api_token_client: AsyncClient,
    repo: SqliteRepository,
    seeded_crons: SeededCrons,
) -> None:
    resp = await api_token_client.post(f"/api/hb/{seeded_crons.second.fingerprint}/ok")
    assert resp.status_code == 204  # noqa: PLR2004

    row = await repo.fetch_one(
        text(
            "SELECT current_state, current_streak "
            "FROM heartbeats_state WHERE cron_fingerprint = :fp"
        ),
        {"fp": seeded_crons.second.fingerprint},
    )
    assert row is not None
    assert row[0] == "ok"
    assert row[1] == 1


@pytest.mark.asyncio
async def test_fail_returns_204_for_registered_cron_and_records_state(
    api_token_client: AsyncClient,
    repo: SqliteRepository,
    seeded_crons: SeededCrons,
) -> None:
    resp = await api_token_client.post(f"/api/hb/{seeded_crons.second.fingerprint}/fail")
    assert resp.status_code == 204  # noqa: PLR2004

    row = await repo.fetch_one(
        text("SELECT current_state FROM heartbeats_state WHERE cron_fingerprint = :fp"),
        {"fp": seeded_crons.second.fingerprint},
    )
    assert row is not None
    assert row[0] == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("verb", ["start", "ok", "fail"])
async def test_unknown_cron_returns_404(
    api_token_client: AsyncClient,
    seeded_crons: SeededCrons,  # forces fixture creation but uses unknown id
    verb: str,
) -> None:
    resp = await api_token_client.post(f"/api/hb/no-such-cron/{verb}")
    assert resp.status_code == 404  # noqa: PLR2004


@pytest.mark.asyncio
async def test_ok_with_duration_query_param_persists_duration(
    api_token_client: AsyncClient,
    repo: SqliteRepository,
    seeded_crons: SeededCrons,
) -> None:
    resp = await api_token_client.post(
        f"/api/hb/{seeded_crons.second.fingerprint}/ok?duration=12.5",
    )
    assert resp.status_code == 204  # noqa: PLR2004

    row = await repo.fetch_one(
        text("SELECT last_duration_seconds FROM heartbeats_state WHERE cron_fingerprint = :fp"),
        {"fp": seeded_crons.second.fingerprint},
    )
    assert row is not None
    assert float(row[0]) == 12.5  # noqa: PLR2004


@pytest.mark.asyncio
async def test_fail_with_exit_code_query_param_persists_exit_code(
    api_token_client: AsyncClient,
    repo: SqliteRepository,
    seeded_crons: SeededCrons,
) -> None:
    resp = await api_token_client.post(
        f"/api/hb/{seeded_crons.second.fingerprint}/fail?exit_code=42",
    )
    assert resp.status_code == 204  # noqa: PLR2004

    row = await repo.fetch_one(
        text("SELECT last_exit_code FROM heartbeats_state WHERE cron_fingerprint = :fp"),
        {"fp": seeded_crons.second.fingerprint},
    )
    assert row is not None
    assert int(row[0]) == 42  # noqa: PLR2004


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["-1", "999999"])
async def test_ok_with_invalid_duration_returns_422(
    api_token_client: AsyncClient,
    seeded_crons: SeededCrons,
    bad: str,
) -> None:
    resp = await api_token_client.post(
        f"/api/hb/{seeded_crons.second.fingerprint}/ok?duration={bad}",
    )
    assert resp.status_code == 422  # noqa: PLR2004


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["-1", "999"])
async def test_fail_with_invalid_exit_code_returns_422(
    api_token_client: AsyncClient,
    seeded_crons: SeededCrons,
    bad: str,
) -> None:
    resp = await api_token_client.post(
        f"/api/hb/{seeded_crons.second.fingerprint}/fail?exit_code={bad}",
    )
    assert resp.status_code == 422  # noqa: PLR2004


@pytest.mark.asyncio
async def test_start_with_unknown_query_param_returns_422(
    api_token_client: AsyncClient,
    seeded_crons: SeededCrons,
) -> None:
    resp = await api_token_client.post(
        f"/api/hb/{seeded_crons.second.fingerprint}/start?foo=bar",
    )
    assert resp.status_code == 422  # noqa: PLR2004


@pytest.mark.asyncio
async def test_ok_with_unknown_query_param_returns_422(
    api_token_client: AsyncClient,
    seeded_crons: SeededCrons,
) -> None:
    resp = await api_token_client.post(
        f"/api/hb/{seeded_crons.second.fingerprint}/ok?foo=bar",
    )
    assert resp.status_code == 422  # noqa: PLR2004


@pytest.mark.asyncio
async def test_fail_with_unknown_query_param_returns_422(
    api_token_client: AsyncClient,
    seeded_crons: SeededCrons,
) -> None:
    resp = await api_token_client.post(
        f"/api/hb/{seeded_crons.second.fingerprint}/fail?foo=bar",
    )
    assert resp.status_code == 422  # noqa: PLR2004


@pytest.mark.asyncio
async def test_unknown_query_param_returns_structured_validation_error(
    api_token_client: AsyncClient,
    seeded_crons: SeededCrons,
) -> None:
    """The 422 body uses ErrorEnvelope with structured details.errors."""
    resp = await api_token_client.post(
        f"/api/hb/{seeded_crons.second.fingerprint}/start?foo=bar",
    )
    assert resp.status_code == 422  # noqa: PLR2004
    body = resp.json()
    assert body["error"]["code"] == "validation_error"
    assert "errors" in body["error"]["details"]
    assert any(e["type"] == "extra_forbidden" for e in body["error"]["details"]["errors"])


@pytest.mark.asyncio
async def test_ok_increments_streak_on_consecutive_oks(
    api_token_client: AsyncClient,
    repo: SqliteRepository,
    seeded_crons: SeededCrons,
) -> None:
    for _ in range(3):
        r = await api_token_client.post(f"/api/hb/{seeded_crons.second.fingerprint}/ok")
        assert r.status_code == 204  # noqa: PLR2004

    row = await repo.fetch_one(
        text("SELECT current_streak FROM heartbeats_state WHERE cron_fingerprint = :fp"),
        {"fp": seeded_crons.second.fingerprint},
    )
    assert row is not None
    assert int(row[0]) == 3  # noqa: PLR2004


@pytest.mark.asyncio
async def test_state_transition_resets_streak_to_1(
    api_token_client: AsyncClient,
    repo: SqliteRepository,
    seeded_crons: SeededCrons,
) -> None:
    cid = seeded_crons.second.fingerprint
    await api_token_client.post(f"/api/hb/{cid}/ok")
    await api_token_client.post(f"/api/hb/{cid}/ok")
    await api_token_client.post(f"/api/hb/{cid}/fail")

    row = await repo.fetch_one(
        text(
            "SELECT current_state, current_streak "
            "FROM heartbeats_state WHERE cron_fingerprint = :fp"
        ),
        {"fp": cid},
    )
    assert row is not None
    assert row[0] == "failed"
    assert int(row[1]) == 1


@pytest.mark.asyncio
async def test_rate_limit_returns_429_with_retry_after(
    api_token_client: AsyncClient, seeded_crons: SeededCrons
) -> None:
    """Construct a tiny limiter, swap it in, exhaust it, expect 429 + header."""
    from homelab_monitor.kernel.heartbeat import rate_limiter as rl_module  # noqa: PLC0415

    tiny = rl_module.CronRateLimiter(capacity=1, refill_per_second=1.0)
    import homelab_monitor.kernel.api.routers.heartbeat as router_mod  # noqa: PLC0415

    original = router_mod.cron_rate_limiter
    router_mod.cron_rate_limiter = tiny
    try:
        # First request consumes the only token -> 204.
        r1 = await api_token_client.post(f"/api/hb/{seeded_crons.second.fingerprint}/ok")
        assert r1.status_code == 204  # noqa: PLR2004
        # Second request immediately -> 429.
        r2 = await api_token_client.post(f"/api/hb/{seeded_crons.second.fingerprint}/ok")
        assert r2.status_code == 429  # noqa: PLR2004
        assert "Retry-After" in r2.headers
        assert int(r2.headers["Retry-After"]) >= 1
    finally:
        router_mod.cron_rate_limiter = original


@pytest.mark.asyncio
@pytest.mark.parametrize("verb", ["start", "ok", "fail"])
async def test_rate_limit_returns_429_with_retry_after_per_verb(
    api_token_client: AsyncClient, seeded_crons: SeededCrons, verb: str
) -> None:
    """Test that all three verbs (/start, /ok, /fail) return 429 when rate-limited."""
    import homelab_monitor.kernel.api.routers.heartbeat as router_mod  # noqa: PLC0415
    from homelab_monitor.kernel.heartbeat import rate_limiter as rl_module  # noqa: PLC0415

    tiny = rl_module.CronRateLimiter(capacity=1, refill_per_second=1.0)
    original = router_mod.cron_rate_limiter
    router_mod.cron_rate_limiter = tiny
    try:
        # First request consumes the only token -> 204.
        r1 = await api_token_client.post(f"/api/hb/{seeded_crons.second.fingerprint}/{verb}")
        assert r1.status_code == 204  # noqa: PLR2004
        # Second request immediately -> 429.
        r2 = await api_token_client.post(f"/api/hb/{seeded_crons.second.fingerprint}/{verb}")
        assert r2.status_code == 429  # noqa: PLR2004
        assert "Retry-After" in r2.headers
        assert int(r2.headers["Retry-After"]) >= 1
    finally:
        router_mod.cron_rate_limiter = original


@pytest.mark.asyncio
async def test_rate_limiter_does_not_share_buckets_across_crons(
    api_token_client: AsyncClient, seeded_crons: SeededCrons
) -> None:
    """A separate cron has its own bucket; one cron exhausting does not 429 the other."""
    import homelab_monitor.kernel.api.routers.heartbeat as router_mod  # noqa: PLC0415
    from homelab_monitor.kernel.heartbeat import rate_limiter as rl_module  # noqa: PLC0415

    tiny = rl_module.CronRateLimiter(capacity=1, refill_per_second=1.0)
    original = router_mod.cron_rate_limiter
    router_mod.cron_rate_limiter = tiny
    try:
        # Exhaust cron A.
        await api_token_client.post(f"/api/hb/{seeded_crons.second.fingerprint}/ok")
        r_a_2 = await api_token_client.post(f"/api/hb/{seeded_crons.second.fingerprint}/ok")
        assert r_a_2.status_code == 429  # noqa: PLR2004
        # Cron B still has a fresh bucket.
        r_b = await api_token_client.post(f"/api/hb/{seeded_crons.third.fingerprint}/ok")
        assert r_b.status_code == 204  # noqa: PLR2004
    finally:
        router_mod.cron_rate_limiter = original


@pytest.mark.asyncio
async def test_audit_row_written_for_state_change(
    api_token_client: AsyncClient,
    repo: SqliteRepository,
    seeded_crons: SeededCrons,
) -> None:
    await api_token_client.post(f"/api/hb/{seeded_crons.second.fingerprint}/ok")
    row = await repo.fetch_one(
        text(
            "SELECT who, what, json_extract(after_json, '$.cron_fingerprint') AS cron_fingerprint "
            "FROM audit_log WHERE what = :w "
            'ORDER BY "when" DESC LIMIT 1'
        ),
        {"w": "heartbeat.ok"},
    )
    assert row is not None
    assert row[1] == "heartbeat.ok"
    # Verify audit includes the cron_fingerprint key.
    assert row[2] == seeded_crons.second.fingerprint
    # ``who`` is the token name (set in the api_token_client fixture).
    assert row[0] == "test-token"


@pytest.mark.asyncio
async def test_404_does_not_write_audit(
    api_token_client: AsyncClient,
    repo: SqliteRepository,
    seeded_crons: SeededCrons,
) -> None:
    async def _heartbeat_audit_count() -> object:
        return await repo.fetch_one(
            text("SELECT COUNT(*) FROM audit_log WHERE what LIKE 'heartbeat.%'")
        )

    before_row = await _heartbeat_audit_count()
    before = int(before_row[0]) if before_row is not None else 0  # type: ignore[index]

    resp = await api_token_client.post("/api/hb/missing-cron/ok")
    assert resp.status_code == 404  # noqa: PLR2004

    after_row = await _heartbeat_audit_count()
    after = int(after_row[0]) if after_row is not None else 0  # type: ignore[index]
    assert after == before


@pytest.mark.asyncio
async def test_expected_next_at_computed_when_cadence_set(
    api_token_client: AsyncClient,
    repo: SqliteRepository,
    seeded_crons: SeededCrons,
) -> None:
    """seeded_crons.second has cadence_seconds=60; /ok must populate expected_next_at."""
    await api_token_client.post(f"/api/hb/{seeded_crons.second.fingerprint}/ok")
    row = await repo.fetch_one(
        text("SELECT expected_next_at FROM heartbeats_state WHERE cron_fingerprint = :fp"),
        {"fp": seeded_crons.second.fingerprint},
    )
    assert row is not None
    assert row[0] is not None  # ISO-8601 string


@pytest.mark.asyncio
async def test_expected_next_at_null_when_cadence_zero(
    api_token_client: AsyncClient,
    repo: SqliteRepository,
) -> None:
    """A cron with cadence_seconds=0 leaves expected_next_at NULL after /ok."""
    fp = await _insert_cron(
        repo,
        name="zerocad",
        host="th",
        command_str="/opt/test.sh",
        cadence_seconds=0,
    )
    await api_token_client.post(f"/api/hb/{fp}/ok")
    row = await repo.fetch_one(
        text("SELECT expected_next_at FROM heartbeats_state WHERE cron_fingerprint = :fp"),
        {"fp": fp},
    )
    assert row is not None
    assert row[0] is None


# STAGE-002-005 D5: hidden crons accept heartbeats; they continue to capture
# data and write audit rows. Hidden affects display + notification suppression only.


@pytest.mark.asyncio
async def test_heartbeat_returns_204_for_hidden_cron(
    api_token_client: AsyncClient,
    repo: SqliteRepository,
) -> None:
    """D5 (STAGE-002-005): hidden_at suppresses display + notifications only;
    /start/ok/fail accept and write state + audit normally."""
    # Seed a hidden cron.
    fp = await _insert_cron(
        repo,
        name="dead",
        host="h",
        command_str="/opt/test.sh",
    )
    async with repo.engine.begin() as conn:
        await conn.execute(
            text("UPDATE crons SET hidden_at = :ts WHERE fingerprint = :fp"),
            {"ts": utc_now_iso(), "fp": fp},
        )

    audit_before = await repo.fetch_one(
        text("SELECT COUNT(*) FROM audit_log WHERE what LIKE 'heartbeat.%'")
    )
    audit_count_before = int(audit_before[0]) if audit_before is not None else 0

    # Hidden cron: /start MUST succeed (204) and write state.
    resp = await api_token_client.post(f"/api/hb/{fp}/start")
    assert resp.status_code == 204  # noqa: PLR2004

    # New audit row written.
    audit_after = await repo.fetch_one(
        text("SELECT COUNT(*) FROM audit_log WHERE what LIKE 'heartbeat.%'")
    )
    audit_count_after = int(audit_after[0]) if audit_after is not None else 0
    assert audit_count_after == audit_count_before + 1

    # heartbeats_state row created.
    state_row = await repo.fetch_one(
        text("SELECT current_state FROM heartbeats_state WHERE cron_fingerprint = :fp"),
        {"fp": fp},
    )
    assert state_row is not None
    assert state_row[0] == "running"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "verb,expected_state",
    [("start", "running"), ("ok", "ok"), ("fail", "failed")],
)
async def test_heartbeat_accepts_hidden_cron_per_verb(
    api_token_client: AsyncClient,
    repo: SqliteRepository,
    verb: str,
    expected_state: str,
) -> None:
    """D5: each of /start /ok /fail accepts hidden crons normally."""
    fp = await _insert_cron(
        repo,
        name=f"hid-{verb}",
        host="h",
        command_str=f"/opt/{verb}.sh",
    )
    async with repo.engine.begin() as conn:
        await conn.execute(
            text("UPDATE crons SET hidden_at = :ts WHERE fingerprint = :fp"),
            {"ts": utc_now_iso(), "fp": fp},
        )

    resp = await api_token_client.post(f"/api/hb/{fp}/{verb}")
    assert resp.status_code == 204  # noqa: PLR2004

    state_row = await repo.fetch_one(
        text("SELECT current_state FROM heartbeats_state WHERE cron_fingerprint = :fp"),
        {"fp": fp},
    )
    assert state_row is not None
    assert state_row[0] == expected_state


# ---------------------------------------------------------------------------
# STAGE-002-011: run_id threading into cron_runs table
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_with_run_id_creates_running_cron_run(
    api_token_client: AsyncClient,
    repo: SqliteRepository,
    seeded_crons: SeededCrons,
) -> None:
    """POST /api/hb/{fp}/start?run_id=test-run-1 creates a cron_runs row."""
    fp = seeded_crons.first.fingerprint
    run_id = "test-run-1"

    resp = await api_token_client.post(f"/api/hb/{fp}/start?run_id={run_id}")
    assert resp.status_code == 204  # noqa: PLR2004

    row = await repo.fetch_one(
        text("SELECT run_id, state, source, cron_fingerprint FROM cron_runs WHERE run_id = :rid"),
        {"rid": run_id},
    )
    assert row is not None
    assert row[0] == run_id
    assert row[1] == "running"
    assert row[2] == "wrapper"
    assert row[3] == fp


@pytest.mark.asyncio
async def test_ok_with_run_id_closes_run_ok(
    api_token_client: AsyncClient,
    repo: SqliteRepository,
    seeded_crons: SeededCrons,
) -> None:
    """start?run_id=r2 then ok?run_id=r2 closes the run as 'ok'."""
    fp = seeded_crons.first.fingerprint
    run_id = "r2"

    await api_token_client.post(f"/api/hb/{fp}/start?run_id={run_id}")
    resp = await api_token_client.post(f"/api/hb/{fp}/ok?run_id={run_id}&duration=3.5")
    assert resp.status_code == 204  # noqa: PLR2004

    row = await repo.fetch_one(
        text(
            "SELECT state, duration_seconds, exit_code, ended_at, vl_window_end "
            "FROM cron_runs WHERE run_id = :rid"
        ),
        {"rid": run_id},
    )
    assert row is not None
    assert row[0] == "ok"
    assert float(row[1]) == 3.5  # noqa: PLR2004
    assert int(row[2]) == 0  # default exit_code
    assert row[3] is not None  # ended_at set
    assert row[4] is not None  # vl_window_end set


@pytest.mark.asyncio
async def test_ok_with_run_id_and_explicit_exit_code(
    api_token_client: AsyncClient,
    repo: SqliteRepository,
    seeded_crons: SeededCrons,
) -> None:
    """ok?run_id=r3&exit_code=0 stores the explicit exit code."""
    fp = seeded_crons.first.fingerprint
    run_id = "r3"

    await api_token_client.post(f"/api/hb/{fp}/start?run_id={run_id}")
    resp = await api_token_client.post(f"/api/hb/{fp}/ok?run_id={run_id}&exit_code=0")
    assert resp.status_code == 204  # noqa: PLR2004

    row = await repo.fetch_one(
        text("SELECT exit_code FROM cron_runs WHERE run_id = :rid"),
        {"rid": run_id},
    )
    assert row is not None
    assert int(row[0]) == 0


@pytest.mark.asyncio
async def test_fail_with_run_id_closes_run_fail(
    api_token_client: AsyncClient,
    repo: SqliteRepository,
    seeded_crons: SeededCrons,
) -> None:
    """start?run_id=r4 then fail?run_id=r4&exit_code=42 closes as 'fail'."""
    fp = seeded_crons.first.fingerprint
    run_id = "r4"

    await api_token_client.post(f"/api/hb/{fp}/start?run_id={run_id}")
    resp = await api_token_client.post(
        f"/api/hb/{fp}/fail?run_id={run_id}&exit_code=42&duration=1.0"
    )
    assert resp.status_code == 204  # noqa: PLR2004

    row = await repo.fetch_one(
        text("SELECT state, exit_code FROM cron_runs WHERE run_id = :rid"),
        {"rid": run_id},
    )
    assert row is not None
    assert row[0] == "fail"
    assert int(row[1]) == 42  # noqa: PLR2004


@pytest.mark.asyncio
async def test_ok_with_run_id_no_prior_start_upserts_closed_row(
    api_token_client: AsyncClient,
    repo: SqliteRepository,
    seeded_crons: SeededCrons,
) -> None:
    """POST /ok?run_id=lost-1 without prior /start upserts a closed row."""
    fp = seeded_crons.first.fingerprint
    run_id = "lost-1"

    resp = await api_token_client.post(f"/api/hb/{fp}/ok?run_id={run_id}&duration=2.0")
    assert resp.status_code == 204  # noqa: PLR2004

    row = await repo.fetch_one(
        text("SELECT state, started_at, ended_at FROM cron_runs WHERE run_id = :rid"),
        {"rid": run_id},
    )
    assert row is not None
    assert row[0] == "ok"
    # started_at = ended_at - 2.0 seconds
    assert row[1] is not None
    assert row[2] is not None


@pytest.mark.asyncio
async def test_start_with_duplicate_run_id_is_idempotent(
    api_token_client: AsyncClient,
    repo: SqliteRepository,
    seeded_crons: SeededCrons,
) -> None:
    """POST /start?run_id=dup twice creates only one row."""
    fp = seeded_crons.first.fingerprint
    run_id = "dup"

    await api_token_client.post(f"/api/hb/{fp}/start?run_id={run_id}")
    await api_token_client.post(f"/api/hb/{fp}/start?run_id={run_id}")

    rows = await repo.fetch_all(
        text("SELECT COUNT(*) FROM cron_runs WHERE run_id = :rid"),
        {"rid": run_id},
    )
    assert rows is not None
    count = int(rows[0][0]) if rows else 0
    assert count == 1


@pytest.mark.asyncio
async def test_start_without_run_id_creates_no_cron_run(
    api_token_client: AsyncClient,
    repo: SqliteRepository,
    seeded_crons: SeededCrons,
) -> None:
    """POST /start (no run_id) creates no cron_runs row."""
    fp = seeded_crons.first.fingerprint

    await api_token_client.post(f"/api/hb/{fp}/start")

    rows = await repo.fetch_all(
        text("SELECT COUNT(*) FROM cron_runs WHERE cron_fingerprint = :fp"),
        {"fp": fp},
    )
    count = int(rows[0][0]) if rows else 0
    assert count == 0


@pytest.mark.asyncio
async def test_ok_without_run_id_creates_no_cron_run(
    api_token_client: AsyncClient,
    repo: SqliteRepository,
    seeded_crons: SeededCrons,
) -> None:
    """POST /ok (no run_id) creates no cron_runs row."""
    fp = seeded_crons.first.fingerprint

    await api_token_client.post(f"/api/hb/{fp}/ok")

    rows = await repo.fetch_all(
        text("SELECT COUNT(*) FROM cron_runs WHERE cron_fingerprint = :fp"),
        {"fp": fp},
    )
    count = int(rows[0][0]) if rows else 0
    assert count == 0


@pytest.mark.asyncio
async def test_fail_without_run_id_creates_no_cron_run(
    api_token_client: AsyncClient,
    repo: SqliteRepository,
    seeded_crons: SeededCrons,
) -> None:
    """POST /fail (no run_id) creates no cron_runs row."""
    fp = seeded_crons.first.fingerprint

    await api_token_client.post(f"/api/hb/{fp}/fail")

    rows = await repo.fetch_all(
        text("SELECT COUNT(*) FROM cron_runs WHERE cron_fingerprint = :fp"),
        {"fp": fp},
    )
    count = int(rows[0][0]) if rows else 0
    assert count == 0


@pytest.mark.asyncio
async def test_start_with_bad_charset_run_id_returns_422(
    api_token_client: AsyncClient,
    seeded_crons: SeededCrons,
) -> None:
    """POST /start?run_id=bad;value (semicolon) returns 422."""
    fp = seeded_crons.first.fingerprint

    resp = await api_token_client.post(f"/api/hb/{fp}/start?run_id=bad;value")
    assert resp.status_code == 422  # noqa: PLR2004


@pytest.mark.asyncio
async def test_ok_with_overlong_run_id_returns_422(
    api_token_client: AsyncClient,
    seeded_crons: SeededCrons,
) -> None:
    """POST /ok?run_id=<65 chars> returns 422."""
    fp = seeded_crons.first.fingerprint
    long_id = "a" * 65

    resp = await api_token_client.post(f"/api/hb/{fp}/ok?run_id={long_id}")
    assert resp.status_code == 422  # noqa: PLR2004


@pytest.mark.asyncio
async def test_fail_with_whitespace_run_id_returns_422(
    api_token_client: AsyncClient,
    seeded_crons: SeededCrons,
) -> None:
    """POST /fail?run_id=has%20space returns 422."""
    fp = seeded_crons.first.fingerprint

    resp = await api_token_client.post(f"/api/hb/{fp}/fail?run_id=has%20space")
    assert resp.status_code == 422  # noqa: PLR2004


@pytest.mark.asyncio
async def test_ok_with_valid_uuid_run_id_accepted(
    api_token_client: AsyncClient,
    seeded_crons: SeededCrons,
) -> None:
    """POST /ok?run_id=<36-char-UUID> accepts it."""
    fp = seeded_crons.first.fingerprint
    uuid_id = "123e4567-e89b-12d3-a456-426614174000"

    resp = await api_token_client.post(f"/api/hb/{fp}/ok?run_id={uuid_id}")
    assert resp.status_code == 204  # noqa: PLR2004


@pytest.mark.asyncio
async def test_run_id_422_writes_no_cron_run_row(
    api_token_client: AsyncClient,
    repo: SqliteRepository,
    seeded_crons: SeededCrons,
) -> None:
    """A 422 on bad run_id doesn't create a cron_runs row."""
    fp = seeded_crons.first.fingerprint

    resp = await api_token_client.post(f"/api/hb/{fp}/start?run_id=bad;value")
    assert resp.status_code == 422  # noqa: PLR2004

    rows = await repo.fetch_all(
        text("SELECT COUNT(*) FROM cron_runs WHERE cron_fingerprint = :fp"),
        {"fp": fp},
    )
    count = int(rows[0][0]) if rows else 0
    assert count == 0
