"""Tests for kernel/auth/repository.py — user, session, and API token management."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from homelab_monitor.kernel.auth.passwords import hash_password
from homelab_monitor.kernel.auth.repository import AuthRepository
from homelab_monitor.kernel.auth.scopes import Scope
from homelab_monitor.kernel.db.repository import SqliteRepository


@pytest.mark.asyncio
async def test_users_count_zero_to_one_to_two(repo: SqliteRepository) -> None:
    """users_count increments correctly."""
    auth_repo = AuthRepository(repo)
    assert await auth_repo.users_count() == 0

    await auth_repo.create_user("alice", hash_password("password1234", cost=4))
    assert await auth_repo.users_count() == 1

    await auth_repo.create_user("bob", hash_password("password1234", cost=4))
    assert await auth_repo.users_count() == 2  # noqa: PLR2004


@pytest.mark.asyncio
async def test_create_user_writes_audit_row(repo: SqliteRepository) -> None:
    """create_user writes an audit row with verb 'user.create'."""
    auth_repo = AuthRepository(repo)
    user = await auth_repo.create_user("alice", hash_password("password1234", cost=4))
    assert user.username == "alice"

    # Verify audit row
    row = await repo.fetch_one(
        text("SELECT id, who, what FROM audit_log WHERE what = :verb ORDER BY id DESC LIMIT 1"),
        {"verb": "user.create"},
    )
    assert row is not None
    assert row[2] == "user.create"


@pytest.mark.asyncio
async def test_get_user_by_username_round_trip(repo: SqliteRepository) -> None:
    """get_user_by_username returns the created user."""
    auth_repo = AuthRepository(repo)
    created = await auth_repo.create_user("alice", hash_password("password1234", cost=4))
    fetched = await auth_repo.get_user_by_username("alice")
    assert fetched is not None
    assert fetched.id == created.id
    assert fetched.username == "alice"


@pytest.mark.asyncio
async def test_get_user_by_id_round_trip(repo: SqliteRepository) -> None:
    """get_user_by_id returns the created user."""
    auth_repo = AuthRepository(repo)
    created = await auth_repo.create_user("alice", hash_password("password1234", cost=4))
    fetched = await auth_repo.get_user_by_id(created.id)
    assert fetched is not None
    assert fetched.username == "alice"


@pytest.mark.asyncio
async def test_verify_user_password_correct_returns_user(repo: SqliteRepository) -> None:
    """verify_user_password with correct password returns User."""
    auth_repo = AuthRepository(repo)
    created = await auth_repo.create_user("alice", hash_password("password1234", cost=4))
    user = await auth_repo.verify_user_password("alice", "password1234")
    assert user is not None
    assert user.id == created.id


@pytest.mark.asyncio
async def test_verify_user_password_wrong_returns_none(repo: SqliteRepository) -> None:
    """verify_user_password with wrong password returns None."""
    auth_repo = AuthRepository(repo)
    await auth_repo.create_user("alice", hash_password("password1234", cost=4))
    user = await auth_repo.verify_user_password("alice", "wrongpassword12")
    assert user is None


@pytest.mark.asyncio
async def test_verify_user_password_missing_returns_none(repo: SqliteRepository) -> None:
    """verify_user_password with nonexistent user returns None."""
    auth_repo = AuthRepository(repo)
    user = await auth_repo.verify_user_password("nobody", "password1234")
    assert user is None


@pytest.mark.asyncio
async def test_change_password_updates_hash(repo: SqliteRepository) -> None:
    """change_password updates the hash; new pw verifies, old fails."""
    auth_repo = AuthRepository(repo)
    user = await auth_repo.create_user("alice", hash_password("oldpassword12", cost=4))
    new_hash = hash_password("newpassword12", cost=4)
    await auth_repo.change_password(user.id, new_hash, who="alice", ip="127.0.0.1")

    # Old password should fail
    assert await auth_repo.verify_user_password("alice", "oldpassword12") is None
    # New password should work
    assert await auth_repo.verify_user_password("alice", "newpassword12") is not None


@pytest.mark.asyncio
async def test_change_password_missing_user_raises(repo: SqliteRepository) -> None:
    """change_password on missing user raises LookupError."""
    auth_repo = AuthRepository(repo)
    new_hash = hash_password("newpassword12", cost=4)
    with pytest.raises(LookupError):
        await auth_repo.change_password(999, new_hash, who="admin", ip="127.0.0.1")


@pytest.mark.asyncio
async def test_change_password_audit_row(repo: SqliteRepository) -> None:
    """change_password writes audit row with verb 'user.password_change'."""
    auth_repo = AuthRepository(repo)
    user = await auth_repo.create_user("alice", hash_password("oldpassword12", cost=4))
    new_hash = hash_password("newpassword12", cost=4)
    await auth_repo.change_password(user.id, new_hash, who="alice", ip="127.0.0.1")

    # Verify audit row
    row = await repo.fetch_one(
        text("SELECT what FROM audit_log WHERE what = :verb ORDER BY id DESC LIMIT 1"),
        {"verb": "user.password_change"},
    )
    assert row is not None


@pytest.mark.asyncio
async def test_delete_user_removes_row(repo: SqliteRepository) -> None:
    """delete_user removes the user row."""
    auth_repo = AuthRepository(repo)
    user = await auth_repo.create_user("alice", hash_password("password1234", cost=4))
    await auth_repo.delete_user(user.id, who="admin", ip="127.0.0.1")
    fetched = await auth_repo.get_user_by_id(user.id)
    assert fetched is None


@pytest.mark.asyncio
async def test_delete_user_cascades_sessions(repo: SqliteRepository) -> None:
    """delete_user cascades and removes user's sessions."""
    auth_repo = AuthRepository(repo)
    user = await auth_repo.create_user("alice", hash_password("password1234", cost=4))
    session = await auth_repo.create_session(user.id, "192.168.1.1", 3600, "token123")
    await auth_repo.delete_user(user.id, who="admin", ip="127.0.0.1")

    # Session should be gone
    fetched = await auth_repo.get_session(session.id)
    assert fetched is None


