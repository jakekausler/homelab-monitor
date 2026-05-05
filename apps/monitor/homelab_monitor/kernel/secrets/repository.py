"""Async repository for the encrypted secrets store.

Thin layer over :class:`SqliteRepository` that encapsulates AES-GCM AEAD
encryption with HKDF per-row key derivation. Every mutation writes both the
data row and an audit row inside a single ``repo.transaction()`` block — the
audit INSERT is issued directly against the same connection (NOT via
``audit_write``) so a failed audit rolls back the secret op.
"""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from homelab_monitor.kernel.db.ids import uuid7
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.db.time import utc_now_iso
from homelab_monitor.kernel.secrets.crypto import decrypt, encrypt
from homelab_monitor.kernel.secrets.errors import (
    SecretIntegrityError,
    SecretNotFoundError,
)
from homelab_monitor.kernel.secrets.master_key import (
    EXPECTED_KEY_LEN,
    master_key_fingerprint,
)
from homelab_monitor.kernel.secrets.resolver import SyncSecretsResolver

AUDIT_INSERT = text(
    'INSERT INTO audit_log (id, who, what, "when", before_json, after_json, ip) '
    "VALUES (:id, :who, :what, :when, :before_json, :after_json, :ip)"
)


@dataclass(frozen=True)
class SecretMeta:
    """Metadata-only view of a secret row. Never includes the plaintext value."""

    # SCAFFOLDING: ``id`` is exposed for future audit cross-referencing and for the
    # admin UI in STAGE-001-014. Its UUIDv7 form encodes creation-time ordering, so
    # operators with list access can already infer creation time from ``created_at``;
    # leaking ``id`` adds no information beyond what ``created_at`` already exposes.
    id: str
    name: str
    created_at: str
    rotated_at: str | None


async def _insert_audit(  # noqa: PLR0913
    conn: AsyncConnection,
    *,
    who: str,
    what: str,
    before: dict[str, object] | None,
    after: dict[str, object] | None,
    ip: str | None = None,
) -> None:
    """Issue an audit INSERT against an EXISTING transaction's connection.

    Mirrors :func:`homelab_monitor.kernel.db.audit.audit_write` but reuses the
    caller's connection so the write participates in the surrounding
    transaction. Required by the Design lock: audit and data must be atomic.
    """
    await conn.execute(
        AUDIT_INSERT,
        {
            "id": uuid7(),
            "who": who,
            "what": what,
            "when": utc_now_iso(),
            "before_json": json.dumps(before) if before is not None else None,
            "after_json": json.dumps(after) if after is not None else None,
            "ip": ip,
        },
    )


