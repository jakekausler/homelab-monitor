"""Tests for AsyncSecretsRepository: CRUD + audit + rotate-master."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.secrets import repository as repo_mod
from homelab_monitor.kernel.secrets.errors import (
    SecretIntegrityError,
    SecretNotFoundError,
)
from homelab_monitor.kernel.secrets.repository import AsyncSecretsRepository


async def test_set_and_get_round_trip(secrets_repo: AsyncSecretsRepository) -> None:
    """A value set then fetched returns the original plaintext."""
    await secrets_repo.set("api-token", "hl-test-secret-v1-7c3f8b9a")
    assert await secrets_repo.get("api-token") == "hl-test-secret-v1-7c3f8b9a"


async def test_get_missing_returns_none(secrets_repo: AsyncSecretsRepository) -> None:
    """Unknown name yields None."""
    assert await secrets_repo.get("missing") is None


async def test_set_writes_audit_row(
    secrets_repo: AsyncSecretsRepository, repo: SqliteRepository
) -> None:
    """A successful ``set`` creates an audit_log row with metadata only."""
    await secrets_repo.set("api-token", "value")
    rows = await repo.fetch_all(
        text("SELECT what, after_json FROM audit_log WHERE what = :w"),
        {"w": "secrets.set"},
    )
    assert len(rows) == 1
    assert "api-token" in rows[0].after_json
    # Plaintext value MUST NOT appear anywhere in audit metadata.
    assert "value" not in (rows[0].after_json or "")


async def test_set_existing_acts_as_rotation(
    secrets_repo: AsyncSecretsRepository, repo: SqliteRepository
) -> None:
    """Calling ``set`` on an existing name updates ciphertext and writes secrets.rotate audit."""
    await secrets_repo.set("api-token", "old-value")
    first_rows = await repo.fetch_all(
        text("SELECT id, ciphertext FROM secrets WHERE name = :n"), {"n": "api-token"}
    )
    assert len(first_rows) == 1
    first_id = first_rows[0].id
    first_ct = first_rows[0].ciphertext

    await secrets_repo.set("api-token", "new-value")
    second_rows = await repo.fetch_all(
        text("SELECT id, ciphertext, rotated_at FROM secrets WHERE name = :n"),
        {"n": "api-token"},
    )
    assert len(second_rows) == 1
    assert second_rows[0].id == first_id  # same row id preserved
    assert second_rows[0].ciphertext != first_ct
    assert second_rows[0].rotated_at is not None

    audit = await repo.fetch_all(
        text('SELECT what FROM audit_log WHERE what IN ("secrets.set", "secrets.rotate")')
    )
    whats = [r.what for r in audit]
    assert "secrets.set" in whats
    assert "secrets.rotate" in whats

    assert await secrets_repo.get("api-token") == "new-value"


async def test_rotate_unknown_raises(secrets_repo: AsyncSecretsRepository) -> None:
    """``rotate`` on an unknown name raises SecretNotFoundError."""
    with pytest.raises(SecretNotFoundError):
        await secrets_repo.rotate("never-set", "v")


async def test_delete_removes_row_and_audits(
    secrets_repo: AsyncSecretsRepository, repo: SqliteRepository
) -> None:
    """``delete`` removes the row and writes a secrets.delete audit entry."""
    await secrets_repo.set("api-token", "v")
    await secrets_repo.delete("api-token")
    assert await secrets_repo.get("api-token") is None

    audit = await repo.fetch_all(
        text('SELECT what, before_json FROM audit_log WHERE what = "secrets.delete"')
    )
    assert len(audit) == 1
    assert "api-token" in audit[0].before_json


async def test_delete_unknown_raises(secrets_repo: AsyncSecretsRepository) -> None:
    """Deleting a missing secret raises SecretNotFoundError."""
    with pytest.raises(SecretNotFoundError):
        await secrets_repo.delete("never-existed")


async def test_list_names_returns_metadata_only(
    secrets_repo: AsyncSecretsRepository,
) -> None:
    """``list_names`` returns SecretMeta entries with name + timestamps; no value."""
    await secrets_repo.set("alpha", "value-a")
    await secrets_repo.set("beta", "value-b")

    metas = await secrets_repo.list_names()
    names = [m.name for m in metas]
    assert names == ["alpha", "beta"]  # ORDER BY name
    for m in metas:
        # SecretMeta has no `value` field; assert by attribute lookup.
        assert not hasattr(m, "value")
        assert m.created_at  # truthy ISO string
        assert m.rotated_at is None  # never rotated


async def test_rotate_master_re_encrypts_all(
    secrets_repo: AsyncSecretsRepository, repo: SqliteRepository
) -> None:
    """``rotate_master`` re-encrypts every row; old ciphertexts are gone."""
    await secrets_repo.set("alpha", "a-value")
    await secrets_repo.set("beta", "b-value")

    old_rows = await repo.fetch_all(
        text("SELECT name, ciphertext, kdf_salt FROM secrets ORDER BY name")
    )
    old_cts = {r.name: r.ciphertext for r in old_rows}

    new_master = bytes(range(32, 64))
    count = await secrets_repo.rotate_master(new_master)
    assert count == 2  # noqa: PLR2004

    secrets_repo.set_master_key(new_master)
    assert await secrets_repo.get("alpha") == "a-value"
    assert await secrets_repo.get("beta") == "b-value"

    new_rows = await repo.fetch_all(text("SELECT name, ciphertext FROM secrets ORDER BY name"))
    for r in new_rows:
        assert r.ciphertext != old_cts[r.name]


async def test_rotate_master_aborts_pre_flight_on_decrypt_failure(
    secrets_repo: AsyncSecretsRepository, repo: SqliteRepository
) -> None:
    """If any row fails to decrypt during the pre-flight pass, no row is updated.

    rotate_master decrypts every row OUTSIDE the transaction first (pre-flight),
    then opens a transaction to write all the new ciphertexts. This test pins
    the pre-flight abort: a corrupted row triggers SecretIntegrityError before
    any UPDATE runs, so no rows are partially rotated.
    """
    await secrets_repo.set("alpha", "a")
    await secrets_repo.set("beta", "b")

    # Corrupt beta's ciphertext directly in the DB (simulates wrong master / bit flip).
    await repo.execute(
        text("UPDATE secrets SET ciphertext = :ct WHERE name = :n"),
        {"ct": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA", "n": "beta"},
    )

    pre_alpha = await repo.fetch_one(
        text("SELECT ciphertext FROM secrets WHERE name = :n"), {"n": "alpha"}
    )
    assert pre_alpha is not None
    pre_alpha_ct = pre_alpha.ciphertext

    new_master = bytes(range(32, 64))
    with pytest.raises(SecretIntegrityError):
        await secrets_repo.rotate_master(new_master)

    # alpha must NOT have been re-encrypted (rollback / pre-flight abort).
    post_alpha = await repo.fetch_one(
        text("SELECT ciphertext FROM secrets WHERE name = :n"), {"n": "alpha"}
    )
    assert post_alpha is not None
    assert post_alpha.ciphertext == pre_alpha_ct


async def test_rotate_master_rejects_wrong_length(
    secrets_repo: AsyncSecretsRepository,
) -> None:
    """A non-32-byte new master raises SecretIntegrityError."""
    with pytest.raises(SecretIntegrityError):
        await secrets_repo.rotate_master(b"\x00" * 16)


async def test_set_master_key_rejects_wrong_length(
    secrets_repo: AsyncSecretsRepository,
) -> None:
    """``set_master_key`` validates length."""
    with pytest.raises(SecretIntegrityError):
        secrets_repo.set_master_key(b"\x00" * 16)


async def test_repo_property_round_trip(
    secrets_repo: AsyncSecretsRepository, repo: SqliteRepository
) -> None:
    """The ``repo`` property returns the underlying SqliteRepository."""
    assert isinstance(secrets_repo.repo, SqliteRepository)
    # Sanity: same engine.
    assert secrets_repo.repo.engine is repo.engine


async def test_get_corrupted_row_raises(
    secrets_repo: AsyncSecretsRepository, repo: SqliteRepository
) -> None:
    """Direct ciphertext corruption causes ``get`` to raise SecretIntegrityError."""
    await secrets_repo.set("api-token", "value")
    await repo.execute(
        text("UPDATE secrets SET ciphertext = :ct WHERE name = :n"),
        {"ct": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA", "n": "api-token"},
    )
    with pytest.raises(SecretIntegrityError):
        await secrets_repo.get("api-token")


async def test_snapshot_returns_all_decrypted(
    secrets_repo: AsyncSecretsRepository,
) -> None:
    """``snapshot`` decrypts every row into a SyncSecretsResolver."""
    await secrets_repo.set("alpha", "a-val")
    await secrets_repo.set("beta", "b-val")

    snap = await secrets_repo.snapshot()
    assert snap.get("alpha") == "a-val"
    assert snap.get("beta") == "b-val"
    assert snap.list_names() == ["alpha", "beta"]


async def test_set_duplicate_name_via_direct_insert_violates_unique(
    db_engine: AsyncEngine, secrets_repo: AsyncSecretsRepository
) -> None:
    """Direct ``INSERT`` of a duplicate-name row violates the UNIQUE index from migration 0002."""
    # First, set a secret normally so a row exists for "tok".
    await secrets_repo.set("tok", "value-1")

    # Try to insert a second row with the same name via raw SQL — should violate UNIQUE.
    async with db_engine.connect() as conn:
        with pytest.raises(IntegrityError):
            await conn.execute(
                text(
                    "INSERT INTO secrets (id, name, ciphertext, kdf_salt, created_at) "
                    "VALUES (:id, :name, :ct, :salt, :ts)"
                ),
                {
                    "id": "duplicate-id",
                    "name": "tok",
                    "ct": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
                    "salt": b"\x00" * 16,
                    "ts": "2026-05-05T00:00:00+00:00",
                },
            )
            await conn.commit()


async def test_audit_failure_rolls_back_data(
    secrets_repo: AsyncSecretsRepository,
    db_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If audit INSERT fails, the data INSERT must roll back too (atomicity invariant)."""

    async def _boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("simulated audit failure")

    monkeypatch.setattr(repo_mod, "_insert_audit", _boom)
    with pytest.raises(RuntimeError, match="simulated audit failure"):
        await secrets_repo.set("api-token", "v")

    # Verify no row was written despite the data INSERT having executed first.
    async with db_engine.connect() as conn:
        result = await conn.execute(
            text("SELECT id FROM secrets WHERE name = :n"), {"n": "api-token"}
        )
        rows = result.fetchall()
    assert rows == []