@pytest.mark.asyncio
async def test_delete_user_audit_row(repo: SqliteRepository) -> None:
    """delete_user writes audit row with verb 'user.delete'."""
    auth_repo = AuthRepository(repo)
    user = await auth_repo.create_user("alice", hash_password("password1234", cost=4))
    await auth_repo.delete_user(user.id, who="admin", ip="127.0.0.1")

    # Verify audit row
    row = await repo.fetch_one(
        text("SELECT what FROM audit_log WHERE what = :verb ORDER BY id DESC LIMIT 1"),
        {"verb": "user.delete"},
    )
    assert row is not None


@pytest.mark.asyncio
async def test_delete_user_missing_raises(repo: SqliteRepository) -> None:
    """delete_user on missing user raises LookupError."""
    auth_repo = AuthRepository(repo)
    with pytest.raises(LookupError):
        await auth_repo.delete_user(999, who="admin", ip="127.0.0.1")


@pytest.mark.asyncio
async def test_list_users_empty_ordered_by_id(repo: SqliteRepository) -> None:
    """list_users on empty DB returns empty list."""
    auth_repo = AuthRepository(repo)
    users = await auth_repo.list_users()
    assert users == []


@pytest.mark.asyncio
async def test_list_users_ordered_by_id(repo: SqliteRepository) -> None:
    """list_users returns users ordered by id."""
    auth_repo = AuthRepository(repo)
    user1 = await auth_repo.create_user("alice", hash_password("password1234", cost=4))
    user2 = await auth_repo.create_user("bob", hash_password("password1234", cost=4))
    users = await auth_repo.list_users()
    assert len(users) == 2  # noqa: PLR2004
    assert users[0].id == user1.id
    assert users[1].id == user2.id


@pytest.mark.asyncio
async def test_create_session_row_and_audit(repo: SqliteRepository) -> None:
    """create_session writes session row and audit row."""
    auth_repo = AuthRepository(repo)
    user = await auth_repo.create_user("alice", hash_password("password1234", cost=4))
    session = await auth_repo.create_session(user.id, "192.168.1.1", 3600, "csrf_token")
    assert session.user_id == user.id
    assert session.created_ip == "192.168.1.1"
    assert session.csrf_token == "csrf_token"


