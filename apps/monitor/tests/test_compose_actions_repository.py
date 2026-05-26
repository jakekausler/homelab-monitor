"""Tests for ComposeActionsRepository (STAGE-003-010)."""

from __future__ import annotations

import pytest

from homelab_monitor.kernel.db.repositories.compose_actions_repository import (
    ComposeActionsRepository,
)
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.db.time import utc_now_iso

EXPECTED_DURATION_SECONDS = 3.5
EXPECTED_LIST_LIMIT = 2


@pytest.mark.asyncio
async def test_insert_running_returns_id(repo: SqliteRepository) -> None:
    """insert_running returns a positive integer id."""
    r = ComposeActionsRepository(repo)
    action_id = await r.insert_running(
        action="pull_and_restart",
        container_name="caddy",
        compose_service="caddy",
        command="docker compose -f /x/docker-compose.yml pull caddy",
        started_at=utc_now_iso(),
        who="testuser",
        client_ip="127.0.0.1",
    )
    assert isinstance(action_id, int)
    assert action_id > 0


@pytest.mark.asyncio
async def test_get_by_id_returns_inserted_row(repo: SqliteRepository) -> None:
    """get_by_id returns the inserted row with state=pulling."""
    r = ComposeActionsRepository(repo)
    now = utc_now_iso()
    action_id = await r.insert_running(
        action="pull_and_restart",
        container_name="caddy",
        compose_service="caddy",
        command="docker compose pull caddy",
        started_at=now,
        who="alice",
        client_ip="10.0.0.5",
        before_image="caddy:latest",
        before_digest="sha256:abc",
    )
    row = await r.get_by_id(action_id)
    assert row is not None
    assert row.id == action_id
    assert row.state == "pulling"
    assert row.container_name == "caddy"
    assert row.who == "alice"
    assert row.client_ip == "10.0.0.5"
    assert row.before_image == "caddy:latest"
    assert row.before_digest == "sha256:abc"
    assert row.started_at == now
    assert row.ended_at is None


@pytest.mark.asyncio
async def test_get_by_id_missing_returns_none(repo: SqliteRepository) -> None:
    r = ComposeActionsRepository(repo)
    row = await r.get_by_id(99999)
    assert row is None


@pytest.mark.asyncio
async def test_update_terminal_state_success(repo: SqliteRepository) -> None:
    """update_terminal_state with state='success' updates row in place."""
    r = ComposeActionsRepository(repo)
    action_id = await r.insert_running(
        action="pull_and_restart",
        container_name="caddy",
        compose_service="caddy",
        command="cmd",
        started_at=utc_now_iso(),
        who="op",
        client_ip=None,
    )
    await r.update_terminal_state(
        action_id=action_id,
        state="success",
        stdout="ok\n",
        stderr="",
        exit_code=0,
        ended_at=utc_now_iso(),
        duration_seconds=3.5,
        after_image="caddy:latest",
        after_digest="sha256:def",
        audit_log_id="aud-xyz",
    )
    row = await r.get_by_id(action_id)
    assert row is not None
    assert row.state == "success"
    assert row.exit_code == 0
    assert row.stdout == "ok\n"
    assert row.stderr == ""
    assert row.duration_seconds == EXPECTED_DURATION_SECONDS
    assert row.after_image == "caddy:latest"
    assert row.after_digest == "sha256:def"
    assert row.audit_log_id == "aud-xyz"


@pytest.mark.asyncio
async def test_update_terminal_state_failed_with_error_reason(
    repo: SqliteRepository,
) -> None:
    r = ComposeActionsRepository(repo)
    action_id = await r.insert_running(
        action="pull_and_restart",
        container_name="caddy",
        compose_service="caddy",
        command="cmd",
        started_at=utc_now_iso(),
        who="op",
        client_ip=None,
    )
    await r.update_terminal_state(
        action_id=action_id,
        state="failed",
        stdout="",
        stderr="boom",
        exit_code=1,
        ended_at=utc_now_iso(),
        duration_seconds=0.1,
        error_reason="exit_nonzero",
    )
    row = await r.get_by_id(action_id)
    assert row is not None
    assert row.state == "failed"
    assert row.error_reason == "exit_nonzero"


@pytest.mark.asyncio
async def test_update_terminal_state_rejects_invalid_state(
    repo: SqliteRepository,
) -> None:
    r = ComposeActionsRepository(repo)
    action_id = await r.insert_running(
        action="pull_and_restart",
        container_name="x",
        compose_service="x",
        command="cmd",
        started_at=utc_now_iso(),
        who="op",
        client_ip=None,
    )
    with pytest.raises(ValueError, match="invalid terminal state"):
        await r.update_terminal_state(
            action_id=action_id,
            state="running",  # not terminal
            stdout=None,
            stderr=None,
            exit_code=None,
            ended_at=utc_now_iso(),
            duration_seconds=0.0,
        )


@pytest.mark.asyncio
async def test_list_for_container_orders_desc_by_started_at(
    repo: SqliteRepository,
) -> None:
    r = ComposeActionsRepository(repo)
    id1 = await r.insert_running(
        action="pull_and_restart",
        container_name="caddy",
        compose_service="caddy",
        command="cmd",
        started_at="2026-01-01T00:00:00+00:00",
        who="op",
        client_ip=None,
    )
    id2 = await r.insert_running(
        action="pull_and_restart",
        container_name="caddy",
        compose_service="caddy",
        command="cmd",
        started_at="2026-01-02T00:00:00+00:00",
        who="op",
        client_ip=None,
    )
    rows = await r.list_for_container(container_name="caddy", limit=10)
    assert [r.id for r in rows] == [id2, id1]


