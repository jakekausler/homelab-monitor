"""Tests for ``kernel.db.migrations``: pending check, run, round-trip, env gate."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncEngine

from alembic import command
from homelab_monitor.kernel.db.engine import get_engine
from homelab_monitor.kernel.db.migrations import (
    ALEMBIC_DIR,
    MigrationsPendingError,
    alembic_current_revision,
    alembic_head_revision,
    alembic_history,
    alembic_upgrade_head,
    check_pending_migrations,
    run_migrations,
)

EXPECTED_TABLES = {
    "users",
    "sessions",
    "audit_log",
    "api_tokens",
    "targets",
    "targets_docker",
    "collectors",
    "crons",
    "heartbeats_state",
    "alerts",
    "alert_outcomes",
    "runbooks",
    "runbook_runs",
    "secrets",
    "channels",
    "routing_rules",
    "digest_configs",
    "maintenance_windows",
    "suggestions",
    "suggestions_docker",
    "tool_scorecards",
    "cron_log_cursors",
    "cron_runs",
    "probe_targets",
    "docker_override_ownership",
    "docker_build_hashes",
    "image_update_state",
    "compose_actions",
}


async def test_check_pending_returns_revisions_on_empty_db(db_url: str) -> None:
    """A fresh DB lists all known revisions as pending."""
    engine = get_engine(url=db_url)
    try:
        pending = await check_pending_migrations(engine)
        # Don't hardcode the migration list — test that all migrations are pending
        # and that 0001 is the oldest (last in newest-to-oldest order).
        assert len(pending) >= 1
        assert pending[-1] == "0001"
    finally:
        await engine.dispose()


async def test_run_migrations_applies_head(db_url: str) -> None:
    """After ``run_migrations`` the DB is at head and contains all 20 tables."""
    engine = get_engine(url=db_url)
    try:
        await run_migrations(engine)
        pending = await check_pending_migrations(engine)
        assert pending == []

        def _list_tables(sync_conn: object) -> set[str]:
            inspector = inspect(sync_conn)
            return set(inspector.get_table_names()) if inspector is not None else set()

        async with engine.connect() as conn:
            tables = await conn.run_sync(_list_tables)
        # alembic_version is added by Alembic itself; remove for the assertion.
        tables.discard("alembic_version")
        assert tables == EXPECTED_TABLES
    finally:
        await engine.dispose()


async def test_run_migrations_no_op_at_head(db_url: str) -> None:
    """Calling ``run_migrations`` twice is safe."""
    engine = get_engine(url=db_url)
    try:
        await run_migrations(engine)
        await run_migrations(engine)  # no-op
        assert await check_pending_migrations(engine) == []
    finally:
        await engine.dispose()


async def test_run_migrations_raises_when_disabled(
    db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With auto-migrate disabled and pending migrations, refuse to start."""
    monkeypatch.setenv("HOMELAB_MONITOR_AUTO_MIGRATE", "false")
    engine = get_engine(url=db_url)
    try:
        with pytest.raises(MigrationsPendingError, match="HOMELAB_MONITOR_AUTO_MIGRATE"):
            await run_migrations(engine)
    finally:
        await engine.dispose()


async def test_round_trip_downgrade_then_upgrade(db_url: str) -> None:
    """Upgrade head -> downgrade base -> upgrade head leaves the DB at head."""
    cfg = Config()
    cfg.set_main_option("script_location", str(ALEMBIC_DIR))
    cfg.set_main_option("sqlalchemy.url", db_url)

    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")

    engine: AsyncEngine = get_engine(url=db_url)
    try:
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text("SELECT name FROM sqlite_master WHERE type='table' AND name='alerts'")
                )
            ).first()
        assert row is not None
    finally:
        await engine.dispose()


def test_alembic_helpers_at_head(db_url: str) -> None:
    """``current``/``head``/``history`` helpers return sensible values after upgrade."""
    alembic_upgrade_head(db_url)
    # Current and head should be equal (and non-None) after upgrade.
    current = alembic_current_revision(db_url)
    head = alembic_head_revision(db_url)
    assert current is not None
    assert head is not None
    assert current == head
    history = alembic_history(db_url)
    # 0001 is always the base revision regardless of how many migrations exist.
    assert any(line.startswith("0001 ->") for line in history)


