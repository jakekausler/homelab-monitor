"""API endpoint tests for the auto-fix kill-switch settings (STAGE-009-007).

Tests the router endpoints: GET /api/settings/autofix/kill-switch,
POST /api/settings/autofix/kill-switch.

Uses authenticated_client fixture (session + CSRF); orchestrator wired via
app.state when a test needs kill_inflight to actually run. Covers all
branches: auth, CSRF, extra-field 422, phrase mismatch, case-insensitivity,
noop, on->off with/without inflight, docker-kill-failure 502, unwind warning.
"""

from __future__ import annotations

import json
import time

import pytest
from httpx import AsyncClient
from sqlalchemy import text

from homelab_monitor.kernel.autofix.orchestrator import (
    _CurrentRun,  # pyright: ignore[reportPrivateUsage]
)
from homelab_monitor.kernel.autofix.runs_repository import RunbookRunsRepository
from homelab_monitor.kernel.autofix.types import RunMode
from homelab_monitor.kernel.db.repositories.app_settings_repository import (
    AppSettingsRepository,
)
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.docker.socket_client import DockerSocketConnectionError
from homelab_monitor.kernel.secrets.repository import AsyncSecretsRepository
from homelab_monitor.kernel.security.pin import PIN_HASH_KEY, hash_pin

# Cross-file helper reuse — established idiom in this codebase (see
# tests/test_api_autofix.py's import of the same helpers).
from tests.test_autofix_orchestrator import (
    _FakeDockerClient,  # pyright: ignore[reportPrivateUsage]
    _insert_alert,  # pyright: ignore[reportPrivateUsage]
    _insert_runbook,  # pyright: ignore[reportPrivateUsage]
    _make_alert,  # pyright: ignore[reportPrivateUsage]
    _make_orchestrator,  # pyright: ignore[reportPrivateUsage]
    _make_runbook_record,  # pyright: ignore[reportPrivateUsage]
)

_URL = "/api/settings/autofix/kill-switch"


def _csrf(client: AsyncClient) -> dict[str, str]:
    """Extract CSRF token from client cookies (empty string when absent)."""
    csrf = client.cookies.get("homelab_monitor_csrf") or ""
    return {"X-CSRF-Token": csrf}


async def _seed_real_run(repo: SqliteRepository, *, run_id: str) -> None:
    """Seed a full FK chain runbooks -> alerts -> runbook_runs (mode=real) with
    a KNOWN run id, so a poked _CurrentRun(run_id=...) has a matching row for
    kill_inflight's UPDATE runbook_runs SET killed_at ... to affect."""
    rb = _make_runbook_record(alertname="TestAlert")
    await _insert_runbook(repo, rb)
    alert = _make_alert(alertname="TestAlert")
    await _insert_alert(repo, alert)
    runs_repo = RunbookRunsRepository(repo)
    async with repo.transaction() as conn:
        await conn.execute(
            text(
                "INSERT INTO runbook_runs "
                "(id, runbook_id, created_at, alert_id, mode, prompt, started_at, "
                " ended_at, fixer_user, host, runbook_hash, initiated_by) "
                "VALUES (:id, :rb_id, :ca, :alert_id, :mode, :prompt, :started, "
                " NULL, :fixer, :host, :hash, :initiated_by)"
            ),
            {
                "id": run_id,
                "rb_id": rb.id,
                "ca": "2026-01-01T00:00:00+00:00",
                "alert_id": alert.id,
                "mode": RunMode.REAL.value,
                "prompt": rb.path,
                "started": "2026-01-01T00:00:00+00:00",
                "fixer": "homelab-fixer",
                "host": "testhost",
                "hash": rb.content_hash,
                "initiated_by": "alert",
            },
        )
    _ = runs_repo  # constructed to mirror pattern; direct SQL used for a known id


