"""Tests for cli/user.py — user management commands."""
# pyright: reportPrivateUsage=false

from __future__ import annotations

from unittest.mock import patch

import pytest
from sqlalchemy import text

from homelab_monitor.kernel.db.engine import get_engine
from homelab_monitor.kernel.db.migrations import alembic_upgrade_head
from homelab_monitor.kernel.db.repository import SqliteRepository


@pytest.mark.asyncio
async def test_cmd_create_valid_user_created(db_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """_cmd_create with matching password creates user and audit row."""
    from homelab_monitor.cli.user import _cmd_create  # noqa: PLC0415

    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    alembic_upgrade_head(db_url)

    # Mock getpass to return matching passwords
    with patch("homelab_monitor.cli.user.getpass.getpass") as mock_getpass:
        mock_getpass.side_effect = ["testpass1234", "testpass1234"]
        result = await _cmd_create("testuser")
        assert result == 0

    # Verify user exists
    engine = get_engine(url=db_url)
    repo = SqliteRepository(engine=engine)
    user = await repo.fetch_one(
        text("SELECT id, username FROM users WHERE username = :u"), {"u": "testuser"}
    )
    assert user is not None
    assert user[1] == "testuser"
    await engine.dispose()


@pytest.mark.asyncio
async def test_cmd_create_mismatched_passwords_returns_1(
    db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_cmd_create with mismatched passwords returns 1 (user not created)."""
    from homelab_monitor.cli.user import _cmd_create  # noqa: PLC0415

    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)

    with patch("homelab_monitor.cli.user.getpass.getpass") as mock_getpass:
        mock_getpass.side_effect = ["testpass1234", "differentpass"]
        result = await _cmd_create("testuser")
        assert result == 1

    # Verify user NOT created
    alembic_upgrade_head(db_url)
    engine = get_engine(url=db_url)
    repo = SqliteRepository(engine=engine)
    user = await repo.fetch_one(text("SELECT id FROM users WHERE username = :u"), {"u": "testuser"})
    assert user is None
    await engine.dispose()


@pytest.mark.asyncio
async def test_cmd_create_short_password_returns_1(
    db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_cmd_create with too-short password returns 1."""
    from homelab_monitor.cli.user import _cmd_create  # noqa: PLC0415

    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)

    with patch("homelab_monitor.cli.user.getpass.getpass") as mock_getpass:
        mock_getpass.side_effect = ["short", "short"]
        result = await _cmd_create("testuser")
        assert result == 1


@pytest.mark.asyncio
async def test_cmd_create_existing_user_returns_1(
    db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_cmd_create with existing username returns 1."""
    from homelab_monitor.cli.user import _cmd_create  # noqa: PLC0415

    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    alembic_upgrade_head(db_url)

    # Create first user
    with patch("homelab_monitor.cli.user.getpass.getpass") as mock_getpass:
        mock_getpass.side_effect = ["testpass1234", "testpass1234"]
        result = await _cmd_create("testuser")
        assert result == 0

    # Try to create same user again
    with patch("homelab_monitor.cli.user.getpass.getpass") as mock_getpass:
        mock_getpass.side_effect = ["testpass1234", "testpass1234"]
        result = await _cmd_create("testuser")
        assert result == 1


@pytest.mark.asyncio
async def test_cmd_list_empty_returns_no_users_message(
    db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_cmd_list with no users prints '(no users)'."""
    from homelab_monitor.cli.user import _cmd_list  # noqa: PLC0415

    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    alembic_upgrade_head(db_url)

    with patch("builtins.print") as mock_print:
        result = await _cmd_list()
        assert result == 0
        # Should print something indicating no users
        calls = [str(call) for call in mock_print.call_args_list]
        assert any("no users" in str(c).lower() for c in calls)


@pytest.mark.asyncio
async def test_cmd_list_with_users_no_hash(db_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """_cmd_list prints user rows without password hashes."""
    from homelab_monitor.cli.user import _cmd_create, _cmd_list  # noqa: PLC0415

    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    alembic_upgrade_head(db_url)

    # Create user
    with patch("homelab_monitor.cli.user.getpass.getpass") as mock_getpass:
        mock_getpass.side_effect = ["testpass1234", "testpass1234"]
        await _cmd_create("testuser")

    # List users
    with patch("builtins.print") as mock_print:
        result = await _cmd_list()
        assert result == 0
        # Should NOT print password hash
        output = str(mock_print.call_args_list)
        assert "testuser" in output


@pytest.mark.asyncio
async def test_cmd_passwd_valid_password_changed(
    db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_cmd_passwd changes user's password."""
    from homelab_monitor.cli.user import _cmd_create, _cmd_passwd  # noqa: PLC0415

    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    alembic_upgrade_head(db_url)

    # Create user
    with patch("homelab_monitor.cli.user.getpass.getpass") as mock_getpass:
        mock_getpass.side_effect = ["testpass1234", "testpass1234"]
        await _cmd_create("testuser")

    # Change password
    with patch("homelab_monitor.cli.user.getpass.getpass") as mock_getpass:
        mock_getpass.side_effect = ["newpassword12", "newpassword12"]
        result = await _cmd_passwd("testuser")
        assert result == 0


@pytest.mark.asyncio
async def test_cmd_passwd_missing_user_returns_1(
    db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_cmd_passwd for missing user returns 1."""
    from homelab_monitor.cli.user import _cmd_passwd  # noqa: PLC0415

    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    alembic_upgrade_head(db_url)

    with patch("homelab_monitor.cli.user.getpass.getpass") as mock_getpass:
        mock_getpass.side_effect = ["newpassword12", "newpassword12"]
        result = await _cmd_passwd("nobody")
        assert result == 1


@pytest.mark.asyncio
async def test_cmd_delete_valid_user_deleted(db_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """_cmd_delete removes user from DB."""
    from homelab_monitor.cli.user import _cmd_create, _cmd_delete  # noqa: PLC0415

    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    alembic_upgrade_head(db_url)

    # Create user
    with patch("homelab_monitor.cli.user.getpass.getpass") as mock_getpass:
        mock_getpass.side_effect = ["testpass1234", "testpass1234"]
        await _cmd_create("testuser")

    # Delete user
    result = await _cmd_delete("testuser")
    assert result == 0

    # Verify user gone
    engine = get_engine(url=db_url)
    repo = SqliteRepository(engine=engine)
    user = await repo.fetch_one(text("SELECT id FROM users WHERE username = :u"), {"u": "testuser"})
    assert user is None
    await engine.dispose()


@pytest.mark.asyncio
async def test_cmd_delete_missing_user_returns_1(
    db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_cmd_delete for missing user returns 1."""
    from homelab_monitor.cli.user import _cmd_delete  # noqa: PLC0415

    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    alembic_upgrade_head(db_url)

    result = await _cmd_delete("nobody")
    assert result == 1