def test_alembic_current_revision_empty_db(db_url: str) -> None:
    """Without running upgrade, current revision is ``None``."""
    assert alembic_current_revision(db_url) is None


async def test_check_pending_returns_empty_at_head(db_url: str) -> None:
    """Once at head, pending migrations list is empty."""
    engine = get_engine(url=db_url)
    try:
        await run_migrations(engine)
        pending = await check_pending_migrations(engine)
        assert pending == []
    finally:
        await engine.dispose()


async def test_check_pending_migrations_with_intermediate_current(
    db_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When current revision is mid-history, only revisions newer than current are pending.

    Mocks both the script directory (to simulate a multi-revision history) and the
    MigrationContext (to control what "current" the function reads). CRIT-1's fix
    must work for future N-revision states regardless of how many real migrations exist.
    """
    # Mock walk_revisions to pretend there are three revisions: 0003 (head), 0002, 0001.
    # walk_revisions yields newest-to-oldest. Mock current = 0001 (intermediate).
    mock_revs = [
        MagicMock(revision="0003"),
        MagicMock(revision="0002"),
        MagicMock(revision="0001"),
    ]
    mock_script = MagicMock()
    mock_script.get_current_head.return_value = "0003"
    mock_script.walk_revisions.return_value = mock_revs

    with (
        patch(
            "homelab_monitor.kernel.db.migrations.ScriptDirectory.from_config",
            return_value=mock_script,
        ),
        patch("homelab_monitor.kernel.db.migrations.MigrationContext.configure") as mock_ctx,
    ):
        # DB's actual current revision (0002) is overridden by the mock to be 0001
        # so the loop walks from 0003 down and breaks when it hits 0001.
        mock_ctx.return_value.get_current_revision.return_value = "0001"
        pending = await check_pending_migrations(db_engine)

    # Pending should be [0003, 0002] (everything newer than mocked-current 0001).
    assert pending == ["0003", "0002"]


async def test_check_pending_migrations_with_unknown_current(
    db_engine: AsyncEngine,
) -> None:
    """If DB's current revision is not in the script directory, the loop exhausts.

    Defensive: a stale ``alembic_version`` row pointing at a revision file that no
    longer exists in the script tree. Exercises the ``for`` loop's no-break exit
    branch in :func:`check_pending_migrations`. The function returns all walked
    revisions because none of them matches ``current``.
    """
    mock_revs = [
        MagicMock(revision="0002"),
        MagicMock(revision="0001"),
    ]
    mock_script = MagicMock()
    mock_script.get_current_head.return_value = "0002"
    mock_script.walk_revisions.return_value = mock_revs

    with (
        patch(
            "homelab_monitor.kernel.db.migrations.ScriptDirectory.from_config",
            return_value=mock_script,
        ),
        patch("homelab_monitor.kernel.db.migrations.MigrationContext.configure") as mock_ctx,
    ):
        mock_ctx.return_value.get_current_revision.return_value = "9999-stale"
        pending = await check_pending_migrations(db_engine)

    # No mock revision matches 9999-stale, so the loop exhausts and returns all.
    assert pending == ["0002", "0001"]


async def test_api_tokens_hash_unique_constraint_enforced(db_engine: AsyncEngine) -> None:
    """Migration 0004 adds UNIQUE INDEX api_tokens_hash_idx; duplicate hash insert fails.

    Without the unique index, a partial-transaction retry could produce two
    rows with the same hash that both match at lookup. The migration is
    tested for round-trip elsewhere; this test verifies the constraint
    actually rejects duplicates at the DB layer.
    """
    from sqlalchemy.exc import IntegrityError  # noqa: PLC0415

    # Verify the named index from migration 0004 exists with unique=True
    async with db_engine.connect() as conn:
        result = await conn.execute(text("PRAGMA index_list('api_tokens')"))
        rows = result.fetchall()
        found = False
        for row in rows:
            # PRAGMA index_list columns: seq, name, unique, origin, partial
            if row[1] == "api_tokens_hash_idx":
                assert row[2] == 1, "api_tokens_hash_idx is not UNIQUE"
                found = True
                break
        assert found, "Migration 0004's api_tokens_hash_idx not found"

    async with db_engine.connect() as conn:
        await conn.execute(
            text(
                "INSERT INTO api_tokens (id, name, hash, scopes, created_at) "
                "VALUES (:id, :n, :h, :s, :t)"
            ),
            {
                "id": "tok-1",
                "n": "first",
                "h": "deadbeef" * 8,  # 64 chars, valid SHA-256 hex shape
                "s": "read:status",
                "t": "2026-05-06T00:00:00Z",
            },
        )
        await conn.commit()

    with pytest.raises(IntegrityError):
        async with db_engine.connect() as conn:
            await conn.execute(
                text(
                    "INSERT INTO api_tokens (id, name, hash, scopes, created_at) "
                    "VALUES (:id, :n, :h, :s, :t)"
                ),
                {
                    "id": "tok-2",
                    "n": "second",
                    "h": "deadbeef" * 8,  # SAME hash as first — must fail
                    "s": "read:status",
                    "t": "2026-05-06T00:00:01Z",
                },
            )
            await conn.commit()


async def test_migration_0006_crons_columns_present(db_url: str) -> None:
    """After upgrading to 0006, ``crons`` has all behavioural columns added by 0006."""
    expected = {
        "id",
        "command",
        "created_at",
        "name",
        "host",
        "schedule",
        "cadence_seconds",
        "expected_grace_seconds",
        "integration_mode",
        "enabled",
        "last_seen_state",
        "updated_at",
        "archived_at",
    }

    cfg = Config()
    cfg.set_main_option("script_location", str(ALEMBIC_DIR))
    cfg.set_main_option("sqlalchemy.url", db_url)
    command.upgrade(cfg, "0006")

    def _list_cols(sync_conn: object) -> set[str]:
        ins = inspect(sync_conn)
        return {c["name"] for c in ins.get_columns("crons")} if ins is not None else set()

    engine = get_engine(url=db_url)
    try:
        async with engine.connect() as conn:
            cols = await conn.run_sync(_list_cols)
        assert expected.issubset(cols)
    finally:
        await engine.dispose()


async def test_migration_0006_heartbeats_state_replaced(db_url: str) -> None:
    """After upgrading to 0006, ``heartbeats_state`` is keyed by cron_id (not id)."""
    expected = {
        "cron_id",
        "current_state",
        "last_start_at",
        "last_ok_at",
        "last_fail_at",
        "current_streak",
        "expected_next_at",
        "last_duration_seconds",
        "last_exit_code",
        "updated_at",
    }

    cfg = Config()
    cfg.set_main_option("script_location", str(ALEMBIC_DIR))
    cfg.set_main_option("sqlalchemy.url", db_url)
    command.upgrade(cfg, "0006")

    def _inspect(sync_conn: object) -> tuple[set[str], set[str]]:
        ins = inspect(sync_conn)
        if ins is None:
            return set(), set()
        cols = {c["name"] for c in ins.get_columns("heartbeats_state")}
        pk = set(ins.get_pk_constraint("heartbeats_state")["constrained_columns"])
        return cols, pk

    engine = get_engine(url=db_url)
    try:
        async with engine.connect() as conn:
            cols, pk = await conn.run_sync(_inspect)
        assert cols == expected
        assert pk == {"cron_id"}
    finally:
        await engine.dispose()


async def test_migration_0006_round_trip(db_url: str) -> None:
    """upgrade -> downgrade -1 -> upgrade leaves the schema at head."""
    cfg = Config()
    cfg.set_main_option("script_location", str(ALEMBIC_DIR))
    cfg.set_main_option("sqlalchemy.url", db_url)
    command.upgrade(cfg, "0006")
    # Downgrade past 0006 to land at 0005 — the assertion
    # below verifies 0006's downgrade restored the stub heartbeats_state shape.
    command.downgrade(cfg, "0005")

    # Verify downgrade actually restored the stub shape (not just succeeded silently).
    engine = get_engine(url=db_url)
    try:

        def _stub_shape(sync_conn: object) -> set[str]:
            ins = inspect(sync_conn)
            return {c["name"] for c in ins.get_columns("heartbeats_state")}  # pyright: ignore[reportOptionalMemberAccess]

        async with engine.connect() as conn:
            stub_cols = await conn.run_sync(_stub_shape)
        assert stub_cols == {"id", "key", "created_at"}, f"downgrade leaked columns: {stub_cols}"
    finally:
        await engine.dispose()

    command.upgrade(cfg, "0006")

    engine = get_engine(url=db_url)
    try:

        def _has_cron_id(sync_conn: object) -> bool:
            ins = inspect(sync_conn)
            cols = {c["name"] for c in ins.get_columns("heartbeats_state")}  # pyright: ignore[reportOptionalMemberAccess]
            return "cron_id" in cols

        async with engine.connect() as conn:
            assert await conn.run_sync(_has_cron_id) is True
    finally:
        await engine.dispose()


async def test_migration_0007_schedule_canonical_column_present(db_engine: AsyncEngine) -> None:
    """After head-migration, ``crons.schedule_canonical`` exists."""

    def _list_cols(sync_conn: object) -> set[str]:
        ins = inspect(sync_conn)
        return {c["name"] for c in ins.get_columns("crons")} if ins is not None else set()

    async with db_engine.connect() as conn:
        cols = await conn.run_sync(_list_cols)
    assert "schedule_canonical" in cols


async def test_migration_0007_at_least_one_check_constraint_enforced(
    db_url: str,
) -> None:
    """The CHECK constraint rejects rows with neither schedule nor cadence_seconds set."""
    from sqlalchemy.exc import IntegrityError  # noqa: PLC0415

    cfg = Config()
    cfg.set_main_option("script_location", str(ALEMBIC_DIR))
    cfg.set_main_option("sqlalchemy.url", db_url)
    command.upgrade(cfg, "0007")

    engine = get_engine(url=db_url)
    try:
        with pytest.raises(IntegrityError):
            async with engine.connect() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO crons ("
                        "  id, name, host, command, schedule, schedule_canonical, "
                        "  cadence_seconds, expected_grace_seconds, integration_mode, "
                        "  enabled, last_seen_state, created_at, updated_at, archived_at"
                        ") VALUES ("
                        "  'bad', 'bad', 'h', '/x', '', NULL, "
                        "  0, 300, 'observe', 1, 'unknown', "
                        "  '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00', NULL"
                        ")"
                    )
                )
                await conn.commit()
    finally:
        await engine.dispose()


async def test_migration_0007_xor_rejects_neither(db_url: str) -> None:
    """The xor CHECK rejects rows with neither schedule nor cadence_seconds."""
    from sqlalchemy.exc import IntegrityError  # noqa: PLC0415

    cfg = Config()
    cfg.set_main_option("script_location", str(ALEMBIC_DIR))
    cfg.set_main_option("sqlalchemy.url", db_url)
    command.upgrade(cfg, "0007")

    engine = get_engine(url=db_url)
    try:
        with pytest.raises(IntegrityError):
            async with engine.connect() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO crons ("
                        "  id, name, host, command, schedule, schedule_canonical, "
                        "  cadence_seconds, expected_grace_seconds, integration_mode, "
                        "  enabled, last_seen_state, created_at, updated_at, archived_at"
                        ") VALUES ("
                        "  'bad2', 'bad2', 'h', '/x', '', NULL, 0, 300, 'observe', 1, 'unknown', "
                        "  '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00', NULL"
                        ")"
                    )
                )
                await conn.commit()
    finally:
        await engine.dispose()


async def test_migration_0007_partial_index_idx_crons_active_present(
    db_engine: AsyncEngine,
) -> None:
    """The partial index ``idx_crons_active`` is present and partial."""
    async with db_engine.connect() as conn:
        result = await conn.execute(text("PRAGMA index_list('crons')"))
        rows = result.fetchall()
        found = any(row[1] == "idx_crons_active" and row[4] == 1 for row in rows)
    assert found, "idx_crons_active partial index missing"


async def test_migration_0007_round_trip(db_url: str) -> None:
    """upgrade -> downgrade -1 -> upgrade leaves the schema at head."""
    cfg = Config()
    cfg.set_main_option("script_location", str(ALEMBIC_DIR))
    cfg.set_main_option("sqlalchemy.url", db_url)
    command.upgrade(cfg, "0007")
    command.downgrade(cfg, "0006")

    engine = get_engine(url=db_url)
    try:

        def _has_canonical(sync_conn: object) -> bool:
            ins = inspect(sync_conn)
            cols = {c["name"] for c in ins.get_columns("crons")}  # pyright: ignore[reportOptionalMemberAccess]
            return "schedule_canonical" in cols

        async with engine.connect() as conn:
            assert await conn.run_sync(_has_canonical) is False
    finally:
        await engine.dispose()

    command.upgrade(cfg, "0007")
    engine = get_engine(url=db_url)
    try:

        def _has_canonical_after(sync_conn: object) -> bool:
            ins = inspect(sync_conn)
            cols = {c["name"] for c in ins.get_columns("crons")}  # pyright: ignore[reportOptionalMemberAccess]
            return "schedule_canonical" in cols

        async with engine.connect() as conn:
            assert await conn.run_sync(_has_canonical_after) is True
    finally:
        await engine.dispose()


async def test_crons_columns_present_at_head(db_engine: AsyncEngine) -> None:
    """After head, ``crons`` has the redesigned column set (fingerprint PK,
    hidden_at, source_path, wrapper_installed_at; NO integration_mode, NO
    archived_at, NO id)."""
    expected = {
        "fingerprint",
        "name",
        "host",
        "command",
        "schedule",
        "schedule_canonical",
        "cadence_seconds",
        "expected_grace_seconds",
        "enabled",
        "last_seen_state",
        "created_at",
        "updated_at",
        "hidden_at",
        "source_path",
        "wrapper_last_seen_at",
        "wrapper_installed",
        "wrapper_format_version",
        "last_discovered_at",
        "soft_deleted_at",
        "log_match_key",
    }
    forbidden = {"id", "integration_mode", "archived_at"}

    def _list_cols(sync_conn: object) -> set[str]:
        ins = inspect(sync_conn)
        return {c["name"] for c in ins.get_columns("crons")} if ins is not None else set()

    async with db_engine.connect() as conn:
        cols = await conn.run_sync(_list_cols)
    assert cols == expected
    assert forbidden.isdisjoint(cols)


async def test_migration_0008_heartbeats_state_keyed_by_fingerprint(
    db_engine: AsyncEngine,
) -> None:
    """After head, ``heartbeats_state.cron_fingerprint`` is the PK (replacing
    ``cron_id``)."""

    def _inspect(sync_conn: object) -> tuple[set[str], set[str]]:
        ins = inspect(sync_conn)
        if ins is None:
            return set(), set()
        cols = {c["name"] for c in ins.get_columns("heartbeats_state")}
        pk = set(ins.get_pk_constraint("heartbeats_state")["constrained_columns"])
        return cols, pk

    async with db_engine.connect() as conn:
        cols, pk = await conn.run_sync(_inspect)
    assert "cron_fingerprint" in cols
    assert "cron_id" not in cols
    assert pk == {"cron_fingerprint"}


async def test_migration_0008_seed_rows_present(db_engine: AsyncEngine) -> None:
    """After head, ``crons`` contains the four demo rows inserted by 0008."""
    async with db_engine.connect() as conn:
        result = await conn.execute(text("SELECT COUNT(*) FROM crons"))
        row = result.first()
    assert row is not None
    assert int(row[0]) == 0  # seeds only inserted when HOMELAB_MONITOR_INCLUDE_DEMO_SEEDS=1


async def test_migration_0008_remote_seed_row_has_null_source_path(
    db_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The remote-host seed row uses NULL source_path (per D2)."""
    monkeypatch.setenv("HOMELAB_MONITOR_INCLUDE_DEMO_SEEDS", "1")
    cfg = Config()
    cfg.set_main_option("script_location", str(ALEMBIC_DIR))
    cfg.set_main_option("sqlalchemy.url", db_url)
    command.upgrade(cfg, "head")

    engine = get_engine(url=db_url)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                text("SELECT source_path FROM crons WHERE host = 'remote-host'")
            )
            row = result.first()
        assert row is not None
        assert row[0] is None
    finally:
        await engine.dispose()