@pytest.mark.asyncio
async def test_get_kill_switch_returns_current_state(
    authenticated_client: AsyncClient, repo: SqliteRepository
) -> None:
    """1: GET returns current state (enabled)."""
    app_settings = AppSettingsRepository(repo)
    await app_settings.set("autofix_enabled", "true")

    response = await authenticated_client.get(_URL)

    assert response.status_code == 200  # noqa: PLR2004
    data = response.json()
    assert data["enabled"] is True
    assert data["updated_at"] is not None


@pytest.mark.asyncio
async def test_get_kill_switch_unauthenticated_401(
    unauthenticated_client: AsyncClient,
) -> None:
    """2: GET without auth -> 401."""
    response = await unauthenticated_client.get(_URL)
    assert response.status_code == 401  # noqa: PLR2004


@pytest.mark.asyncio
async def test_toggle_unauthenticated_401(unauthenticated_client: AsyncClient) -> None:
    """3: POST without auth -> 401."""
    response = await unauthenticated_client.post(
        _URL, json={"enabled": False, "confirm_phrase": "disable auto-fix"}
    )
    assert response.status_code == 401  # noqa: PLR2004


@pytest.mark.asyncio
async def test_toggle_missing_csrf_403(authenticated_client: AsyncClient) -> None:
    """4: POST without X-CSRF-Token -> 403."""
    response = await authenticated_client.post(
        _URL, json={"enabled": False, "confirm_phrase": "disable auto-fix"}
    )
    assert response.status_code == 403  # noqa: PLR2004


@pytest.mark.asyncio
async def test_toggle_transition_missing_credential_returns_400(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    secrets_repo: AsyncSecretsRepository,
) -> None:
    """Toggling enabled state requires a credential; transition without one -> 400.

    Seed autofix_enabled=true, then attempt to disable (a transition) without
    confirm_phrase or confirm_pin -> 400 invalid_input.
    """
    app_settings = AppSettingsRepository(repo)
    await app_settings.set("autofix_enabled", "true")

    docker = _FakeDockerClient()
    orch = _make_orchestrator(repo, secrets_repo, docker)
    authenticated_client.app.state.autofix_orchestrator = orch  # type: ignore[attr-defined]

    response = await authenticated_client.post(
        _URL,
        json={"enabled": False},
        headers=_csrf(authenticated_client),
    )
    assert response.status_code == 400  # noqa: PLR2004
    assert response.json()["error"]["code"] == "invalid_input"


@pytest.mark.asyncio
async def test_toggle_extra_field_forbidden_422(authenticated_client: AsyncClient) -> None:
    """extra field -> 422 (ConfigDict extra=forbid)."""
    response = await authenticated_client.post(
        _URL,
        json={"enabled": False, "confirm_phrase": "disable auto-fix", "extra_field": "x"},
        headers=_csrf(authenticated_client),
    )
    assert response.status_code == 422  # noqa: PLR2004


@pytest.mark.asyncio
async def test_toggle_wrong_phrase_on_to_off_400(
    authenticated_client: AsyncClient, repo: SqliteRepository
) -> None:
    """6: on->off with wrong phrase -> 400, detail contains 'disable auto-fix'."""
    app_settings = AppSettingsRepository(repo)
    await app_settings.set("autofix_enabled", "true")

    response = await authenticated_client.post(
        _URL,
        json={"enabled": False, "confirm_phrase": "wrong phrase"},
        headers=_csrf(authenticated_client),
    )

    assert response.status_code == 400  # noqa: PLR2004
    assert "disable auto-fix" in response.json()["error"]["message"]


@pytest.mark.asyncio
async def test_toggle_wrong_phrase_off_to_on_400(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    secrets_repo: AsyncSecretsRepository,
) -> None:
    """7: off->on with wrong phrase -> 400, detail contains 'enable auto-fix'."""
    app_settings = AppSettingsRepository(repo)
    await app_settings.set("autofix_enabled", "false")

    docker = _FakeDockerClient()
    orch = _make_orchestrator(repo, secrets_repo, docker)
    authenticated_client.app.state.autofix_orchestrator = orch  # type: ignore[attr-defined]

    response = await authenticated_client.post(
        _URL,
        json={"enabled": True, "confirm_phrase": "wrong phrase"},
        headers=_csrf(authenticated_client),
    )

    assert response.status_code == 400  # noqa: PLR2004
    assert "enable auto-fix" in response.json()["error"]["message"]


