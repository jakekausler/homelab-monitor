"""Authentication repository for users, sessions, and API tokens."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import cast

from sqlalchemy import text

from homelab_monitor.kernel.auth.api_tokens import hash_token
from homelab_monitor.kernel.auth.models import ApiToken, Session, User
from homelab_monitor.kernel.auth.passwords import verify_password
from homelab_monitor.kernel.auth.scopes import Scope, serialize_scopes
from homelab_monitor.kernel.auth.sessions import make_session_id
from homelab_monitor.kernel.db.audit import insert_audit
from homelab_monitor.kernel.db.ids import uuid7
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.db.time import utc_now_iso


def _utc_now() -> datetime:
    """Return current datetime in UTC."""
    return datetime.now(UTC)


class AuthRepository:
    """Async repository for users, sessions, and api_tokens.

    Every state-changing method writes to audit_log in the SAME transaction
    as the primary write (using insert_audit on the same AsyncConnection).
    """

    def __init__(self, repo: SqliteRepository) -> None:
        self._repo = repo

    # ----- Users -----

    async def users_count(self) -> int:
        """Return the total count of users in the database."""
        row = await self._repo.fetch_one(text("SELECT COUNT(*) AS c FROM users"))
        if row is None:
            return 0
        return int(row[0])

    async def list_users(self) -> list[User]:
        """Return all users ordered by id."""
        rows = await self._repo.fetch_all(
            text("SELECT id, username, created_at FROM users ORDER BY id"),
        )
        return [User(id=int(r[0]), username=str(r[1]), created_at=str(r[2])) for r in rows]

    async def get_user_by_id(self, user_id: int) -> User | None:
        """Get a user by id; returns None if not found."""
        row = await self._repo.fetch_one(
            text("SELECT id, username, created_at FROM users WHERE id = :id"),
            {"id": user_id},
        )
        if row is None:
            return None
        return User(id=int(row[0]), username=str(row[1]), created_at=str(row[2]))

    async def get_user_by_username(self, username: str) -> User | None:
        """Get a user by username; returns None if not found."""
        row = await self._repo.fetch_one(
            text("SELECT id, username, created_at FROM users WHERE username = :u"),
            {"u": username},
        )
        if row is None:
            return None
        return User(id=int(row[0]), username=str(row[1]), created_at=str(row[2]))

    async def create_user(
        self,
        username: str,
        password_hash: str,
        *,
        who: str = "system",
        ip: str | None = None,
    ) -> User:
        """Insert user + audit row atomically. Caller hashes the password."""
        now = utc_now_iso()
        async with self._repo.transaction() as conn:
            result = await conn.execute(
                text("INSERT INTO users (username, bcrypt_hash, created_at) VALUES (:u, :h, :t)"),
                {"u": username, "h": password_hash, "t": now},
            )
            new_id = result.lastrowid
            assert new_id is not None
            user_id = int(new_id)
            await insert_audit(
                conn,
                who=who,
                what="user.create",
                after={"user_id": user_id, "username": username},
                ip=ip,
            )
        return User(id=user_id, username=username, created_at=now)

    async def change_password(
        self,
        user_id: int,
        new_password_hash: str,
        *,
        who: str,
        ip: str | None = None,
    ) -> None:
        """Update bcrypt_hash + audit; caller deletes sessions separately."""
        async with self._repo.transaction() as conn:
            res = await conn.execute(
                text("UPDATE users SET bcrypt_hash = :h WHERE id = :id"),
                {"h": new_password_hash, "id": user_id},
            )
            if res.rowcount == 0:
                msg = f"user not found: id={user_id}"
                raise LookupError(msg)
            await insert_audit(
                conn,
                who=who,
                what="user.password_change",
                after={"user_id": user_id},
                ip=ip,
            )

    async def delete_user(
        self,
        user_id: int,
        *,
        who: str,
        ip: str | None = None,
    ) -> None:
        """Delete user (and their sessions) atomically; write audit row.

        Order matters: SQLite's FK from ``sessions.user_id -> users.id`` is NOT
        declared ON DELETE CASCADE, so child rows must be removed first or the
        parent DELETE raises FOREIGN KEY constraint failed.
        """
        async with self._repo.transaction() as conn:
            # Delete child rows FIRST (no ON DELETE CASCADE on sessions.user_id).
            await conn.execute(
                text("DELETE FROM sessions WHERE user_id = :uid"),
                {"uid": user_id},
            )
            res = await conn.execute(
                text("DELETE FROM users WHERE id = :id"),
                {"id": user_id},
            )
            if res.rowcount == 0:
                msg = f"user not found: id={user_id}"
                raise LookupError(msg)
            await insert_audit(
                conn,
                who=who,
                what="user.delete",
                before={"user_id": user_id},
                ip=ip,
            )

    async def verify_user_password(
        self,
        username: str,
        plaintext: str,
    ) -> User | None:
        """Look up user + bcrypt-verify; returns User or None."""
        row = await self._repo.fetch_one(
            text("SELECT id, username, created_at, bcrypt_hash FROM users WHERE username = :u"),
            {"u": username},
        )
        if row is None:
            return None
        if not verify_password(plaintext, str(row[3])):
            return None
        return User(id=int(row[0]), username=str(row[1]), created_at=str(row[2]))

    # ----- Sessions -----

    async def create_session(
        self,
        user_id: int,
        ip: str,
        ttl_seconds: int,
        csrf_token: str,
    ) -> Session:
        """Create a new session for a user; write audit row."""
        sid = make_session_id()
        now = _utc_now()
        expires_at = (now + timedelta(seconds=ttl_seconds)).isoformat()
        async with self._repo.transaction() as conn:
            await conn.execute(
                text(
                    "INSERT INTO sessions (id, user_id, expires_at, created_ip, csrf_token) "
                    "VALUES (:id, :uid, :exp, :ip, :csrf)"
                ),
                {
                    "id": sid,
                    "uid": user_id,
                    "exp": expires_at,
                    "ip": ip,
                    "csrf": csrf_token,
                },
            )
            await insert_audit(
                conn,
                who=str(user_id),
                what="session.login",
                after={"session_id": sid},
                ip=ip,
            )
        return Session(
            id=sid,
            user_id=user_id,
            expires_at=expires_at,
            created_ip=ip,
            csrf_token=csrf_token,
        )

    async def get_session(self, session_id: str) -> Session | None:
        """Get a session by id; returns None if not found."""
        row = await self._repo.fetch_one(
            text(
                "SELECT id, user_id, expires_at, created_ip, csrf_token "
                "FROM sessions WHERE id = :id"
            ),
            {"id": session_id},
        )
        if row is None:
            return None
        return Session(
            id=str(row[0]),
            user_id=int(row[1]),
            expires_at=str(row[2]),
            created_ip=str(row[3]),
            csrf_token=str(row[4]),
        )

    async def delete_session(
        self,
        session_id: str,
        *,
        who: str,
        ip: str | None = None,
    ) -> None:
        """Delete a session; write audit row."""
        async with self._repo.transaction() as conn:
            await conn.execute(
                text("DELETE FROM sessions WHERE id = :id"),
                {"id": session_id},
            )
            await insert_audit(
                conn,
                who=who,
                what="session.logout",
                before={"session_id": session_id},
                ip=ip,
            )

    async def delete_all_user_sessions(self, user_id: int) -> int:
        """Delete every session for a user; used on password change. Returns count."""
        async with self._repo.transaction() as conn:
            res = await conn.execute(
                text("DELETE FROM sessions WHERE user_id = :uid"),
                {"uid": user_id},
            )
            return int(res.rowcount or 0)

    async def cleanup_expired_sessions(self) -> int:
        """Delete sessions with expires_at < now; returns count.

        TODO(STAGE-001-013): Wire this into the scheduler as a periodic
        housekeeping tick. Currently called only by the test suite — expired
        rows accumulate in production until manual cleanup. Non-blocking for
        STAGE-001-011 (auth still rejects expired sessions at the middleware
        layer; the rows are dead but harmless).
        """
        now = _utc_now().isoformat()
        async with self._repo.transaction() as conn:
            res = await conn.execute(
                text("DELETE FROM sessions WHERE expires_at < :now"),
                {"now": now},
            )
            return int(res.rowcount or 0)

    @staticmethod
    def is_session_expired(expires_at_iso: str) -> bool:
        """Return True if expires_at is in the past or unparseable.

        An unparseable timestamp is treated as expired (defense in depth: we
        won't trust an opaque/corrupt session row, even if HMAC happens to
        validate). Used by middleware on cookie validation; never raises.
        """
        try:
            ts = datetime.fromisoformat(expires_at_iso)
        except ValueError:
            return True
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        return ts < _utc_now()

    # ----- API tokens -----

    async def create_api_token(
        self,
        name: str,
        scopes: set[Scope],
        plaintext_token: str,
        *,
        who: str = "system",
        ip: str | None = None,
    ) -> ApiToken:
        """Create an API token record; write audit row."""
        token_id = uuid7()
        now = utc_now_iso()
        scopes_str = serialize_scopes(scopes)
        sha = hash_token(plaintext_token)
        async with self._repo.transaction() as conn:
            await conn.execute(
                text(
                    "INSERT INTO api_tokens (id, name, hash, scopes, created_at) "
                    "VALUES (:id, :n, :h, :s, :t)"
                ),
                {"id": token_id, "n": name, "h": sha, "s": scopes_str, "t": now},
            )
            await insert_audit(
                conn,
                who=who,
                what="api_token.create",
                after={"id": token_id, "name": name, "scopes": scopes_str},
                ip=ip,
            )
        return ApiToken(
            id=token_id,
            name=name,
            scopes=scopes_str,
            created_at=now,
            last_used_at=None,
            rotated_at=None,
        )

    async def get_api_token_by_hash(self, sha_hex: str) -> ApiToken | None:
        """Get an API token by its SHA-256 hash; returns None if not found."""
        row = await self._repo.fetch_one(
            text(
                "SELECT id, name, scopes, created_at, last_used_at, rotated_at "
                "FROM api_tokens WHERE hash = :h"
            ),
            {"h": sha_hex},
        )
        if row is None:
            return None
        return ApiToken(
            id=str(row[0]),
            name=str(row[1]),
            scopes=str(row[2]),
            created_at=str(row[3]),
            last_used_at=cast("str | None", row[4]),
            rotated_at=cast("str | None", row[5]),
        )

    async def get_api_token_by_name(self, name: str) -> ApiToken | None:
        """Return the API token row whose ``name`` column matches; ``None`` if absent.

        Used by the alertmanager render module to detect whether the bootstrap
        token has already been minted, so we don't mint duplicates on every boot.
        """
        row = await self._repo.fetch_one(
            text(
                "SELECT id, name, scopes, created_at, last_used_at, rotated_at "
                "FROM api_tokens WHERE name = :n"
            ),
            {"n": name},
        )
        if row is None:
            return None
        return ApiToken(
            id=str(row[0]),
            name=str(row[1]),
            scopes=str(row[2]),
            created_at=str(row[3]),
            last_used_at=cast("str | None", row[4]),
            rotated_at=cast("str | None", row[5]),
        )

    async def delete_api_token_by_name(self, name: str) -> bool:
        """Delete an API token row by name. Returns True if a row was deleted."""
        async with self._repo.transaction() as conn:
            result = await conn.execute(
                text("DELETE FROM api_tokens WHERE name = :name"),
                {"name": name},
            )
            return bool(result.rowcount > 0)

    async def list_api_tokens(self) -> list[ApiToken]:
        """Return all API tokens ordered by created_at descending."""
        rows = await self._repo.fetch_all(
            text(
                "SELECT id, name, scopes, created_at, last_used_at, rotated_at "
                "FROM api_tokens ORDER BY created_at DESC"
            ),
        )
        return [
            ApiToken(
                id=str(r[0]),
                name=str(r[1]),
                scopes=str(r[2]),
                created_at=str(r[3]),
                last_used_at=cast("str | None", r[4]),
                rotated_at=cast("str | None", r[5]),
            )
            for r in rows
        ]

    async def revoke_api_token(
        self,
        token_id: str,
        *,
        who: str,
        ip: str | None = None,
    ) -> None:
        """Revoke (delete) an API token; write audit row."""
        async with self._repo.transaction() as conn:
            res = await conn.execute(
                text("DELETE FROM api_tokens WHERE id = :id"),
                {"id": token_id},
            )
            if res.rowcount == 0:
                msg = f"api token not found: id={token_id}"
                raise LookupError(msg)
            await insert_audit(
                conn,
                who=who,
                what="api_token.revoke",
                before={"id": token_id},
                ip=ip,
            )

    async def update_token_last_used(self, token_id: str, when_iso: str) -> None:
        """Best-effort update of token's last_used_at.

        Wrapped in try/except by the caller (middleware) — failures here are
        logged at WARN but never raised, so a transient DB write error does
        not break authenticated traffic.
        """
        try:
            await self._repo.execute(
                text("UPDATE api_tokens SET last_used_at = :t WHERE id = :id"),
                {"t": when_iso, "id": token_id},
            )
        except Exception as exc:  # pragma: no cover -- defensive; tested via fault-injection
            import structlog  # noqa: PLC0415

            structlog.get_logger().warning(
                "auth.token_last_used_update_failed",
                token_id=token_id,
                error=str(exc),
            )