class AsyncSecretsRepository:
    """High-level async API for the secrets store.

    Construct once per process boot with a :class:`SqliteRepository` and a
    32-byte master key. Mutations write encrypted data + audit row atomically.
    """

    def __init__(self, repo: SqliteRepository, master_key: bytes) -> None:
        self._repo = repo
        self._master = master_key

    @property
    def repo(self) -> SqliteRepository:
        """Underlying repository, exposed for tests and migration scripts."""
        return self._repo

    # ----- internal helpers -----

    async def _fetch_row(self, name: str) -> tuple[str, bytes, bytes] | None:
        """Return ``(id, salt, ciphertext_blob)`` for ``name`` or ``None``.

        ``ciphertext`` is base64-decoded back to raw ``nonce||ct`` bytes.
        """
        row = await self._repo.fetch_one(
            text("SELECT id, kdf_salt, ciphertext FROM secrets WHERE name = :name"),
            {"name": name},
        )
        if row is None:
            return None
        try:
            ct_blob = base64.b64decode(row.ciphertext)
        except (binascii.Error, ValueError) as exc:
            raise SecretIntegrityError("ciphertext is not valid base64") from exc
        return row.id, bytes(row.kdf_salt), ct_blob

    # ----- mutating operations -----

    async def set(self, name: str, value: str, *, who: str = "system") -> None:
        """Insert OR replace the secret named ``name`` with ``value``.

        If a row already exists, this acts as a rotation: same row id is
        preserved, ciphertext + salt are replaced, ``rotated_at`` is bumped.
        Audit row is written in the same transaction.

        Note: Idempotent calls (setting an existing name to the same plaintext value)
        still write a new audit row because the ciphertext rotates (new nonce + salt +
        key derivation). The audit log reflects ciphertext writes, not just plaintext
        value changes.
        """
        plaintext = value.encode("utf-8")
        async with self._repo.transaction() as conn:
            existing = (
                await conn.execute(
                    text("SELECT id FROM secrets WHERE name = :name"),
                    {"name": name},
                )
            ).first()
            if existing is None:
                row_id = uuid7()
                salt, blob = encrypt(self._master, plaintext, row_id)
                await conn.execute(
                    text(
                        "INSERT INTO secrets "
                        "(id, name, created_at, ciphertext, kdf_salt, rotated_at) "
                        "VALUES (:id, :name, :ts, :ct, :salt, NULL)"
                    ),
                    {
                        "id": row_id,
                        "name": name,
                        "ts": utc_now_iso(),
                        "ct": base64.b64encode(blob).decode("ascii"),
                        "salt": salt,
                    },
                )
                await _insert_audit(
                    conn,
                    who=who,
                    what="secrets.set",
                    before=None,
                    after={"name": name},
                )
            else:
                row_id = existing.id
                salt, blob = encrypt(self._master, plaintext, row_id)
                await conn.execute(
                    text(
                        "UPDATE secrets SET ciphertext = :ct, kdf_salt = :salt, "
                        "rotated_at = :ts WHERE id = :id"
                    ),
                    {
                        "id": row_id,
                        "ct": base64.b64encode(blob).decode("ascii"),
                        "salt": salt,
                        "ts": utc_now_iso(),
                    },
                )
                await _insert_audit(
                    conn,
                    who=who,
                    what="secrets.rotate",
                    before={"name": name},
                    after={"name": name},
                )

    async def rotate(self, name: str, value: str, *, who: str = "system") -> None:
        """Rotate the value of an existing secret.

        Distinct from :meth:`set` only in that it errors if the secret does
        not yet exist (``SecretNotFoundError``).
        """
        existing = await self._fetch_row(name)
        if existing is None:
            raise SecretNotFoundError(name)
        await self.set(name, value, who=who)

    async def delete(self, name: str, *, who: str = "system") -> None:
        """Delete a secret by name. No-op error if it doesn't exist.

        Raises :class:`SecretNotFoundError` if absent. Audit row is written in
        the same transaction.
        """
        async with self._repo.transaction() as conn:
            existing = (
                await conn.execute(
                    text("SELECT id FROM secrets WHERE name = :name"),
                    {"name": name},
                )
            ).first()
            if existing is None:
                raise SecretNotFoundError(name)
            await conn.execute(
                text("DELETE FROM secrets WHERE name = :name"),
                {"name": name},
            )
            await _insert_audit(
                conn,
                who=who,
                what="secrets.delete",
                before={"name": name},
                after=None,
            )

    async def rotate_master(self, new_master: bytes, *, who: str = "system") -> int:
        """Re-encrypt every secret under a new master key. Returns count.

        Atomic across all rows: if ANY row fails to decrypt under the current
        master, the whole rotation rolls back and the original ``MasterKeyError``
        / :class:`SecretIntegrityError` propagates.

        After successful rotation, the caller is responsible for swapping the
        in-memory master via :meth:`set_master_key`. The repository does not
        update its own ``_master`` automatically — the new master is a
        parameter, not a side effect.

        Note: ``rotated_at`` is NOT updated by this operation. The audit_log
        captures the rotation event.
        """
        if len(new_master) != EXPECTED_KEY_LEN:
            raise SecretIntegrityError("new master key must be 32 bytes")

        rows = await self._repo.fetch_all(
            text("SELECT id, name, kdf_salt, ciphertext FROM secrets")
        )
        # Decrypt every row OUTSIDE the transaction first; any failure aborts
        # before we touch the DB. This means transient partial state cannot
        # leak even if the writer fails halfway through.
        plaintexts: list[tuple[str, bytes]] = []
        for row in rows:
            try:
                blob = base64.b64decode(row.ciphertext)
            except (binascii.Error, ValueError) as exc:
                raise SecretIntegrityError("ciphertext is not valid base64") from exc
            pt = decrypt(self._master, bytes(row.kdf_salt), blob, row.id)
            plaintexts.append((row.id, pt))

        # Re-encrypt + persist atomically.
        async with self._repo.transaction() as conn:
            for row_id, pt in plaintexts:
                new_salt, new_blob = encrypt(new_master, pt, row_id)
                await conn.execute(
                    text("UPDATE secrets SET ciphertext = :ct, kdf_salt = :salt WHERE id = :id"),
                    {
                        "id": row_id,
                        "ct": base64.b64encode(new_blob).decode("ascii"),
                        "salt": new_salt,
                    },
                )
            await _insert_audit(
                conn,
                who=who,
                what="secrets.rotate_master",
                before={"row_count": len(plaintexts)},
                after={"row_count": len(plaintexts)},
            )
        return len(plaintexts)

    def current_fingerprint(self) -> str:
        """Return the HMAC fingerprint of the in-memory master key.

        Used by the CLI's ``rotate-master`` command to display the old key's
        fingerprint without exposing the bytes via ``_master``.
        """
        return master_key_fingerprint(self._master)

    def set_master_key(self, master_key: bytes) -> None:
        """Replace the in-memory master key (used after :meth:`rotate_master`)."""
        if len(master_key) != EXPECTED_KEY_LEN:
            raise SecretIntegrityError("master key must be 32 bytes")
        self._master = master_key

    # ----- read-only operations -----

    async def get(self, name: str) -> str | None:
        """Return the plaintext for ``name`` or ``None`` if absent.

        Raises :class:`SecretIntegrityError` if the row exists but its
        ciphertext fails AEAD verification.
        """
        existing = await self._fetch_row(name)
        if existing is None:
            return None
        row_id, salt, blob = existing
        plaintext = decrypt(self._master, salt, blob, row_id)
        return plaintext.decode("utf-8")

    async def list_names(self) -> list[SecretMeta]:
        """Return metadata for every secret. NEVER returns plaintext."""
        rows = await self._repo.fetch_all(
            text("SELECT id, name, created_at, rotated_at FROM secrets ORDER BY name")
        )
        return [
            SecretMeta(
                id=row.id, name=row.name, created_at=row.created_at, rotated_at=row.rotated_at
            )
            for row in rows
        ]

    async def snapshot(self) -> SyncSecretsResolver:
        """Decrypt every secret and return a frozen sync resolver.

        Used at plugin-spawn time (STAGE-001-009) and at FastAPI app startup
        for sync read paths. Holds plaintext in memory — caller is responsible
        for lifetime.
        """
        rows = await self._repo.fetch_all(
            text("SELECT id, name, kdf_salt, ciphertext FROM secrets")
        )
        values: dict[str, str] = {}
        for row in rows:
            try:
                blob = base64.b64decode(row.ciphertext)
            except (binascii.Error, ValueError) as exc:
                raise SecretIntegrityError("ciphertext is not valid base64") from exc
            pt = decrypt(self._master, bytes(row.kdf_salt), blob, row.id)
            values[row.name] = pt.decode("utf-8")
        return SyncSecretsResolver(_values=values)