@pytest.mark.asyncio
async def test_toggle_case_insensitive_confirm_accepts_upper_and_whitespace(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    secrets_repo: AsyncSecretsRepository,
) -> None:
    """8: 'DISABLE AUTO-FIX' and '  disable auto-fix  ' both succeed (on->off)."""
    app_settings = AppSettingsRepository(repo)
    await app_settings.set("autofix_enabled", "true")

    docker = _FakeDockerClient()
    orch = _make_orchestrator(repo, secrets_repo, docker)
    authenticated_client.app.state.autofix_orchestrator = orch  # type: ignore[attr-defined]

    response = await authenticated_client.post(
        _URL,
        json={"enabled": False, "confirm_phrase": "DISABLE AUTO-FIX"},
        headers=_csrf(authenticated_client),
    )
    assert response.status_code == 200  # noqa: PLR2004
    assert response.json()["enabled"] is False

    # Toggle back on to reset, then test whitespace variant off again.
    await app_settings.set("autofix_enabled", "true")
    response2 = await authenticated_client.post(
        _URL,
        json={"enabled": False, "confirm_phrase": "  disable auto-fix  "},
        headers=_csrf(authenticated_client),
    )
    assert response2.status_code == 200  # noqa: PLR2004
    assert response2.json()["enabled"] is False


@pytest.mark.asyncio
async def test_toggle_on_to_off_no_inflight_run(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    secrets_repo: AsyncSecretsRepository,
) -> None:
    """9: on->off, no in-flight run -> 200; killed_inflight_run_id is None;
    audit rows autofix.kill_switch_toggled + autofix.kill_no_inflight."""
    app_settings = AppSettingsRepository(repo)
    await app_settings.set("autofix_enabled", "true")

    docker = _FakeDockerClient()
    orch = _make_orchestrator(repo, secrets_repo, docker)
    authenticated_client.app.state.autofix_orchestrator = orch  # type: ignore[attr-defined]

    response = await authenticated_client.post(
        _URL,
        json={"enabled": False, "confirm_phrase": "disable auto-fix"},
        headers=_csrf(authenticated_client),
    )

    assert response.status_code == 200  # noqa: PLR2004
    data = response.json()
    assert data["enabled"] is False
    assert data["killed_inflight_run_id"] is None
    assert data["unwind_warning"] is None

    toggled = await repo.fetch_all(
        text("SELECT what FROM audit_log WHERE what = 'autofix.kill_switch_toggled'"), {}
    )
    assert len(toggled) == 1

    no_inflight = await repo.fetch_all(
        text("SELECT what FROM audit_log WHERE what = 'autofix.kill_no_inflight'"), {}
    )
    assert len(no_inflight) == 1


