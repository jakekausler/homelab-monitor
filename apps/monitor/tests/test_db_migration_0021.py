"""Tests for alembic migration 0021: targets logical-key re-key + suggestions dedup rewrite.

Round-trip, logical_key backfill (compose vs name), C2 leading-slash normalization,
duplicate consolidation, suggestions dedup_key rewrite, partial unique index,
downgrade warning + best-effort reversal.
"""

from __future__ import annotations

import json
import warnings

import pytest
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncEngine

from alembic import command
from homelab_monitor.kernel.db.engine import get_engine
from homelab_monitor.kernel.db.migrations import ALEMBIC_DIR


def _make_cfg(db_url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(ALEMBIC_DIR))
    cfg.set_main_option("sqlalchemy.url", db_url)
    return cfg


# ---------------------------------------------------------------------------
# Helpers to seed a minimal targets row (requires schema at 0020 / head-1).
# targets table after 0018: id, kind, name, labels, status, first_seen, last_seen
# ---------------------------------------------------------------------------


async def _seed_target(  # noqa: PLR0913
    engine: AsyncEngine,
    *,
    target_id: str,
    name: str,
    labels: dict[str, str],
    last_seen: str = "2026-01-01T00:00:00Z",
    first_seen: str = "2026-01-01T00:00:00Z",
) -> None:
    async with engine.connect() as conn:
        await conn.execute(
            text(
                "INSERT INTO targets "
                "(id, kind, name, labels, status, first_seen, last_seen, created_at) "
                "VALUES (:id, 'docker_container', :name, :labels, 'running', :fs, :ls, :ca)"
            ),
            {
                "id": target_id,
                "name": name,
                "labels": json.dumps(labels),
                "fs": first_seen,
                "ls": last_seen,
                "ca": first_seen,
            },
        )
        await conn.commit()


async def _seed_suggestion(  # noqa: PLR0913
    engine: AsyncEngine,
    *,
    sugg_id: str,
    dedup_key: str,
    container_id: str,
    container_name: str,
    compose_project: str = "",
    compose_service: str = "",
) -> None:
    """Seed a docker_container_discovered suggestion + sidecar row."""
    async with engine.connect() as conn:
        await conn.execute(
            text(
                "INSERT INTO suggestions "
                "(id, kind, deduplication_key, state, created_at, updated_at) "
                "VALUES (:id, 'docker_container_discovered', :dedup, 'pending', "
                "        '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"
            ),
            {"id": sugg_id, "dedup": dedup_key},
        )
        await conn.execute(
            text(
                "INSERT INTO suggestions_docker "
                "(suggestion_id, container_id, container_name, image_ref, "
                " labels_json, compose_project, compose_service, detection_reason) "
                "VALUES (:sid, :cid, :cn, 'nginx:latest', '{}', :cp, :cs, "
                "        'no_homelab_monitor_label')"
            ),
            {
                "sid": sugg_id,
                "cid": container_id,
                "cn": container_name,
                "cp": compose_project,
                "cs": compose_service,
            },
        )
        await conn.commit()