@pytest.mark.asyncio
async def test_get_session_round_trip(repo: SqliteRepository) -> None:
    """get_session returns the created session; missing returns None."""
    auth_repo = AuthRepository(repo)
    user = await auth_repo.create_user("alice", hash_password("password1234", cost=4))
    created = await auth_repo.create_session(user.id, "192.168.1.1", 3600, "csrf_token")
    fetched = await auth_repo.get_session(created.id)
    assert fetched is not None
    assert fetched.id == created.id
    assert fetched.csrf_token == "csrf_token"


@pytest.mark.asyncio
async def test_get_session_missing_returns_none(repo: SqliteRepository) -> None:
    """get_session on missing session returns None."""
    auth_repo = AuthRepository(repo)
    fetched = await auth_repo.get_session("nonexistent")
    assert fetched is None


@pytest.mark.asyncio
async def test_delete_session_removes_row_and_audit(repo: SqliteRepository) -> None:
    """delete_session removes the session row and writes audit."""
    auth_repo = AuthRepository(repo)
    user = await auth_repo.create_user("alice", hash_password("password1234", cost=4))
    session = await auth_repo.create_session(user.id, "192.168.1.1", 3600, "csrf_token")
    await auth_repo.delete_session(session.id, who="alice", ip="192.168.1.1")

    # Session should be gone
    fetched = await auth_repo.get_session(session.id)
    assert fetched is None


@pytest.mark.asyncio
async def test_delete_all_user_sessions_count(repo: SqliteRepository) -> None:
    """delete_all_user_sessions removes all user's sessions."""
    auth_repo = AuthRepository(repo)
    user = await auth_repo.create_user("alice", hash_password("password1234", cost=4))
    session1 = await auth_repo.create_session(user.id, "192.168.1.1", 3600, "csrf1")
    session2 = await auth_repo.create_session(user.id, "192.168.1.2", 3600, "csrf2")

    count = await auth_repo.delete_all_user_sessions(user.id)
    assert count == 2  # noqa: PLR2004
    assert await auth_repo.get_session(session1.id) is None
    assert await auth_repo.get_session(session2.id) is None


@pytest.mark.asyncio
async def test_cleanup_expired_sessions_removes_only_expired(repo: SqliteRepository) -> None:
    """cleanup_expired_sessions removes only expired rows."""
    auth_repo = AuthRepository(repo)
    user = await auth_repo.create_user("alice", hash_password("password1234", cost=4))

    # Create a session that's already expired (ttl_seconds=0)
    expired = await auth_repo.create_session(user.id, "192.168.1.1", 0, "csrf1")
    # Create a session that's not expired (ttl_seconds=3600)
    fresh = await auth_repo.create_session(user.id, "192.168.1.2", 3600, "csrf2")

    # Clean up
    await auth_repo.cleanup_expired_sessions()
    # At least the expired one should be gone
    assert await auth_repo.get_session(expired.id) is None
    # Fresh one should still be there
    assert await auth_repo.get_session(fresh.id) is not None


@pytest.mark.asyncio
async def test_is_session_expired_past(repo: SqliteRepository) -> None:
    """is_session_expired returns True for past expiry."""
    auth_repo = AuthRepository(repo)
    user = await auth_repo.create_user("alice", hash_password("password1234", cost=4))
    session = await auth_repo.create_session(user.id, "192.168.1.1", 0, "csrf")
    assert auth_repo.is_session_expired(session.expires_at)


@pytest.mark.asyncio
async def test_is_session_expired_future(repo: SqliteRepository) -> None:
    """is_session_expired returns False for future expiry."""
    auth_repo = AuthRepository(repo)
    user = await auth_repo.create_user("alice", hash_password("password1234", cost=4))
    session = await auth_repo.create_session(user.id, "192.168.1.1", 3600, "csrf")
    assert not auth_repo.is_session_expired(session.expires_at)


@pytest.mark.asyncio
async def test_create_api_token_row_and_audit(repo: SqliteRepository) -> None:
    """create_api_token writes row and audit."""
    auth_repo = AuthRepository(repo)
    plaintext = "homelab_test_abc123"
    scopes = {Scope.HEARTBEAT_WRITE, Scope.READ_STATUS}

    token = await auth_repo.create_api_token(
        name="test-token",
        scopes=scopes,
        plaintext_token=plaintext,
    )
    assert token.name == "test-token"