async def test_get_raises_integrity_error_on_invalid_base64(
    secrets_repo: AsyncSecretsRepository, db_engine: AsyncEngine
) -> None:
    """If row.ciphertext is corrupted to non-base64 garbage, get raises SecretIntegrityError."""
    await secrets_repo.set("tok", "v")
    async with db_engine.connect() as conn:
        await conn.execute(
            text("UPDATE secrets SET ciphertext = :ct WHERE name = :n"),
            {"ct": "!!! not base64 !!!", "n": "tok"},
        )
        await conn.commit()
    with pytest.raises(SecretIntegrityError, match="not valid base64"):
        await secrets_repo.get("tok")


async def test_rotate_master_raises_integrity_error_on_invalid_base64(
    secrets_repo: AsyncSecretsRepository, db_engine: AsyncEngine
) -> None:
    """rotate_master surfaces SecretIntegrityError when a row's ciphertext is non-base64."""
    await secrets_repo.set("tok", "v")
    async with db_engine.connect() as conn:
        await conn.execute(
            text("UPDATE secrets SET ciphertext = :ct WHERE name = :n"),
            {"ct": "!!! not base64 !!!", "n": "tok"},
        )
        await conn.commit()
    new_master = bytes(range(32, 64))
    with pytest.raises(SecretIntegrityError, match="not valid base64"):
        await secrets_repo.rotate_master(new_master)


async def test_snapshot_raises_integrity_error_on_invalid_base64(
    secrets_repo: AsyncSecretsRepository, db_engine: AsyncEngine
) -> None:
    """snapshot surfaces SecretIntegrityError when a row's ciphertext is non-base64."""
    await secrets_repo.set("tok", "v")
    async with db_engine.connect() as conn:
        await conn.execute(
            text("UPDATE secrets SET ciphertext = :ct WHERE name = :n"),
            {"ct": "!!! not base64 !!!", "n": "tok"},
        )
        await conn.commit()
    with pytest.raises(SecretIntegrityError, match="not valid base64"):
        await secrets_repo.snapshot()


async def test_current_fingerprint_is_stable(
    secrets_repo: AsyncSecretsRepository,
) -> None:
    """current_fingerprint() returns the same string on repeated calls with same key."""
    fp1 = secrets_repo.current_fingerprint()
    fp2 = secrets_repo.current_fingerprint()
    assert fp1 == fp2
    assert isinstance(fp1, str)
    assert len(fp1) == 64  # noqa: PLR2004  # SHA-256 hex string