# ---------------------------------------------------------------------------
# Schema presence tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_migration_0021_adds_columns_to_targets(db_url: str) -> None:
    """After upgrade, targets has logical_key_kind and logical_key columns."""
    cfg = _make_cfg(db_url)
    command.upgrade(cfg, "head")

    engine: AsyncEngine = get_engine(url=db_url)
    try:
        async with engine.connect() as conn:

            def _get_col_names(sync_conn: object) -> set[str]:
                inspector = inspect(sync_conn)
                if inspector is None:
                    return set()
                return {col["name"] for col in inspector.get_columns("targets")}

            cols = await conn.run_sync(_get_col_names)

        assert "logical_key_kind" in cols
        assert "logical_key" in cols
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_migration_0021_adds_columns_to_targets_docker(db_url: str) -> None:
    """After upgrade, targets_docker has previous_container_id, recreated_at, container_id."""
    cfg = _make_cfg(db_url)
    command.upgrade(cfg, "head")

    engine: AsyncEngine = get_engine(url=db_url)
    try:
        async with engine.connect() as conn:

            def _get_col_names(sync_conn: object) -> set[str]:
                inspector = inspect(sync_conn)
                if inspector is None:
                    return set()
                return {col["name"] for col in inspector.get_columns("targets_docker")}

            cols = await conn.run_sync(_get_col_names)

        assert "previous_container_id" in cols
        assert "recreated_at" in cols
        assert "container_id" in cols
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_migration_0021_partial_unique_index_exists(db_url: str) -> None:
    """After upgrade, ux_targets_docker_logical_key index exists on targets."""
    cfg = _make_cfg(db_url)
    command.upgrade(cfg, "head")

    engine: AsyncEngine = get_engine(url=db_url)
    try:
        async with engine.connect() as conn:

            def _get_index_names(sync_conn: object) -> set[str]:
                inspector = inspect(sync_conn)
                if inspector is None:
                    return set()
                return {
                    idx["name"]
                    for idx in inspector.get_indexes("targets") or []  # pyright: ignore[reportUnknownVariableType,reportUnknownMemberType]
                }

            indexes = await conn.run_sync(_get_index_names)

        assert "ux_targets_docker_logical_key" in indexes
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Backfill tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_migration_0021_backfill_compose_labels(db_url: str) -> None:
    """targets row with compose project+service labels gets logical_key_kind='compose'."""
    cfg = _make_cfg(db_url)
    command.upgrade(cfg, "0020")

    engine: AsyncEngine = get_engine(url=db_url)
    try:
        await _seed_target(
            engine,
            target_id="tgt-compose-1",
            name="myproject_myservice_1",
            labels={
                "com.docker.compose.project": "myproject",
                "com.docker.compose.service": "myservice",
            },
        )
    finally:
        await engine.dispose()

    command.upgrade(cfg, "head")

    engine = get_engine(url=db_url)
    try:
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text("SELECT logical_key_kind, logical_key FROM targets WHERE id = :id"),
                    {"id": "tgt-compose-1"},
                )
            ).first()

        assert row is not None
        assert row[0] == "compose", f"expected 'compose', got {row[0]!r}"
        assert row[1] == "myproject/myservice", f"expected 'myproject/myservice', got {row[1]!r}"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_migration_0021_backfill_name_fallback(db_url: str) -> None:
    """targets row without compose labels gets logical_key_kind='name' and logical_key=name."""
    cfg = _make_cfg(db_url)
    command.upgrade(cfg, "0020")

    engine: AsyncEngine = get_engine(url=db_url)
    try:
        await _seed_target(
            engine,
            target_id="tgt-name-1",
            name="standalone-container",
            labels={},
        )
    finally:
        await engine.dispose()

    command.upgrade(cfg, "head")

    engine = get_engine(url=db_url)
    try:
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text("SELECT logical_key_kind, logical_key FROM targets WHERE id = :id"),
                    {"id": "tgt-name-1"},
                )
            ).first()

        assert row is not None
        assert row[0] == "name", f"expected 'name', got {row[0]!r}"
        assert row[1] == "standalone-container"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_migration_0021_backfill_strips_leading_slash_from_name(db_url: str) -> None:
    """C2 normalization: target name with leading '/' yields logical_key without slash."""
    cfg = _make_cfg(db_url)
    command.upgrade(cfg, "0020")

    engine: AsyncEngine = get_engine(url=db_url)
    try:
        await _seed_target(
            engine,
            target_id="tgt-slash-1",
            name="/slashed-name",
            labels={},
        )
    finally:
        await engine.dispose()

    command.upgrade(cfg, "head")

    engine = get_engine(url=db_url)
    try:
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text("SELECT logical_key FROM targets WHERE id = :id"),
                    {"id": "tgt-slash-1"},
                )
            ).first()

        assert row is not None
        assert not row[0].startswith("/"), f"logical_key should not start with '/': {row[0]!r}"
        assert row[0] == "slashed-name"
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Duplicate consolidation tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_migration_0021_consolidates_duplicate_targets(db_url: str) -> None:
    """Three duplicate targets for same logical key collapses to one (latest last_seen)."""
    cfg = _make_cfg(db_url)
    command.upgrade(cfg, "0020")

    engine: AsyncEngine = get_engine(url=db_url)
    try:
        # Three rows for same compose project/service, different container_ids and last_seen.
        for i, ts in enumerate(
            ["2026-01-01T00:00:00Z", "2026-01-03T00:00:00Z", "2026-01-02T00:00:00Z"]
        ):
            await _seed_target(
                engine,
                target_id=f"tgt-dup-{i}",
                name="dupproject_dupservice_1",
                labels={
                    "com.docker.compose.project": "dupproject",
                    "com.docker.compose.service": "dupservice",
                },
                last_seen=ts,
                first_seen="2026-01-01T00:00:00Z",
            )
    finally:
        await engine.dispose()

    command.upgrade(cfg, "head")

    engine = get_engine(url=db_url)
    try:
        async with engine.connect() as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT id, logical_key, last_seen FROM targets "
                        "WHERE logical_key = 'dupproject/dupservice'"
                    )
                )
            ).fetchall()

        assert len(rows) == 1, f"expected 1 survivor, got {len(rows)}: {rows}"
        # tgt-dup-1 has the latest last_seen (2026-01-03).
        assert rows[0][0] == "tgt-dup-1", f"wrong survivor: {rows[0][0]!r}"
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Suggestions dedup_key rewrite tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_migration_0021_rewrites_suggestion_dedup_key_compose(db_url: str) -> None:
    """suggestion with compose project+service gets dedup_key rewritten to 'compose:p/s'."""
    cfg = _make_cfg(db_url)
    command.upgrade(cfg, "0020")

    engine: AsyncEngine = get_engine(url=db_url)
    try:
        await _seed_suggestion(
            engine,
            sugg_id="sugg-compose-1",
            dedup_key="container_id_abc123",
            container_id="container-abc123",
            container_name="webproj_web_1",
            compose_project="webproj",
            compose_service="web",
        )
    finally:
        await engine.dispose()

    command.upgrade(cfg, "head")

    engine = get_engine(url=db_url)
    try:
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text("SELECT deduplication_key FROM suggestions WHERE id = :id"),
                    {"id": "sugg-compose-1"},
                )
            ).first()

        assert row is not None
        assert row[0] == "compose:webproj/web", f"unexpected dedup_key: {row[0]!r}"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_migration_0021_rewrites_suggestion_dedup_key_name(db_url: str) -> None:
    """suggestion without compose labels gets dedup_key rewritten to 'name:container_name'."""
    cfg = _make_cfg(db_url)
    command.upgrade(cfg, "0020")

    engine: AsyncEngine = get_engine(url=db_url)
    try:
        await _seed_suggestion(
            engine,
            sugg_id="sugg-name-1",
            dedup_key="container_id_xyz456",
            container_id="container-xyz456",
            container_name="standalone",
        )
    finally:
        await engine.dispose()

    command.upgrade(cfg, "head")

    engine = get_engine(url=db_url)
    try:
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text("SELECT deduplication_key FROM suggestions WHERE id = :id"),
                    {"id": "sugg-name-1"},
                )
            ).first()

        assert row is not None
        assert row[0] == "name:standalone", f"unexpected dedup_key: {row[0]!r}"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_migration_0021_suggestion_name_strips_leading_slash(db_url: str) -> None:
    """C2 normalization: container_name with leading '/' → dedup_key without slash."""
    cfg = _make_cfg(db_url)
    command.upgrade(cfg, "0020")

    engine: AsyncEngine = get_engine(url=db_url)
    try:
        await _seed_suggestion(
            engine,
            sugg_id="sugg-slash-1",
            dedup_key="container_id_slashed",
            container_id="container-slashed",
            container_name="/slashed-container",
        )
    finally:
        await engine.dispose()

    command.upgrade(cfg, "head")

    engine = get_engine(url=db_url)
    try:
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text("SELECT deduplication_key FROM suggestions WHERE id = :id"),
                    {"id": "sugg-slash-1"},
                )
            ).first()

        assert row is not None
        assert not row[0].startswith("name:/"), (
            f"dedup_key should not contain leading slash: {row[0]!r}"
        )
        assert row[0] == "name:slashed-container"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_migration_0021_consolidates_duplicate_suggestions(db_url: str) -> None:
    """Two suggestions mapping to same logical dedup_key collapses to one (latest updated_at)."""
    cfg = _make_cfg(db_url)
    command.upgrade(cfg, "0020")

    engine: AsyncEngine = get_engine(url=db_url)
    try:
        # Two suggestions that will both map to compose:projA/svcA after rewrite.
        for i, updated in enumerate(["2026-01-01T00:00:00Z", "2026-01-03T00:00:00Z"]):
            async with engine.connect() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO suggestions "
                        "(id, kind, deduplication_key, state, created_at, updated_at) "
                        "VALUES (:id, 'docker_container_discovered', :dedup, 'pending', "
                        "        '2026-01-01T00:00:00Z', :updated)"
                    ),
                    {"id": f"sugg-dupA-{i}", "dedup": f"container_id_dup{i}", "updated": updated},
                )
                await conn.execute(
                    text(
                        "INSERT INTO suggestions_docker "
                        "(suggestion_id, container_id, container_name, image_ref, "
                        " labels_json, compose_project, compose_service, detection_reason) "
                        "VALUES (:sid, :cid, 'projA_svcA_1', 'nginx:latest', '{}', "
                        "        'projA', 'svcA', 'no_homelab_monitor_label')"
                    ),
                    {"sid": f"sugg-dupA-{i}", "cid": f"container-dup{i}"},
                )
                await conn.commit()
    finally:
        await engine.dispose()

    command.upgrade(cfg, "head")

    engine = get_engine(url=db_url)
    try:
        async with engine.connect() as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT id, deduplication_key FROM suggestions "
                        "WHERE deduplication_key = 'compose:projA/svcA'"
                    )
                )
            ).fetchall()

        assert len(rows) == 1, f"expected 1 survivor, got {len(rows)}: {rows}"
        # sugg-dupA-1 has the later updated_at.
        assert rows[0][0] == "sugg-dupA-1", f"wrong survivor: {rows[0][0]!r}"
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Downgrade tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_migration_0021_downgrade_emits_warning(db_url: str) -> None:
    """downgrade to 0020 emits a UserWarning about best-effort reversal."""
    cfg = _make_cfg(db_url)
    command.upgrade(cfg, "head")

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        command.downgrade(cfg, "0020")

    warning_messages = [str(w.message) for w in caught if issubclass(w.category, UserWarning)]
    assert any("best-effort" in msg.lower() or "0021" in msg for msg in warning_messages), (
        f"Expected downgrade warning not found in: {warning_messages}"
    )