@pytest.mark.asyncio
async def test_toggle_off_to_on_no_kill_inflight_returns_200_and_audits(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    secrets_repo: AsyncSecretsRepository,
) -> None:
    """off->on: 200; killed_inflight_run_id/unwind_warning None; audit row
    autofix.kill_switch_toggled before=false/after=true; no autofix.killed row;
    app_settings persisted as enabled. Covers the FALSE branch of
    `if current_enabled and not target_enabled:`."""
    app_settings = AppSettingsRepository(repo)
    await app_settings.set("autofix_enabled", "false")

    docker = _FakeDockerClient()
    orch = _make_orchestrator(repo, secrets_repo, docker)
    authenticated_client.app.state.autofix_orchestrator = orch  # type: ignore[attr-defined]

    response = await authenticated_client.post(
        _URL,
        json={"enabled": True, "confirm_phrase": "enable auto-fix"},
        headers=_csrf(authenticated_client),
    )

    assert response.status_code == 200  # noqa: PLR2004
    data = response.json()
    assert data["enabled"] is True
    assert data["killed_inflight_run_id"] is None
    assert data["unwind_warning"] is None

    toggled = await repo.fetch_all(
        text(
            "SELECT before_json, after_json FROM audit_log "
            "WHERE what = 'autofix.kill_switch_toggled'"
        ),
        {},
    )
    assert len(toggled) == 1
    before = json.loads(str(toggled[0][0]))
    after = json.loads(str(toggled[0][1]))
    assert before == {"enabled": False}
    assert after == {"enabled": True, "credential_type": "phrase"}

    killed_rows = await repo.fetch_all(
        text("SELECT what FROM audit_log WHERE what = 'autofix.killed'"), {}
    )
    assert len(killed_rows) == 0

    stored = await app_settings.get("autofix_enabled")
    assert stored == "true"


@pytest.mark.asyncio
async def test_toggle_on_to_off_with_inflight_run(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    secrets_repo: AsyncSecretsRepository,
) -> None:
    """10: on->off with an in-flight run -> 200; killed_inflight_run_id == run_id;
    runbook_runs.killed_at set; audit rows autofix.kill_switch_toggled +
    autofix.killed. Exercises the REAL kill_inflight end-to-end via the router."""
    app_settings = AppSettingsRepository(repo)
    await app_settings.set("autofix_enabled", "true")

    run_id = "r-inflight-1"
    await _seed_real_run(repo, run_id=run_id)

    docker = _FakeDockerClient()
    orch = _make_orchestrator(repo, secrets_repo, docker)
    orch._current_run = _CurrentRun(  # pyright: ignore[reportPrivateUsage]
        run_id=run_id, container="test-fixer", started_at_monotonic=time.monotonic()
    )
    # Clear the handle as a side effect of the kill call so the poll's fast
    # path (post_snapshot is None) resolves immediately without a real sleep.
    docker.on_kill_call = lambda: setattr(orch, "_current_run", None)  # pyright: ignore[reportPrivateUsage]
    authenticated_client.app.state.autofix_orchestrator = orch  # type: ignore[attr-defined]

    response = await authenticated_client.post(
        _URL,
        json={"enabled": False, "confirm_phrase": "disable auto-fix"},
        headers=_csrf(authenticated_client),
    )

    assert response.status_code == 200  # noqa: PLR2004
    data = response.json()
    assert data["enabled"] is False
    assert data["killed_inflight_run_id"] == run_id
    assert data["unwind_warning"] is None

    run_row = await repo.fetch_one(
        text("SELECT killed_at FROM runbook_runs WHERE id = :id"), {"id": run_id}
    )
    assert run_row is not None
    assert run_row[0] is not None

    toggled = await repo.fetch_all(
        text("SELECT what FROM audit_log WHERE what = 'autofix.kill_switch_toggled'"), {}
    )
    assert len(toggled) == 1

    killed = await repo.fetch_all(
        text("SELECT after_json FROM audit_log WHERE what = 'autofix.killed'"), {}
    )
    assert len(killed) == 1
    after = json.loads(str(killed[0][0]))
    assert after["run_id"] == run_id