@pytest.mark.asyncio
async def test_list_for_container_filters_by_name(repo: SqliteRepository) -> None:
    r = ComposeActionsRepository(repo)
    await r.insert_running(
        action="pull_and_restart",
        container_name="caddy",
        compose_service="caddy",
        command="cmd",
        started_at=utc_now_iso(),
        who="op",
        client_ip=None,
    )
    await r.insert_running(
        action="pull_and_restart",
        container_name="nginx",
        compose_service="nginx",
        command="cmd",
        started_at=utc_now_iso(),
        who="op",
        client_ip=None,
    )
    caddy_rows = await r.list_for_container(container_name="caddy", limit=10)
    assert len(caddy_rows) == 1
    assert caddy_rows[0].container_name == "caddy"


@pytest.mark.asyncio
async def test_list_for_container_respects_limit(repo: SqliteRepository) -> None:
    r = ComposeActionsRepository(repo)
    for i in range(5):
        await r.insert_running(
            action="pull_and_restart",
            container_name="caddy",
            compose_service="caddy",
            command="cmd",
            started_at=f"2026-01-0{i + 1}T00:00:00+00:00",
            who="op",
            client_ip=None,
        )
    rows = await r.list_for_container(container_name="caddy", limit=EXPECTED_LIST_LIMIT)
    assert len(rows) == EXPECTED_LIST_LIMIT


@pytest.mark.asyncio
async def test_update_phase_invalid_raises_value_error(repo: SqliteRepository) -> None:
    """update_phase raises ValueError for states other than 'pulling'/'restarting'."""
    r = ComposeActionsRepository(repo)
    action_id = await r.insert_running(
        action="pull_and_restart",
        container_name="caddy",
        compose_service="caddy",
        command="cmd",
        started_at=utc_now_iso(),
        who="op",
        client_ip=None,
    )
    with pytest.raises(ValueError, match="invalid phase state"):
        await r.update_phase(action_id=action_id, phase="success")


@pytest.mark.asyncio
async def test_get_active_for_container_returns_row_when_in_flight(
    repo: SqliteRepository,
) -> None:
    """get_active_for_container returns the in-flight row when state is 'pulling'."""
    r = ComposeActionsRepository(repo)
    action_id = await r.insert_running(
        action="pull_and_restart",
        container_name="caddy",
        compose_service="caddy",
        command="cmd",
        started_at=utc_now_iso(),
        who="op",
        client_ip=None,
    )
    row = await r.get_active_for_container("caddy")
    assert row is not None
    assert row.id == action_id
    assert row.state == "pulling"


@pytest.mark.asyncio
async def test_get_active_for_container_returns_none_when_terminal(
    repo: SqliteRepository,
) -> None:
    """get_active_for_container returns None after action reaches a terminal state."""
    r = ComposeActionsRepository(repo)
    action_id = await r.insert_running(
        action="pull_and_restart",
        container_name="caddy",
        compose_service="caddy",
        command="cmd",
        started_at=utc_now_iso(),
        who="op",
        client_ip=None,
    )
    await r.update_terminal_state(
        action_id=action_id,
        state="success",
        stdout="",
        stderr="",
        exit_code=0,
        ended_at=utc_now_iso(),
        duration_seconds=1.0,
    )
    row = await r.get_active_for_container("caddy")
    assert row is None


@pytest.mark.asyncio
async def test_insert_running_building_state(repo: SqliteRepository) -> None:
    """insert_running with initial_state='building' creates a row in building state."""
    r = ComposeActionsRepository(repo)
    action_id = await r.insert_running(
        action="pull_and_restart",
        container_name="myapp",
        compose_service="myapp",
        command="docker compose build myapp",
        started_at=utc_now_iso(),
        who="alice",
        client_ip="10.0.0.1",
        initial_state="building",
    )
    row = await r.get_by_id(action_id)
    assert row is not None
    assert row.state == "building"
    assert row.container_name == "myapp"


@pytest.mark.asyncio
async def test_insert_running_invalid_state_raises(repo: SqliteRepository) -> None:
    """insert_running with invalid initial_state raises ValueError."""
    r = ComposeActionsRepository(repo)
    with pytest.raises(ValueError, match="invalid initial state"):
        await r.insert_running(
            action="pull_and_restart",
            container_name="x",
            compose_service="x",
            command="cmd",
            started_at=utc_now_iso(),
            who="op",
            client_ip=None,
            initial_state="invalid",
        )


@pytest.mark.asyncio
async def test_update_phase_allows_building(repo: SqliteRepository) -> None:
    """update_phase with phase='building' succeeds and updates row."""
    r = ComposeActionsRepository(repo)
    action_id = await r.insert_running(
        action="pull_and_restart",
        container_name="caddy",
        compose_service="caddy",
        command="cmd",
        started_at=utc_now_iso(),
        who="op",
        client_ip=None,
    )
    # Initially state is "pulling"
    row = await r.get_by_id(action_id)
    assert row is not None
    assert row.state == "pulling"

    # Update to "building"
    await r.update_phase(action_id=action_id, phase="building")
    row = await r.get_by_id(action_id)
    assert row is not None
    assert row.state == "building"


@pytest.mark.asyncio
async def test_get_active_for_container_includes_building_state(
    repo: SqliteRepository,
) -> None:
    """get_active_for_container includes rows with state='building'."""
    r = ComposeActionsRepository(repo)
    action_id = await r.insert_running(
        action="pull_and_restart",
        container_name="myapp",
        compose_service="myapp",
        command="cmd",
        started_at=utc_now_iso(),
        who="op",
        client_ip=None,
        initial_state="building",
    )
    row = await r.get_active_for_container("myapp")
    assert row is not None
    assert row.id == action_id
    assert row.state == "building"