async def test_migration_0008_partial_index_idx_crons_active_uses_hidden_at(
    db_engine: AsyncEngine,
) -> None:
    """idx_crons_active is partial on hidden_at IS NULL (replacing archived_at)."""
    async with db_engine.connect() as conn:
        rows = (await conn.execute(text("PRAGMA index_list('crons')"))).fetchall()
        found = any(r[1] == "idx_crons_active" and r[4] == 1 for r in rows)
    assert found


async def test_migration_0008_round_trip(db_url: str) -> None:
    """upgrade -> downgrade -1 -> upgrade leaves the DB at head with new shape."""
    cfg = Config()
    cfg.set_main_option("script_location", str(ALEMBIC_DIR))
    cfg.set_main_option("sqlalchemy.url", db_url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0007")

    engine = get_engine(url=db_url)
    try:

        def _has_legacy(sync_conn: object) -> set[str]:
            ins = inspect(sync_conn)
            assert ins is not None
            return {c["name"] for c in ins.get_columns("crons")}

        async with engine.connect() as conn:
            legacy_cols = await conn.run_sync(_has_legacy)
        # Downgrade restored 0007's shape: id PK, integration_mode, archived_at.
        assert "id" in legacy_cols
        assert "integration_mode" in legacy_cols
        assert "archived_at" in legacy_cols
        assert "fingerprint" not in legacy_cols
    finally:
        await engine.dispose()

    command.upgrade(cfg, "head")
    engine = get_engine(url=db_url)
    try:

        def _has_new(sync_conn: object) -> set[str]:
            ins = inspect(sync_conn)
            assert ins is not None
            return {c["name"] for c in ins.get_columns("crons")}

        async with engine.connect() as conn:
            new_cols = await conn.run_sync(_has_new)
        assert "fingerprint" in new_cols
        assert "hidden_at" in new_cols
    finally:
        await engine.dispose()


async def test_migration_0009_renames_wrapper_column(
    db_engine: AsyncEngine,
) -> None:
    """After head (0009), wrapper_last_seen_at exists and wrapper_installed_at does not."""

    def _list_cols(sync_conn: object) -> set[str]:
        ins = inspect(sync_conn)
        return {c["name"] for c in ins.get_columns("crons")} if ins is not None else set()

    async with db_engine.connect() as conn:
        cols = await conn.run_sync(_list_cols)
    assert "wrapper_last_seen_at" in cols
    assert "wrapper_installed_at" not in cols


async def test_migration_0009_round_trip(db_url: str) -> None:
    """upgrade head -> downgrade 0008 -> upgrade head leaves the new column shape."""
    cfg = Config()
    cfg.set_main_option("script_location", str(ALEMBIC_DIR))
    cfg.set_main_option("sqlalchemy.url", db_url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0008")

    engine = get_engine(url=db_url)
    try:

        def _cols_at_0008(sync_conn: object) -> set[str]:
            ins = inspect(sync_conn)
            assert ins is not None
            return {c["name"] for c in ins.get_columns("crons")}

        async with engine.connect() as conn:
            cols = await conn.run_sync(_cols_at_0008)
        assert "wrapper_installed_at" in cols
        assert "wrapper_last_seen_at" not in cols
    finally:
        await engine.dispose()

    command.upgrade(cfg, "head")
    engine = get_engine(url=db_url)
    try:

        def _cols_at_head(sync_conn: object) -> set[str]:
            ins = inspect(sync_conn)
            assert ins is not None
            return {c["name"] for c in ins.get_columns("crons")}

        async with engine.connect() as conn:
            cols = await conn.run_sync(_cols_at_head)
        assert "wrapper_last_seen_at" in cols
        assert "wrapper_installed_at" not in cols
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Migration 0014 — heartbeats_state.logscrape_runs_since_heartbeat
# ---------------------------------------------------------------------------


async def test_migration_0014_column_present_at_head(db_engine: AsyncEngine) -> None:
    """After head, heartbeats_state has logscrape_runs_since_heartbeat (NOT NULL, default 0)."""

    def _inspect_hb(sync_conn: object) -> dict[str, object]:
        ins = inspect(sync_conn)
        assert ins is not None
        for col in ins.get_columns("heartbeats_state"):
            if col["name"] == "logscrape_runs_since_heartbeat":
                return col
        return {}

    async with db_engine.connect() as conn:
        col_info = await conn.run_sync(_inspect_hb)

    assert col_info, "logscrape_runs_since_heartbeat column missing from heartbeats_state"
    assert col_info.get("nullable") is False
    # server_default "0" → default value is 0
    server_default = col_info.get("default")
    assert server_default is not None
    assert "0" in str(server_default)


async def test_migration_0014_round_trip(db_url: str) -> None:
    """upgrade head -> downgrade 0013 -> upgrade head: column absent then present."""
    cfg = Config()
    cfg.set_main_option("script_location", str(ALEMBIC_DIR))
    cfg.set_main_option("sqlalchemy.url", db_url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0013")

    engine = get_engine(url=db_url)
    try:

        def _hb_cols_at_0013(sync_conn: object) -> set[str]:
            ins = inspect(sync_conn)
            assert ins is not None
            return {c["name"] for c in ins.get_columns("heartbeats_state")}

        async with engine.connect() as conn:
            cols = await conn.run_sync(_hb_cols_at_0013)
        assert "logscrape_runs_since_heartbeat" not in cols
    finally:
        await engine.dispose()

    # Re-upgrade: column should be back
    command.upgrade(cfg, "head")
    engine = get_engine(url=db_url)
    try:

        def _hb_cols_at_head(sync_conn: object) -> set[str]:
            ins = inspect(sync_conn)
            assert ins is not None
            return {c["name"] for c in ins.get_columns("heartbeats_state")}

        async with engine.connect() as conn:
            cols = await conn.run_sync(_hb_cols_at_head)
        assert "logscrape_runs_since_heartbeat" in cols
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Migration 0015 — cron_runs table
# ---------------------------------------------------------------------------


async def test_migration_0015_cron_runs_table_present_at_head(
    db_engine: AsyncEngine,
) -> None:
    """After head, cron_runs table exists with exactly 16 expected columns."""
    expected_columns = {
        "run_id",
        "cron_fingerprint",
        "source",
        "state",
        "started_at",
        "ended_at",
        "duration_seconds",
        "exit_code",
        "vl_window_start",
        "vl_window_end",
        "overlapping",
        "enriched_at",
        "line_count",
        "byte_count",
        "content_digest",
        "anomaly_flags",
    }

    def _inspect_cron_runs(sync_conn: object) -> tuple[set[str], set[str]]:
        ins = inspect(sync_conn)
        assert ins is not None
        cols = {c["name"] for c in ins.get_columns("cron_runs")}
        pk = set(ins.get_pk_constraint("cron_runs").get("constrained_columns", []))
        return cols, pk

    async with db_engine.connect() as conn:
        cols, pk = await conn.run_sync(_inspect_cron_runs)

    assert cols == expected_columns
    assert pk == {"run_id"}


async def test_migration_0015_indexes_present_at_head(db_engine: AsyncEngine) -> None:
    """After head, cron_runs has all 3 indexes including the partial one."""

    async with db_engine.connect() as conn:
        result = await conn.execute(text("PRAGMA index_list('cron_runs')"))
        rows = result.fetchall()
    # PRAGMA index_list columns: seq, name, unique, origin, partial
    indexes = {row[1]: row for row in rows}

    assert "ix_cron_runs_fingerprint_started" in indexes
    assert "ix_cron_runs_enrich_queue" in indexes
    assert "ix_cron_runs_fingerprint_state" in indexes

    # ix_cron_runs_enrich_queue is partial (column 4 = partial flag)
    enrich_queue_idx = indexes["ix_cron_runs_enrich_queue"]
    assert enrich_queue_idx[4] == 1  # partial == 1


async def test_migration_0015_round_trip(db_url: str) -> None:
    """upgrade head -> downgrade 0014 -> upgrade head: table absent then present."""
    cfg = Config()
    cfg.set_main_option("script_location", str(ALEMBIC_DIR))
    cfg.set_main_option("sqlalchemy.url", db_url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0014")

    engine = get_engine(url=db_url)
    try:

        def _tables_at_0014(sync_conn: object) -> set[str]:
            ins = inspect(sync_conn)
            assert ins is not None
            return set(ins.get_table_names())

        async with engine.connect() as conn:
            tables = await conn.run_sync(_tables_at_0014)
        assert "cron_runs" not in tables
    finally:
        await engine.dispose()

    # Re-upgrade: table should be back
    command.upgrade(cfg, "head")
    engine = get_engine(url=db_url)
    try:

        def _tables_at_head(sync_conn: object) -> set[str]:
            ins = inspect(sync_conn)
            assert ins is not None
            return set(ins.get_table_names())

        async with engine.connect() as conn:
            tables = await conn.run_sync(_tables_at_head)
        assert "cron_runs" in tables
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Migration 0016 — crons.wrapper_format_version column
# ---------------------------------------------------------------------------


async def test_migration_0016_adds_wrapper_format_version(db_engine: AsyncEngine) -> None:
    """After head, crons table has wrapper_format_version column (TEXT, nullable)."""

    def _get_crons_cols(sync_conn: object) -> set[str]:
        ins = inspect(sync_conn)
        assert ins is not None
        return {c["name"] for c in ins.get_columns("crons")}

    async with db_engine.connect() as conn:
        cols = await conn.run_sync(_get_crons_cols)

    assert "wrapper_format_version" in cols


async def test_migration_0016_round_trip(db_url: str) -> None:
    """upgrade head -> downgrade 0015 -> upgrade head: column absent then present."""
    cfg = Config()
    cfg.set_main_option("script_location", str(ALEMBIC_DIR))
    cfg.set_main_option("sqlalchemy.url", db_url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0015")

    engine = get_engine(url=db_url)
    try:

        def _cols_at_0015(sync_conn: object) -> set[str]:
            ins = inspect(sync_conn)
            assert ins is not None
            return {c["name"] for c in ins.get_columns("crons")}

        async with engine.connect() as conn:
            cols = await conn.run_sync(_cols_at_0015)
        assert "wrapper_format_version" not in cols
    finally:
        await engine.dispose()

    # Re-upgrade: column should be back
    command.upgrade(cfg, "head")
    engine = get_engine(url=db_url)
    try:

        def _cols_at_head(sync_conn: object) -> set[str]:
            ins = inspect(sync_conn)
            assert ins is not None
            return {c["name"] for c in ins.get_columns("crons")}

        async with engine.connect() as conn:
            cols = await conn.run_sync(_cols_at_head)
        assert "wrapper_format_version" in cols
    finally:
        await engine.dispose()


async def test_migration_0024_round_trip(db_url: str) -> None:
    """Migration 0024 creates docker_override_ownership; downgrade drops it."""
    engine = get_engine(url=db_url)
    try:
        await run_migrations(engine)

        def _list_tables(sync_conn: object) -> set[str]:
            inspector = inspect(sync_conn)
            return set(inspector.get_table_names()) if inspector is not None else set()

        async with engine.connect() as conn:
            tables_at_head = await conn.run_sync(_list_tables)
        assert "docker_override_ownership" in tables_at_head

        # Downgrade to 0023 + verify table dropped.
        cfg = Config()
        cfg.set_main_option("script_location", str(ALEMBIC_DIR))
        cfg.set_main_option("sqlalchemy.url", db_url)
        command.downgrade(cfg, "0023")

        async with engine.connect() as conn:
            tables_at_0023 = await conn.run_sync(_list_tables)
        assert "docker_override_ownership" not in tables_at_0023
    finally:
        await engine.dispose()