@pytest.mark.asyncio
async def test_toggle_on_to_off_docker_kill_fails_502(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    secrets_repo: AsyncSecretsRepository,
) -> None:
    """11: docker.kill_container raises -> 502; audit autofix.kill_failed."""
    app_settings = AppSettingsRepository(repo)
    await app_settings.set("autofix_enabled", "true")

    run_id = "r-inflight-2"
    await _seed_real_run(repo, run_id=run_id)

    docker = _FakeDockerClient(kill_raises=DockerSocketConnectionError("boom"))
    orch = _make_orchestrator(repo, secrets_repo, docker)
    orch._current_run = _CurrentRun(  # pyright: ignore[reportPrivateUsage]
        run_id=run_id, container="test-fixer", started_at_monotonic=time.monotonic()
    )
    authenticated_client.app.state.autofix_orchestrator = orch  # type: ignore[attr-defined]

    response = await authenticated_client.post(
        _URL,
        json={"enabled": False, "confirm_phrase": "disable auto-fix"},
        headers=_csrf(authenticated_client),
    )

    assert response.status_code == 502  # noqa: PLR2004

    kill_failed = await repo.fetch_all(
        text("SELECT what FROM audit_log WHERE what = 'autofix.kill_failed'"), {}
    )
    assert len(kill_failed) == 1

    # Kill-switch flag flip itself IS persisted before the kill attempt (the
    # toggle + audit happen before kill_inflight is invoked).
    state = await app_settings.get("autofix_enabled")
    assert state == "false"


@pytest.mark.asyncio
async def test_toggle_noop_returns_200_and_does_not_audit(
    authenticated_client: AsyncClient, repo: SqliteRepository
) -> None:
    """12: enabled already matches current state -> 200; NO audit row written.
    (Important #2 fix: a matching-state POST is treated as an idempotent
    "current state alias" equivalent to a GET, so it is never audited --
    otherwise any session could flood the audit table with garbage
    confirm_phrases via repeated no-op POSTs.)"""
    app_settings = AppSettingsRepository(repo)
    await app_settings.set("autofix_enabled", "true")

    response = await authenticated_client.post(
        _URL,
        json={"enabled": True, "confirm_phrase": "disable auto-fix"},
        headers=_csrf(authenticated_client),
    )

    assert response.status_code == 200  # noqa: PLR2004
    data = response.json()
    assert data["enabled"] is True
    assert data["killed_inflight_run_id"] is None
    assert data["unwind_warning"] is None

    audits = await repo.fetch_all(
        text("SELECT what FROM audit_log WHERE what = 'autofix.kill_switch_noop'"), {}
    )
    assert len(audits) == 0

    # Scope the "no audit rows" check to autofix events only — the
    # authenticated_client fixture writes user.create + session.login rows
    # during setup, which are unrelated to the noop-audit assertion.
    any_autofix_audits = await repo.fetch_all(
        text("SELECT what FROM audit_log WHERE what LIKE 'autofix.%'"), {}
    )
    assert len(any_autofix_audits) == 0