@pytest.mark.asyncio
async def test_migration_0021_downgrade_drops_unique_index(db_url: str) -> None:
    """After downgrade to 0020, ux_targets_docker_logical_key index is gone."""
    cfg = _make_cfg(db_url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0020")

    engine: AsyncEngine = get_engine(url=db_url)
    try:
        async with engine.connect() as conn:

            def _get_index_names(sync_conn: object) -> set[str]:
                inspector = inspect(sync_conn)
                if inspector is None:
                    return set()
                return {
                    idx["name"]
                    for idx in inspector.get_indexes("targets") or []  # pyright: ignore[reportUnknownVariableType,reportUnknownMemberType]
                }

            indexes = await conn.run_sync(_get_index_names)

        assert "ux_targets_docker_logical_key" not in indexes
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_migration_0021_downgrade_restores_container_id_dedup_key(db_url: str) -> None:
    """After downgrade, suggestions.deduplication_key is restored to container_id form."""
    cfg = _make_cfg(db_url)
    command.upgrade(cfg, "0020")

    engine: AsyncEngine = get_engine(url=db_url)
    try:
        await _seed_suggestion(
            engine,
            sugg_id="sugg-roundtrip-1",
            dedup_key="container_id_orig",
            container_id="container-orig",
            container_name="myapp",
        )
    finally:
        await engine.dispose()

    command.upgrade(cfg, "head")

    with warnings.catch_warnings(record=True):
        warnings.simplefilter("always")
        command.downgrade(cfg, "0020")

    engine = get_engine(url=db_url)
    try:
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text("SELECT deduplication_key FROM suggestions WHERE id = :id"),
                    {"id": "sugg-roundtrip-1"},
                )
            ).first()

        assert row is not None
        # Best-effort restore: should be the container_id from suggestions_docker.
        assert row[0] == "container-orig", f"unexpected dedup_key after downgrade: {row[0]!r}"
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_migration_0021_round_trip(db_url: str) -> None:
    """upgrade → downgrade → upgrade leaves schema in expected post-0021 state."""
    cfg = _make_cfg(db_url)
    command.upgrade(cfg, "head")

    with warnings.catch_warnings(record=True):
        warnings.simplefilter("always")
        command.downgrade(cfg, "0020")

    command.upgrade(cfg, "head")

    engine: AsyncEngine = get_engine(url=db_url)
    try:
        async with engine.connect() as conn:

            def _inspect_schema(sync_conn: object) -> tuple[set[str], set[str], set[str]]:
                inspector = inspect(sync_conn)
                if inspector is None:
                    return set(), set(), set()
                target_cols: set[str] = {col["name"] for col in inspector.get_columns("targets")}
                docker_cols: set[str] = {
                    col["name"] for col in inspector.get_columns("targets_docker")
                }
                target_indexes: set[str] = {
                    idx["name"]
                    for idx in inspector.get_indexes("targets") or []  # pyright: ignore[reportUnknownVariableType,reportUnknownMemberType]
                }
                return target_cols, docker_cols, target_indexes

            t_cols, d_cols, t_indexes = await conn.run_sync(_inspect_schema)

        assert "logical_key_kind" in t_cols
        assert "logical_key" in t_cols
        assert "previous_container_id" in d_cols
        assert "recreated_at" in d_cols
        assert "container_id" in d_cols
        assert "ux_targets_docker_logical_key" in t_indexes
    finally:
        await engine.dispose()
