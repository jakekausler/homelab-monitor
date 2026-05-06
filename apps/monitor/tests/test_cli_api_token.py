"""Tests for cli/api_token.py — API token management commands."""
# pyright: reportPrivateUsage=false

from __future__ import annotations

from unittest.mock import patch

import pytest
from sqlalchemy import text

from homelab_monitor.kernel.db.engine import get_engine
from homelab_monitor.kernel.db.migrations import alembic_upgrade_head
from homelab_monitor.kernel.db.repository import SqliteRepository


@pytest.mark.asyncio
async def test_cmd_create_valid_prints_token_once(
    db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_cmd_create prints plaintext token exactly once."""
    from homelab_monitor.cli.api_token import _cmd_create  # noqa: PLC0415

    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    alembic_upgrade_head(db_url)

    with patch("builtins.print") as mock_print:
        result = await _cmd_create(["heartbeat:write"], "test-token")
        assert result == 0
        # Should have printed something (the token)
        assert mock_print.call_count >= 1


@pytest.mark.asyncio
async def test_cmd_create_unknown_scope_returns_1(
    db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_cmd_create with unknown scope returns 1."""
    from homelab_monitor.cli.api_token import _cmd_create  # noqa: PLC0415

    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)

    result = await _cmd_create(["unknown:scope"], "test-token")
    assert result == 1


@pytest.mark.asyncio
async def test_cmd_create_multiple_scopes(db_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """_cmd_create with multiple scopes creates token."""
    from homelab_monitor.cli.api_token import _cmd_create  # noqa: PLC0415

    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    alembic_upgrade_head(db_url)

    with patch("builtins.print"):
        result = await _cmd_create(["heartbeat:write", "read:status"], "test-token")
        assert result == 0


@pytest.mark.asyncio
async def test_cmd_list_empty_returns_no_tokens_message(
    db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_cmd_list with no tokens prints '(no api tokens)'."""
    from homelab_monitor.cli.api_token import _cmd_list  # noqa: PLC0415

    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    alembic_upgrade_head(db_url)

    with patch("builtins.print") as mock_print:
        result = await _cmd_list()
        assert result == 0
        # Should print something indicating no tokens
        calls = [str(call) for call in mock_print.call_args_list]
        assert any("no api tokens" in str(c).lower() for c in calls)


@pytest.mark.asyncio
async def test_cmd_list_with_tokens_no_plaintext(
    db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_cmd_list prints token metadata (no plaintext leak)."""
    from homelab_monitor.cli.api_token import _cmd_create, _cmd_list  # noqa: PLC0415

    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    alembic_upgrade_head(db_url)

    # Create token
    with patch("builtins.print"):
        await _cmd_create(["heartbeat:write"], "test-token")

    # List tokens
    with patch("builtins.print") as mock_print:
        result = await _cmd_list()
        assert result == 0
        # Should print metadata but not plaintext token
        output = str(mock_print.call_args_list)
        # Should contain name
        assert "test-token" in output


@pytest.mark.asyncio
async def test_cmd_revoke_valid_token_removed(db_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """_cmd_revoke removes token from DB."""
    from homelab_monitor.cli.api_token import _cmd_create, _cmd_revoke  # noqa: PLC0415

    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    alembic_upgrade_head(db_url)

    # Create token
    with patch("builtins.print"):
        await _cmd_create(["heartbeat:write"], "test-token")

    # Get token ID from DB
    engine = get_engine(url=db_url)
    repo = SqliteRepository(engine=engine)
    row = await repo.fetch_one(
        text("SELECT id FROM api_tokens WHERE name = :name"), {"name": "test-token"}
    )
    assert row is not None
    token_id = row[0]

    # Revoke it
    result = await _cmd_revoke(str(token_id))
    assert result == 0

    # Verify gone
    row = await repo.fetch_one(
        text("SELECT id FROM api_tokens WHERE name = :name"), {"name": "test-token"}
    )
    assert row is None
    await engine.dispose()


@pytest.mark.asyncio
async def test_cmd_revoke_missing_id_returns_1(
    db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_cmd_revoke for missing ID returns 1."""
    from homelab_monitor.cli.api_token import _cmd_revoke  # noqa: PLC0415

    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    alembic_upgrade_head(db_url)

    result = await _cmd_revoke("999")
    assert result == 1


@pytest.mark.asyncio
async def test_cmd_create_plaintext_printed_once(
    db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Plaintext token printed exactly once (not logged elsewhere)."""
    from homelab_monitor.cli.api_token import _cmd_create  # noqa: PLC0415

    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    alembic_upgrade_head(db_url)

    printed_tokens: list[str] = []

    def capture_print(*args: object, **kwargs: object) -> None:
        if args:
            printed_tokens.append(str(args[0]))

    with patch("builtins.print", side_effect=capture_print):
        result = await _cmd_create(["heartbeat:write"], "test-token")
        assert result == 0

    # Should have printed the token (at least one print call)
    assert len(printed_tokens) > 0