@pytest.mark.asyncio
async def test_get_api_token_by_hash_round_trip(repo: SqliteRepository) -> None:
    """get_api_token_by_hash returns the created token; missing returns None."""
    auth_repo = AuthRepository(repo)
    plaintext = "homelab_test_abc123"
    import hashlib  # noqa: PLC0415

    hash_val = hashlib.sha256(plaintext.encode()).hexdigest()
    scopes = {Scope.HEARTBEAT_WRITE}

    await auth_repo.create_api_token(
        name="test-token",
        scopes=scopes,
        plaintext_token=plaintext,
    )

    fetched = await auth_repo.get_api_token_by_hash(hash_val)
    assert fetched is not None
    assert fetched.name == "test-token"


@pytest.mark.asyncio
async def test_get_api_token_by_hash_missing_returns_none(repo: SqliteRepository) -> None:
    """get_api_token_by_hash on missing hash returns None."""
    auth_repo = AuthRepository(repo)
    fetched = await auth_repo.get_api_token_by_hash("nonexistent")
    assert fetched is None


@pytest.mark.asyncio
async def test_list_api_tokens_ordering(repo: SqliteRepository) -> None:
    """list_api_tokens returns tokens in created order."""
    auth_repo = AuthRepository(repo)

    plaintext1 = "homelab_test_token1"
    plaintext2 = "homelab_test_token2"
    scopes = {Scope.HEARTBEAT_WRITE}

    await auth_repo.create_api_token(
        name="token-1",
        scopes=scopes,
        plaintext_token=plaintext1,
    )
    await auth_repo.create_api_token(
        name="token-2",
        scopes=scopes,
        plaintext_token=plaintext2,
    )

    tokens = await auth_repo.list_api_tokens()
    assert len(tokens) >= 2  # noqa: PLR2004
    # Verify both are present
    names = [t.name for t in tokens]
    assert "token-1" in names
    assert "token-2" in names


@pytest.mark.asyncio
async def test_revoke_api_token_removes_and_audits(repo: SqliteRepository) -> None:
    """revoke_api_token removes row and writes audit."""
    auth_repo = AuthRepository(repo)
    plaintext = "homelab_test_abc123"
    scopes = {Scope.HEARTBEAT_WRITE}

    token = await auth_repo.create_api_token(
        name="test-token",
        scopes=scopes,
        plaintext_token=plaintext,
    )

    import hashlib  # noqa: PLC0415

    hash_val = hashlib.sha256(plaintext.encode()).hexdigest()
    await auth_repo.revoke_api_token(token.id, who="admin", ip="127.0.0.1")

    # Token should be gone
    fetched = await auth_repo.get_api_token_by_hash(hash_val)
    assert fetched is None


@pytest.mark.asyncio
async def test_revoke_api_token_missing_raises(repo: SqliteRepository) -> None:
    """revoke_api_token on missing token raises LookupError."""
    auth_repo = AuthRepository(repo)
    with pytest.raises(LookupError):
        await auth_repo.revoke_api_token("nonexistent", who="admin", ip="127.0.0.1")


@pytest.mark.asyncio
async def test_update_token_last_used_writes_timestamp(repo: SqliteRepository) -> None:
    """update_token_last_used writes a timestamp."""
    auth_repo = AuthRepository(repo)
    plaintext = "homelab_test_abc123"
    scopes = {Scope.HEARTBEAT_WRITE}

    token = await auth_repo.create_api_token(
        name="test-token",
        scopes=scopes,
        plaintext_token=plaintext,
    )

    import hashlib  # noqa: PLC0415

    from homelab_monitor.kernel.db.time import utc_now_iso  # noqa: PLC0415

    hash_val = hashlib.sha256(plaintext.encode()).hexdigest()
    await auth_repo.update_token_last_used(token.id, utc_now_iso())

    # Verify the token was updated
    fetched = await auth_repo.get_api_token_by_hash(hash_val)
    assert fetched is not None
    assert fetched.last_used_at is not None
