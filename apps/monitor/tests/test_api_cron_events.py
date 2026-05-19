"""Tests for POST /api/internal/cron-events — cron log event ingest endpoint."""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from homelab_monitor.kernel.cron.fingerprint import compute_fingerprint
from homelab_monitor.kernel.cron.log_match import canonical_log_key
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.db.time import utc_now_iso

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_HOST = "h1"
_CMD = "/usr/bin/backup.sh"
_CMD_LOGGED = f"({_CMD})"  # vanilla cron log wraps in parens


def _now_iso() -> str:
    return utc_now_iso()


async def _insert_cron_with_log_key(  # noqa: PLR0913
    repo: SqliteRepository,
    *,
    host: str = _HOST,
    command: str = _CMD,
    source_path: str = "/etc/crontab",
    schedule: str = "* * * * *",
    cadence_seconds: int = 60,
    name: str = "test-cron",
) -> str:
    """Insert a cron row with log_match_key populated. Returns fingerprint."""
    fp = compute_fingerprint(host=host, source_path=source_path, schedule=schedule, command=command)
    lmk = canonical_log_key(command)
    now = _now_iso()
    async with repo.engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO crons ("
                "  fingerprint, name, host, command, schedule, schedule_canonical,"
                "  cadence_seconds, expected_grace_seconds, enabled, last_seen_state,"
                "  created_at, updated_at, hidden_at, source_path, wrapper_last_seen_at,"
                "  last_discovered_at, soft_deleted_at, log_match_key"
                ") VALUES ("
                "  :fp, :name, :host, :cmd, :sched, :sched_canon,"
                "  :cad, :grace, 1, 'unknown',"
                "  :now, :now, NULL, :sp, NULL,"
                "  :now, NULL, :lmk"
                ")"
            ),
            {
                "fp": fp,
                "name": name,
                "host": host,
                "cmd": command,
                "sched": schedule,
                "sched_canon": schedule,
                "cad": cadence_seconds,
                "grace": 300,
                "now": now,
                "sp": source_path,
                "lmk": lmk,
            },
        )
    return fp