@pytest.mark.asyncio
async def test_toggle_unwind_deadline_exceeded_surfaces_warning(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    secrets_repo: AsyncSecretsRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """13: kill_inflight returns unwind_warning='unwind_deadline_exceeded' ->
    the response mirrors it. Uses a tiny deadline + never-clearing _current_run
    so the real kill_inflight poll times out fast."""
    monkeypatch.setattr(
        "homelab_monitor.kernel.autofix.orchestrator._KILL_UNWIND_DEADLINE_SECONDS", 0.05
    )
    monkeypatch.setattr(
        "homelab_monitor.kernel.autofix.orchestrator._KILL_UNWIND_POLL_INTERVAL_SECONDS", 0.01
    )

    app_settings = AppSettingsRepository(repo)
    await app_settings.set("autofix_enabled", "true")

    run_id = "r-inflight-3"
    await _seed_real_run(repo, run_id=run_id)

    docker = _FakeDockerClient()
    orch = _make_orchestrator(repo, secrets_repo, docker)
    # Never clear _current_run: the poll will always see it non-None and time out.
    orch._current_run = _CurrentRun(  # pyright: ignore[reportPrivateUsage]
        run_id=run_id, container="test-fixer", started_at_monotonic=time.monotonic()
    )
    authenticated_client.app.state.autofix_orchestrator = orch  # type: ignore[attr-defined]

    response = await authenticated_client.post(
        _URL,
        json={"enabled": False, "confirm_phrase": "disable auto-fix"},
        headers=_csrf(authenticated_client),
    )

    assert response.status_code == 200  # noqa: PLR2004
    data = response.json()
    assert data["killed_inflight_run_id"] == run_id
    assert data["unwind_warning"] == "unwind_deadline_exceeded"


@pytest.mark.asyncio
async def test_get_kill_switch_disabled_state(
    authenticated_client: AsyncClient, repo: SqliteRepository
) -> None:
    """GET returns disabled + updated_at None when never set (covers _is_truthy(None))."""
    response = await authenticated_client.get(_URL)

    assert response.status_code == 200  # noqa: PLR2004
    data = response.json()
    assert data["enabled"] is False
    assert data["updated_at"] is None


# ---------------------------------------------------------------------------
# STAGE-009-010B: confirm_pin as an alternative to confirm_phrase
# ---------------------------------------------------------------------------


async def _seed_pin(repo: SqliteRepository, pin: str = "1234") -> None:
    """Seed a PIN hash directly via app_settings (bypassing the security_pin router)."""
    app_settings = AppSettingsRepository(repo)
    await app_settings.set(PIN_HASH_KEY, hash_pin(pin, cost=4))


@pytest.mark.asyncio
async def test_kill_switch_pin_success(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    secrets_repo: AsyncSecretsRepository,
) -> None:
    """A PIN configured + correct confirm_pin toggles the kill switch."""
    await _seed_pin(repo, pin="1234")
    app_settings = AppSettingsRepository(repo)
    await app_settings.set("autofix_enabled", "true")

    docker = _FakeDockerClient()
    orch = _make_orchestrator(repo, secrets_repo, docker)
    authenticated_client.app.state.autofix_orchestrator = orch  # type: ignore[attr-defined]

    response = await authenticated_client.post(
        _URL,
        json={"enabled": False, "confirm_pin": "1234"},
        headers=_csrf(authenticated_client),
    )
    assert response.status_code == 200  # noqa: PLR2004
    assert response.json()["enabled"] is False


@pytest.mark.asyncio
async def test_kill_switch_audit_includes_credential_type(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    secrets_repo: AsyncSecretsRepository,
) -> None:
    """autofix.kill_switch_toggled after_json contains credential_type for
    both the phrase path and the pin path."""
    docker = _FakeDockerClient()
    orch = _make_orchestrator(repo, secrets_repo, docker)
    authenticated_client.app.state.autofix_orchestrator = orch  # type: ignore[attr-defined]

    # Phrase path: on->off.
    app_settings = AppSettingsRepository(repo)
    await app_settings.set("autofix_enabled", "true")
    resp_phrase = await authenticated_client.post(
        _URL,
        json={"enabled": False, "confirm_phrase": "disable auto-fix"},
        headers=_csrf(authenticated_client),
    )
    assert resp_phrase.status_code == 200  # noqa: PLR2004

    rows_phrase = await repo.fetch_all(
        text("SELECT after_json FROM audit_log WHERE what = 'autofix.kill_switch_toggled'"),
        {},
    )
    assert len(rows_phrase) == 1
    after_phrase = json.loads(rows_phrase[0][0])
    assert after_phrase == {"enabled": False, "credential_type": "phrase"}

    # PIN path: off->on.
    await _seed_pin(repo, pin="1234")
    resp_pin = await authenticated_client.post(
        _URL,
        json={"enabled": True, "confirm_pin": "1234"},
        headers=_csrf(authenticated_client),
    )
    assert resp_pin.status_code == 200  # noqa: PLR2004

    rows_pin = await repo.fetch_all(
        text(
            "SELECT after_json FROM audit_log WHERE what = 'autofix.kill_switch_toggled' "
            'ORDER BY "when"'
        ),
        {},
    )
    assert len(rows_pin) == 2  # noqa: PLR2004
    after_pin = json.loads(rows_pin[1][0])
    assert after_pin == {"enabled": True, "credential_type": "pin"}