def _event(
    *,
    host: str = _HOST,
    command: str = _CMD_LOGGED,
    exit_code: int | None = None,
    cursor: str | None = "c1",
    timestamp: str | None = None,
) -> dict[str, object]:
    return {
        "host": host,
        "command": command,
        "user": "root",
        "timestamp": timestamp or _now_iso(),
        "exit_code": exit_code,
        "journal_cursor": cursor,
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def cron_events_client(
    db_url: str,
    master_key: bytes,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[tuple[AsyncClient, SqliteRepository]]:
    """App client with CRON_EVENTS_INGEST_WRITE token + repo access."""
    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    monkeypatch.setenv("HOMELAB_MONITOR_MASTER_KEY", base64.b64encode(master_key).decode())
    monkeypatch.setenv("HOMELAB_MONITOR_ALERTMANAGER_URL", "disabled")
    monkeypatch.setenv("HOMELAB_MONITOR_DISABLE_STARTUP_CRON_DISCOVERY", "1")

    from homelab_monitor.kernel.api.app import create_app  # noqa: PLC0415
    from homelab_monitor.kernel.auth.api_tokens import make_api_token  # noqa: PLC0415
    from homelab_monitor.kernel.auth.scopes import Scope  # noqa: PLC0415

    app = create_app(lifespan_enabled=True)
    async with app.router.lifespan_context(app):
        plaintext, _ = make_api_token(prefix="ce-test")
        await app.state.auth_repo.create_api_token(
            name="ce-test-token",
            scopes={Scope.CRON_EVENTS_INGEST_WRITE},
            plaintext_token=plaintext,
        )
        repo: SqliteRepository = app.state.repo
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": f"Bearer {plaintext}"},
        ) as client:
            yield client, repo


_URL = "/api/internal/cron-events"


async def _state(repo: SqliteRepository, fp: str) -> dict[str, object] | None:
    """Fetch heartbeats_state row as a dict for the given fingerprint."""
    row = await repo.fetch_one(
        text(
            "SELECT current_state, last_ok_at, last_fail_at, last_exit_code,"
            "       observed_runs_total, last_observed_run_at"
            " FROM heartbeats_state WHERE cron_fingerprint = :fp"
        ),
        {"fp": fp},
    )
    if row is None:
        return None
    return {
        "current_state": row[0],
        "last_ok_at": row[1],
        "last_fail_at": row[2],
        "last_exit_code": row[3],
        "observed_runs_total": row[4],
        "last_observed_run_at": row[5],
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bare_line_records_observed_run(
    cron_events_client: tuple[AsyncClient, SqliteRepository],
) -> None:
    """D1: event with exit_code=None → observed_run; current_state remains 'unknown'."""
    client, repo = cron_events_client
    fp = await _insert_cron_with_log_key(repo)

    resp = await client.post(_URL, json=[_event(exit_code=None, cursor="c1")])
    assert resp.status_code == 202  # noqa: PLR2004
    body = resp.json()
    assert body["observed_runs"] == 1
    assert body["state_ok"] == 0
    assert body["state_fail"] == 0

    state = await _state(repo, fp)
    assert state is not None
    assert state["observed_runs_total"] == 1
    assert state["last_observed_run_at"] is not None
    # D1 correctness: bare line must NOT flip current_state to "ok"
    assert state["current_state"] == "unknown"


@pytest.mark.asyncio
async def test_wrapper_tagged_exit0_records_ok(
    cron_events_client: tuple[AsyncClient, SqliteRepository],
) -> None:
    """exit_code=0 → state_ok disposition; current_state becomes 'ok'."""
    client, repo = cron_events_client
    fp = await _insert_cron_with_log_key(repo)

    resp = await client.post(_URL, json=[_event(exit_code=0, cursor="c2")])
    assert resp.status_code == 202  # noqa: PLR2004
    body = resp.json()
    assert body["state_ok"] == 1

    state = await _state(repo, fp)
    assert state is not None
    assert state["current_state"] == "ok"
    assert state["last_ok_at"] is not None


@pytest.mark.asyncio
async def test_wrapper_tagged_nonzero_records_fail(
    cron_events_client: tuple[AsyncClient, SqliteRepository],
) -> None:
    """exit_code=1 → state_fail disposition; current_state becomes 'failed'."""
    client, repo = cron_events_client
    fp = await _insert_cron_with_log_key(repo)

    resp = await client.post(_URL, json=[_event(exit_code=1, cursor="c3")])
    assert resp.status_code == 202  # noqa: PLR2004
    body = resp.json()
    assert body["state_fail"] == 1

    state = await _state(repo, fp)
    assert state is not None
    assert state["current_state"] == "failed"
    assert state["last_fail_at"] is not None
    assert state["last_exit_code"] == 1


@pytest.mark.asyncio
async def test_idempotent_replay_same_cursor(
    cron_events_client: tuple[AsyncClient, SqliteRepository],
) -> None:
    """Posting the same cursor twice: second POST returns replay_skipped, no double-count."""
    client, repo = cron_events_client
    fp = await _insert_cron_with_log_key(repo)

    ev = [_event(exit_code=None, cursor="dedup-cursor-1")]
    r1 = await client.post(_URL, json=ev)
    assert r1.status_code == 202  # noqa: PLR2004
    assert r1.json()["observed_runs"] == 1

    r2 = await client.post(_URL, json=ev)
    assert r2.status_code == 202  # noqa: PLR2004
    body2 = r2.json()
    assert body2["replay_skipped"] == 1
    assert body2["observed_runs"] == 0

    state = await _state(repo, fp)
    assert state is not None
    assert state["observed_runs_total"] == 1  # NOT 2


@pytest.mark.asyncio
async def test_unknown_command_no_match(
    cron_events_client: tuple[AsyncClient, SqliteRepository],
) -> None:
    """Command matching no cron → no_match disposition; no state row created."""
    client, repo = cron_events_client
    await _insert_cron_with_log_key(repo)

    resp = await client.post(
        _URL,
        json=[_event(command="(/opt/no-such-command.sh)", cursor="c-nomatch")],
    )
    assert resp.status_code == 202  # noqa: PLR2004
    assert resp.json()["no_match"] == 1

    # confirm no state row was written
    row = await repo.fetch_one(
        text("SELECT COUNT(*) FROM heartbeats_state"),
        {},
    )
    assert row is not None
    assert row[0] == 0


@pytest.mark.asyncio
async def test_ambiguous_match_skips_state_write(
    cron_events_client: tuple[AsyncClient, SqliteRepository],
) -> None:
    """Two crons with same (host, log_match_key) → ambiguous; neither gets state."""
    client, repo = cron_events_client
    fp1 = await _insert_cron_with_log_key(
        repo,
        host=_HOST,
        command=_CMD,
        source_path="/etc/crontab",
        name="amb-cron-1",
    )
    fp2 = await _insert_cron_with_log_key(
        repo,
        host=_HOST,
        command=_CMD,
        source_path="/var/spool/cron/crontabs/root",  # different source → different fp
        name="amb-cron-2",
    )

    resp = await client.post(_URL, json=[_event(exit_code=None, cursor="c-amb")])
    assert resp.status_code == 202  # noqa: PLR2004
    body = resp.json()
    assert body["ambiguous"] == 1
    assert body["observed_runs"] == 0

    # Neither cron gets a state row
    for fp in (fp1, fp2):
        row = await repo.fetch_one(
            text("SELECT 1 FROM heartbeats_state WHERE cron_fingerprint = :fp"),
            {"fp": fp},
        )
        assert row is None, f"state row should NOT exist for {fp}"


@pytest.mark.asyncio
async def test_syslog_path_synthesizes_cursor(
    cron_events_client: tuple[AsyncClient, SqliteRepository],
) -> None:
    """journal_cursor=null → synthesized cursor; event processes; second POST dedupes."""
    client, repo = cron_events_client
    await _insert_cron_with_log_key(repo)

    ts = "2026-01-01T00:00:00Z"
    ev = [_event(exit_code=None, cursor=None, timestamp=ts)]

    r1 = await client.post(_URL, json=ev)
    assert r1.status_code == 202  # noqa: PLR2004
    assert r1.json()["observed_runs"] == 1

    r2 = await client.post(_URL, json=ev)
    assert r2.status_code == 202  # noqa: PLR2004
    assert r2.json()["replay_skipped"] == 1


@pytest.mark.asyncio
async def test_batch_mixed_dispositions(
    cron_events_client: tuple[AsyncClient, SqliteRepository],
) -> None:
    """Batch of 3 events: one bare match, one exit=0 match, one no-match."""
    client, repo = cron_events_client
    await _insert_cron_with_log_key(repo)

    events = [
        _event(exit_code=None, cursor="batch-c1"),
        _event(exit_code=0, cursor="batch-c2"),
        _event(command="(/no/such/cmd.sh)", cursor="batch-c3"),
    ]
    resp = await client.post(_URL, json=events)
    assert resp.status_code == 202  # noqa: PLR2004
    body = resp.json()
    assert body["received"] == 3  # noqa: PLR2004
    assert body["observed_runs"] == 1
    assert body["state_ok"] == 1
    assert body["no_match"] == 1


@pytest.mark.asyncio
async def test_requires_token_scope_401_no_auth(
    db_url: str,
    master_key: bytes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No Authorization header → 401."""
    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    monkeypatch.setenv("HOMELAB_MONITOR_MASTER_KEY", base64.b64encode(master_key).decode())
    monkeypatch.setenv("HOMELAB_MONITOR_ALERTMANAGER_URL", "disabled")

    from homelab_monitor.kernel.api.app import create_app  # noqa: PLC0415

    app = create_app(lifespan_enabled=True)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        resp = await client.post(_URL, json=[_event(cursor="auth-test-1")])
        assert resp.status_code == 401  # noqa: PLR2004


@pytest.mark.asyncio
async def test_requires_token_scope_403_wrong_scope(
    db_url: str,
    master_key: bytes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Token with HEARTBEAT_WRITE but not CRON_EVENTS_INGEST_WRITE → 403."""
    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    monkeypatch.setenv("HOMELAB_MONITOR_MASTER_KEY", base64.b64encode(master_key).decode())
    monkeypatch.setenv("HOMELAB_MONITOR_ALERTMANAGER_URL", "disabled")

    from homelab_monitor.kernel.api.app import create_app  # noqa: PLC0415
    from homelab_monitor.kernel.auth.api_tokens import make_api_token  # noqa: PLC0415
    from homelab_monitor.kernel.auth.scopes import Scope  # noqa: PLC0415

    app = create_app(lifespan_enabled=True)
    async with app.router.lifespan_context(app):
        plaintext, _ = make_api_token(prefix="wrong")
        await app.state.auth_repo.create_api_token(
            name="wrong-scope-token",
            scopes={Scope.HEARTBEAT_WRITE},
            plaintext_token=plaintext,
        )
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": f"Bearer {plaintext}"},
        ) as client:
            resp = await client.post(_URL, json=[_event(cursor="scope-test-1")])
            assert resp.status_code == 403  # noqa: PLR2004


@pytest.mark.asyncio
async def test_extra_field_rejected(
    cron_events_client: tuple[AsyncClient, SqliteRepository],
) -> None:
    """Event body with extra field → 422 (Pydantic extra='forbid')."""
    client, repo = cron_events_client
    await _insert_cron_with_log_key(repo)

    bad_event = {**_event(cursor="extra-c1"), "unexpected_key": "oops"}
    resp = await client.post(_URL, json=[bad_event])
    assert resp.status_code == 422  # noqa: PLR2004


@pytest.mark.asyncio
async def test_observed_run_then_ok_preserves_total(
    cron_events_client: tuple[AsyncClient, SqliteRepository],
) -> None:
    """bare-line then exit=0: observed_runs_total preserved AND current_state=ok."""
    client, repo = cron_events_client
    fp = await _insert_cron_with_log_key(repo)

    r1 = await client.post(_URL, json=[_event(exit_code=None, cursor="seq-c1")])
    assert r1.json()["observed_runs"] == 1

    r2 = await client.post(_URL, json=[_event(exit_code=0, cursor="seq-c2")])
    assert r2.json()["state_ok"] == 1

    state = await _state(repo, fp)
    assert state is not None
    assert state["observed_runs_total"] == 1  # preserved, not reset
    assert state["current_state"] == "ok"


@pytest.mark.asyncio
async def test_production_shape_bare_line_observed_run(
    cron_events_client: tuple[AsyncClient, SqliteRepository],
) -> None:
    """Production Vector journald shape: no journal_cursor, post-wrapper command, real hostname.

    Vector's journald source emits .host from _HOSTNAME but does NOT emit __CURSOR
    as event data. The command field is the post-VRL-wrapper-strip form (no parens).
    The endpoint must synthesize a cursor and correctly match and record the event.
    """
    client, repo = cron_events_client
    prod_host = "intelnuc"
    prod_cmd = "/usr/bin/backup.sh"  # post-wrapper-strip — no parens
    fp = await _insert_cron_with_log_key(repo, host=prod_host, command=prod_cmd)

    prod_event = {
        "host": prod_host,
        "command": prod_cmd,
        "user": "root",
        "timestamp": "2026-05-16T02:00:00+00:00",
        "exit_code": None,
        # journal_cursor intentionally OMITTED — forces synthesize_cursor() path
    }
    resp = await client.post(_URL, json=[prod_event])
    assert resp.status_code == 202  # noqa: PLR2004
    body = resp.json()
    assert body["observed_runs"] == 1
    assert body["state_ok"] == 0
    assert body["state_fail"] == 0
    assert body["replay_skipped"] == 0

    state = await _state(repo, fp)
    assert state is not None
    assert state["observed_runs_total"] == 1
    assert state["last_observed_run_at"] is not None
    assert state["current_state"] == "unknown"  # D1: bare line must NOT flip state


@pytest.mark.asyncio
async def test_production_shape_synthesized_cursor_idempotency(
    cron_events_client: tuple[AsyncClient, SqliteRepository],
) -> None:
    """Same production-shape event posted twice → first observed_run, second replay_skipped.

    Verifies that synthesize_cursor() produces a stable dedup key when journald
    sends no __CURSOR (the normal production path).
    """
    client, repo = cron_events_client
    prod_host = "intelnuc"
    prod_cmd = "/usr/local/bin/db-backup.sh"
    await _insert_cron_with_log_key(repo, host=prod_host, command=prod_cmd)

    prod_event = {
        "host": prod_host,
        "command": prod_cmd,
        "user": "root",
        "timestamp": "2026-05-16T03:00:00+00:00",
        # journal_cursor absent — synthesize_cursor() path
    }

    r1 = await client.post(_URL, json=[prod_event])
    assert r1.status_code == 202  # noqa: PLR2004
    assert r1.json()["observed_runs"] == 1
    assert r1.json()["replay_skipped"] == 0

    r2 = await client.post(_URL, json=[prod_event])
    assert r2.status_code == 202  # noqa: PLR2004
    assert r2.json()["replay_skipped"] == 1
    assert r2.json()["observed_runs"] == 0


@pytest.mark.asyncio
async def test_production_shape_exit0_records_ok(
    cron_events_client: tuple[AsyncClient, SqliteRepository],
) -> None:
    """Production Vector shape with exit_code=0 → state_ok disposition, current_state=ok."""
    client, repo = cron_events_client
    prod_host = "intelnuc"
    prod_cmd = "/usr/bin/certbot-renew.sh"
    fp = await _insert_cron_with_log_key(repo, host=prod_host, command=prod_cmd)

    prod_event = {
        "host": prod_host,
        "command": prod_cmd,
        "user": "root",
        "timestamp": "2026-05-16T04:00:00+00:00",
        "exit_code": 0,
        # no journal_cursor — synthesize_cursor() path
    }
    resp = await client.post(_URL, json=[prod_event])
    assert resp.status_code == 202  # noqa: PLR2004
    body = resp.json()
    assert body["state_ok"] == 1
    assert body["observed_runs"] == 0

    state = await _state(repo, fp)
    assert state is not None
    assert state["current_state"] == "ok"
    assert state["last_ok_at"] is not None


@pytest.mark.asyncio
async def test_production_shape_nonzero_exit_records_fail(
    cron_events_client: tuple[AsyncClient, SqliteRepository],
) -> None:
    """Production Vector shape with exit_code=2 → state_fail disposition, current_state=failed."""
    client, repo = cron_events_client
    prod_host = "intelnuc"
    prod_cmd = "/usr/bin/rsync-backup.sh"
    fp = await _insert_cron_with_log_key(repo, host=prod_host, command=prod_cmd)

    prod_event = {
        "host": prod_host,
        "command": prod_cmd,
        "user": "root",
        "timestamp": "2026-05-16T05:00:00+00:00",
        "exit_code": 2,
        # no journal_cursor — synthesize_cursor() path
    }
    resp = await client.post(_URL, json=[prod_event])
    assert resp.status_code == 202  # noqa: PLR2004
    body = resp.json()
    assert body["state_fail"] == 1
    assert body["state_ok"] == 0

    state = await _state(repo, fp)
    assert state is not None
    assert state["current_state"] == "failed"
    assert state["last_fail_at"] is not None
    assert state["last_exit_code"] == 2  # noqa: PLR2004


@pytest.mark.asyncio
async def test_empty_timestamp_rejected_422(
    cron_events_client: tuple[AsyncClient, SqliteRepository],
) -> None:
    """Empty timestamp string (Vector VRL fallback) → 422 from field_validator."""
    client, repo = cron_events_client
    await _insert_cron_with_log_key(repo)

    bad_event = {
        "host": _HOST,
        "command": _CMD,
        "user": "root",
        "timestamp": "",  # Vector emits "" when journald has no usable timestamp
    }
    resp = await client.post(_URL, json=[bad_event])
    assert resp.status_code == 422  # noqa: PLR2004


@pytest.mark.asyncio
async def test_z_suffix_timestamp_accepted_and_normalized(
    cron_events_client: tuple[AsyncClient, SqliteRepository],
) -> None:
    """Timestamp with Z suffix (common ISO-8601 variant) is accepted and normalized to +00:00."""
    client, repo = cron_events_client
    prod_host = "intelnuc"
    prod_cmd = "/usr/bin/logrotate.sh"
    fp = await _insert_cron_with_log_key(repo, host=prod_host, command=prod_cmd)

    prod_event = {
        "host": prod_host,
        "command": prod_cmd,
        "user": "root",
        "timestamp": "2026-05-16T06:00:00Z",  # Z-suffix form
        "exit_code": None,
    }
    resp = await client.post(_URL, json=[prod_event])
    assert resp.status_code == 202  # noqa: PLR2004
    assert resp.json()["observed_runs"] == 1

    state = await _state(repo, fp)
    assert state is not None
    assert state["observed_runs_total"] == 1


@pytest.mark.asyncio
async def test_malformed_timestamp_rejected_422(
    cron_events_client: tuple[AsyncClient, SqliteRepository],
) -> None:
    """Non-ISO timestamp string → 422 from field_validator (fromisoformat ValueError branch)."""
    client, repo = cron_events_client
    await _insert_cron_with_log_key(repo)

    bad_event = {
        "host": _HOST,
        "command": _CMD,
        "user": "root",
        "timestamp": "not-a-date",
    }
    resp = await client.post(_URL, json=[bad_event])
    assert resp.status_code == 422  # noqa: PLR2004


@pytest.mark.asyncio
async def test_naive_timestamp_rejected_422(
    cron_events_client: tuple[AsyncClient, SqliteRepository],
) -> None:
    """Naive (no timezone) ISO timestamp → 422 from field_validator (tzinfo is None branch)."""
    client, repo = cron_events_client
    await _insert_cron_with_log_key(repo)

    bad_event = {
        "host": _HOST,
        "command": _CMD,
        "user": "root",
        "timestamp": "2026-05-16T06:00:00",  # valid ISO format but no tz offset
    }
    resp = await client.post(_URL, json=[bad_event])
    assert resp.status_code == 422  # noqa: PLR2004


@pytest.mark.asyncio
async def test_metric_emitted_on_match(
    cron_events_client: tuple[AsyncClient, SqliteRepository],
) -> None:
    """Matching event → matches disposition count 1; ambiguous event → ambiguous count 1."""
    client, repo = cron_events_client
    await _insert_cron_with_log_key(repo)

    # Single-match event → disposition observed_runs == 1 (metric emitted internally)
    r1 = await client.post(_URL, json=[_event(exit_code=None, cursor="metric-c1")])
    assert r1.status_code == 202  # noqa: PLR2004
    assert r1.json()["observed_runs"] == 1

    # Seed a second cron with same log_match_key for ambiguous path
    await _insert_cron_with_log_key(
        repo,
        host=_HOST,
        command=_CMD,
        source_path="/var/spool/cron/crontabs/root",
        name="metric-amb",
    )
    r2 = await client.post(_URL, json=[_event(cursor="metric-c2")])
    assert r2.status_code == 202  # noqa: PLR2004
    assert r2.json()["ambiguous"] == 1


# ---------------------------------------------------------------------------
# STAGE-002-013: B-mode cron_runs row create/close tests
# ---------------------------------------------------------------------------


async def _get_cron_runs(repo: SqliteRepository, cron_fingerprint: str) -> list[dict[str, object]]:
    """Fetch all cron_runs rows for a fingerprint as dicts."""
    rows = await repo.fetch_all(
        text(
            "SELECT run_id, cron_fingerprint, source, state, started_at, ended_at,"
            "       duration_seconds, exit_code, vl_window_start, vl_window_end"
            " FROM cron_runs WHERE cron_fingerprint = :fp"
            " ORDER BY started_at ASC"
        ),
        {"fp": cron_fingerprint},
    )
    return [
        {
            "run_id": str(r.run_id),
            "cron_fingerprint": str(r.cron_fingerprint),
            "source": str(r.source),
            "state": str(r.state),
            "started_at": str(r.started_at),
            "ended_at": r.ended_at,
            "duration_seconds": r.duration_seconds,
            "exit_code": r.exit_code,
            "vl_window_start": r.vl_window_start,
            "vl_window_end": r.vl_window_end,
        }
        for r in rows
    ]


@pytest.mark.asyncio
async def test_bmode_cmd_creates_cron_runs_row(
    cron_events_client: tuple[AsyncClient, SqliteRepository],
) -> None:
    """OBSERVED_RUN creates a cron_runs row with source='logscrape' and state='running'."""
    client, repo = cron_events_client
    fp = await _insert_cron_with_log_key(repo)

    ts = _now_iso()
    resp = await client.post(
        _URL, json=[_event(exit_code=None, cursor="bmode-cmd-1", timestamp=ts)]
    )
    assert resp.status_code == 202  # noqa: PLR2004
    assert resp.json()["observed_runs"] == 1

    runs = await _get_cron_runs(repo, fp)
    assert len(runs) == 1
    run = runs[0]
    assert run["source"] == "logscrape"
    assert run["state"] == "running"
    assert run["started_at"] == ts
    assert run["vl_window_start"] == ts
    assert run["ended_at"] is None
    assert run["vl_window_end"] is None

    # Heartbeat state (record_observed_run) still fires
    state = await _state(repo, fp)
    assert state is not None
    assert state["observed_runs_total"] == 1


@pytest.mark.asyncio
async def test_bmode_exit0_closes_run_ok(
    cron_events_client: tuple[AsyncClient, SqliteRepository],
) -> None:
    """CMD then exit=0: the run row is closed state='ok' with duration and exit_code=0."""
    client, repo = cron_events_client
    fp = await _insert_cron_with_log_key(repo)

    ts_start = "2026-05-19T00:00:00+00:00"
    ts_end = "2026-05-19T00:00:10+00:00"

    # CMD event
    await client.post(_URL, json=[_event(exit_code=None, cursor="bclose-cmd", timestamp=ts_start)])
    # exit=0 event
    resp = await client.post(
        _URL, json=[_event(exit_code=0, cursor="bclose-exit0", timestamp=ts_end)]
    )
    assert resp.status_code == 202  # noqa: PLR2004
    assert resp.json()["state_ok"] == 1

    runs = await _get_cron_runs(repo, fp)
    assert len(runs) == 1
    run = runs[0]
    assert run["state"] == "ok"
    assert run["exit_code"] == 0
    assert run["ended_at"] == ts_end
    assert run["vl_window_end"] == ts_end
    assert run["duration_seconds"] is not None
    assert float(run["duration_seconds"]) > 0  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_bmode_exit_nonzero_closes_run_fail(
    cron_events_client: tuple[AsyncClient, SqliteRepository],
) -> None:
    """CMD then exit=N (non-zero): run row is closed state='fail' with exit_code=N."""
    client, repo = cron_events_client
    fp = await _insert_cron_with_log_key(repo)

    ts_start = "2026-05-19T00:00:00+00:00"
    ts_end = "2026-05-19T00:00:05+00:00"

    await client.post(_URL, json=[_event(exit_code=None, cursor="bfail-cmd", timestamp=ts_start)])
    resp = await client.post(
        _URL, json=[_event(exit_code=2, cursor="bfail-exit", timestamp=ts_end)]
    )
    assert resp.status_code == 202  # noqa: PLR2004
    assert resp.json()["state_fail"] == 1

    runs = await _get_cron_runs(repo, fp)
    assert len(runs) == 1
    run = runs[0]
    assert run["state"] == "fail"
    assert run["exit_code"] == 2  # noqa: PLR2004


@pytest.mark.asyncio
async def test_bmode_exit_without_prior_cmd_is_noop(
    cron_events_client: tuple[AsyncClient, SqliteRepository],
) -> None:
    """exit= with no matching open run is a no-op: no cron_runs row, no crash."""
    client, repo = cron_events_client
    fp = await _insert_cron_with_log_key(repo)

    ts_end = "2026-05-19T00:00:10+00:00"
    resp = await client.post(
        _URL, json=[_event(exit_code=0, cursor="bnostart-exit", timestamp=ts_end)]
    )
    assert resp.status_code == 202  # noqa: PLR2004
    assert resp.json()["state_ok"] == 1

    runs = await _get_cron_runs(repo, fp)
    # No CMD was posted, so no cron_runs row should exist
    assert len(runs) == 0


@pytest.mark.asyncio
async def test_bmode_two_cmds_exit_closes_most_recent(
    cron_events_client: tuple[AsyncClient, SqliteRepository],
) -> None:
    """Two CMD events then one exit=: exit correlates to the MOST RECENT open run."""
    client, repo = cron_events_client
    fp = await _insert_cron_with_log_key(repo)

    ts1 = "2026-05-19T00:00:00+00:00"
    ts2 = "2026-05-19T00:01:00+00:00"
    ts_exit = "2026-05-19T00:01:30+00:00"

    await client.post(_URL, json=[_event(exit_code=None, cursor="two-cmd-1", timestamp=ts1)])
    await client.post(_URL, json=[_event(exit_code=None, cursor="two-cmd-2", timestamp=ts2)])
    resp = await client.post(_URL, json=[_event(exit_code=0, cursor="two-exit", timestamp=ts_exit)])
    assert resp.status_code == 202  # noqa: PLR2004

    runs = await _get_cron_runs(repo, fp)
    assert len(runs) == 2  # noqa: PLR2004
    # Sort by started_at ascending (already done by query)
    first_run = runs[0]  # started at ts1
    second_run = runs[1]  # started at ts2

    # First run: still running (no exit correlated)
    assert first_run["state"] == "running"
    # Second run: closed by exit=0
    assert second_run["state"] == "ok"
    assert second_run["ended_at"] == ts_exit


@pytest.mark.asyncio
async def test_bmode_cursor_idempotency_no_duplicate_row(
    cron_events_client: tuple[AsyncClient, SqliteRepository],
) -> None:
    """Posting the same CMD event twice (same journal_cursor) creates exactly ONE cron_runs row."""
    client, repo = cron_events_client
    fp = await _insert_cron_with_log_key(repo)

    ev = [_event(exit_code=None, cursor="idem-cmd-1")]

    r1 = await client.post(_URL, json=ev)
    assert r1.status_code == 202  # noqa: PLR2004
    assert r1.json()["observed_runs"] == 1

    r2 = await client.post(_URL, json=ev)
    assert r2.status_code == 202  # noqa: PLR2004
    assert r2.json()["replay_skipped"] == 1

    runs = await _get_cron_runs(repo, fp)
    assert len(runs) == 1  # exactly ONE row, no duplicate


@pytest.mark.asyncio
async def test_bmode_dropped_exit_leaves_run_running(
    cron_events_client: tuple[AsyncClient, SqliteRepository],
) -> None:
    """CMD with no subsequent exit= leaves the run in state='running' for reconciler."""
    client, repo = cron_events_client
    fp = await _insert_cron_with_log_key(repo)

    ts_start = _now_iso()
    resp = await client.post(
        _URL, json=[_event(exit_code=None, cursor="dropped-exit-1", timestamp=ts_start)]
    )
    assert resp.status_code == 202  # noqa: PLR2004
    assert resp.json()["observed_runs"] == 1

    runs = await _get_cron_runs(repo, fp)
    assert len(runs) == 1
    assert runs[0]["state"] == "running"


# ---------------------------------------------------------------------------
# Group E3 — naive-timestamp normalization in _close_bmode_run
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_close_bmode_run_normalizes_naive_timestamps(
    cron_events_client: tuple[AsyncClient, SqliteRepository],
) -> None:
    """_close_bmode_run handles a naive DB-stored started_at without TypeError.

    The started_dt naive-coercion branch must execute and succeed so the
    naive/aware subtraction works and duration_seconds is computed correctly.
    (event.timestamp is always tz-aware — CronEventItem validation rejects naive.)
    """
    client, repo = cron_events_client
    fp = await _insert_cron_with_log_key(repo)

    # Seed a running cron_runs row whose started_at is a NAIVE ISO-8601 string
    # (no +00:00 offset) — exercises the `started_dt.tzinfo is None` branch.
    naive_started_at = "2026-05-19T00:00:00"
    from homelab_monitor.kernel.cron.run_repository import CronRunRepository  # noqa: PLC0415

    run_repo = CronRunRepository(repo)
    await run_repo.insert_run(
        run_id="naive-ts-run",
        cron_fingerprint=fp,
        source="logscrape",
        started_at=naive_started_at,
        vl_window_start=naive_started_at,
    )

    # POST an exit=0 event whose timestamp is also NAIVE — exercises the
    # `ended_dt.tzinfo is None` branch. The field_validator in CronEventItem
    # normalizes tz-aware timestamps; posting a naive one is rejected at the API
    # layer. To exercise the _close_bmode_run code path we use an aware timestamp
    # that is 10 seconds after the naive started_at (the validator accepts it).
    # The started_at stored in the DB is naive, which is what triggers the branch.
    ts_end = "2026-05-19T00:00:10+00:00"
    resp = await client.post(
        _URL,
        json=[_event(exit_code=0, cursor="naive-ts-exit", timestamp=ts_end)],
    )
    assert resp.status_code == 202  # noqa: PLR2004
    assert resp.json()["state_ok"] == 1

    runs = await _get_cron_runs(repo, fp)
    assert len(runs) == 1
    run = runs[0]
    assert run["state"] == "ok"
    assert run["duration_seconds"] == pytest.approx(10.0)  # pyright: ignore[reportUnknownMemberType]
