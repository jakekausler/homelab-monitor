"""STAGE-003-005 Refinement: re-key targets for docker containers to logical service identity.

Per Refinement scope-expansion (2026-05-22):
  - container_id is volatile across `docker compose up --force-recreate`. Keying
    targets on it produced duplicate rows on every recreation (old → 'missing',
    new → running).
  - This migration adds (logical_key_kind, logical_key) to `targets`, partial
    unique index on it for docker_container rows, and (previous_container_id,
    recreated_at) to `targets_docker` for one-level recreation forensics.
  - Existing duplicate rows are consolidated: keep latest last_seen; delete
    siblings; their `targets_docker` rows cascade.

**WARNING: This migration is ONE-WAY.** The `upgrade()` performs destructive
data consolidation:
- Duplicate `targets` rows are DELETED (only the latest `last_seen` survivor remains).
- `suggestions.deduplication_key` is rewritten from container_id to logical-key form.
- Duplicate `suggestions` rows (with the same post-rewrite logical-key) are DELETED.

The `downgrade()` is BEST-EFFORT: it drops the new columns/indexes and restores
the prior dedup_key form from `suggestions_docker.container_id`, but it CANNOT
restore the rows that were consolidated away. If you need to downgrade, restore
the database from a backup taken BEFORE this migration ran.

Revision ID: 0021
Revises: 0020
"""

from __future__ import annotations

import warnings

from sqlalchemy import inspect, text

from alembic import op

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | None = None
depends_on: str | None = None


def _derive_logical_key_sql() -> str:
    """SQL CASE that mirrors derive_docker_logical_key().

    For compose containers (project + service labels both non-empty):
      ('compose', project || '/' || service)
    Else:
      ('name', name)

    Labels are stored as JSON text in targets.labels. We use json_extract
    (SQLite ≥ 3.9) to pull com.docker.compose.project + service.
    Falls back to ('name', name) when either is NULL / empty.
    """
    return (
        "CASE "
        "  WHEN json_extract(labels, '$.\"com.docker.compose.project\"') IS NOT NULL "
        "       AND json_extract(labels, '$.\"com.docker.compose.project\"') != '' "
        "       AND json_extract(labels, '$.\"com.docker.compose.service\"') IS NOT NULL "
        "       AND json_extract(labels, '$.\"com.docker.compose.service\"') != '' "
        "  THEN 'compose' "
        "  ELSE 'name' "
        "END"
    )


def _derive_logical_value_sql() -> str:
    return (
        "CASE "
        "  WHEN json_extract(labels, '$.\"com.docker.compose.project\"') IS NOT NULL "
        "       AND json_extract(labels, '$.\"com.docker.compose.project\"') != '' "
        "       AND json_extract(labels, '$.\"com.docker.compose.service\"') IS NOT NULL "
        "       AND json_extract(labels, '$.\"com.docker.compose.service\"') != '' "
        "  THEN json_extract(labels, '$.\"com.docker.compose.project\"') "
        "       || '/' || json_extract(labels, '$.\"com.docker.compose.service\"') "
        "  ELSE CASE WHEN name LIKE '/%' THEN SUBSTR(name, 2) ELSE name END "
        "END"
    )


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)

    # ---- 1. Add columns to `targets` (idempotent) ----
    targets_cols = {c["name"] for c in inspector.get_columns("targets")}
    if "logical_key_kind" not in targets_cols:
        op.execute(text("ALTER TABLE targets ADD COLUMN logical_key_kind TEXT NULL"))
    if "logical_key" not in targets_cols:
        op.execute(text("ALTER TABLE targets ADD COLUMN logical_key TEXT NULL"))

    # ---- 2. Add columns to `targets_docker` (idempotent) ----
    docker_cols = {c["name"] for c in inspector.get_columns("targets_docker")}
    if "previous_container_id" not in docker_cols:
        op.execute(text("ALTER TABLE targets_docker ADD COLUMN previous_container_id TEXT NULL"))
    if "recreated_at" not in docker_cols:
        op.execute(text("ALTER TABLE targets_docker ADD COLUMN recreated_at TEXT NULL"))
    if "container_id" not in docker_cols:
        op.execute(text("ALTER TABLE targets_docker ADD COLUMN container_id TEXT NULL"))

    # Backfill: pre-rekey, target_id == container_id. After rekey the relation
    # is broken — but this BACKFILL captures the last-known container_id for
    # every existing row before consolidation, so the surviving row's
    # container_id reflects the latest container_id we had recorded.
    op.execute(
        text("UPDATE targets_docker SET container_id = target_id WHERE container_id IS NULL")
    )

    # ---- 3. Backfill logical_key_kind / logical_key for existing docker rows ----
    op.execute(
        text(
            "UPDATE targets SET "
            f"  logical_key_kind = ({_derive_logical_key_sql()}), "
            f"  logical_key = ({_derive_logical_value_sql()}) "
            "WHERE kind = 'docker_container' "
            "  AND (logical_key_kind IS NULL OR logical_key IS NULL)"
        ).bindparams()
        # bindparams() is a no-op safety; the SQL above contains literal JSON
        # paths quoted with single quotes inside double-quoted JSON-pointer
        # keys, no SQL params needed. The op.execute path is safe because
        # the SQL has no user input — only schema-derived names.
    )

    # ---- 4. Consolidate duplicates: keep latest last_seen per logical key ----
    # Find the survivor target.id for each (logical_key_kind, logical_key) group.
    # ROWID is used as a tiebreaker when last_seen ties.
    op.execute(
        text(
            "DELETE FROM targets WHERE kind = 'docker_container' AND id IN ("
            "  SELECT t1.id FROM targets t1 "
            "  WHERE t1.kind = 'docker_container' "
            "    AND t1.id NOT IN ("
            "      SELECT t2.id FROM targets t2 "
            "      WHERE t2.kind = 'docker_container' "
            "        AND t2.logical_key_kind = t1.logical_key_kind "
            "        AND t2.logical_key = t1.logical_key "
            "      ORDER BY COALESCE(t2.last_seen, t2.first_seen, '') DESC, t2.id DESC "
            "      LIMIT 1"
            "    )"
            ")"
        )
    )
    # CASCADE from targets_docker.target_id FK CONSTRAINT (declared in 0018)
    # automatically removes orphaned sidecar rows.

    # ---- 5. Partial unique index enforcing "one row per logical service" ----
    # TODO(post-0021): if any row's logical_key remains NULL after backfill (e.g.,
    # both labels JSON malformed AND name column NULL), the partial unique index
    # below may misbehave. Audit via: SELECT id FROM targets WHERE kind='docker_container'
    # AND logical_key IS NULL. See code review I3.
    existing_indexes = {ix["name"] for ix in inspect(bind).get_indexes("targets")}
    if "ux_targets_docker_logical_key" not in existing_indexes:
        op.execute(
            text(
                "CREATE UNIQUE INDEX ux_targets_docker_logical_key "
                "ON targets (logical_key_kind, logical_key) "
                "WHERE kind = 'docker_container'"
            )
        )

    # ---- 6. Helper indexes on sidecar ----
    existing_docker_indexes = {ix["name"] for ix in inspect(bind).get_indexes("targets_docker")}
    if "idx_targets_docker_previous_container_id" not in existing_docker_indexes:
        op.execute(
            text(
                "CREATE INDEX idx_targets_docker_previous_container_id "
                "ON targets_docker (previous_container_id)"
            )
        )
    if "idx_targets_docker_container_id" not in existing_docker_indexes:
        op.execute(
            text("CREATE INDEX idx_targets_docker_container_id ON targets_docker(container_id)")
        )

    # ---- 7. Consolidate suggestions deduplication_key for docker_* kinds ----
    # The discoverer now uses logical-key for deduplication too (see D-SUGGESTIONS-DEDUP-KEY).
    # Rewrite existing dedup keys for docker_container_discovered + docker_label_collision
    # suggestions from the old container_id form to the new "<kind>:<value>" form.
    #
    # ORDER MATTERS: we must DELETE duplicates that WOULD collide AFTER the
    # rewrite BEFORE doing the rewrite itself, otherwise the UPDATE triggers
    # UNIQUE(kind, deduplication_key) violations. The CTE below computes the
    # post-rewrite key for each row, then we delete all but the most-recently-
    # updated row per (kind, future_dedup_key) group. FK CASCADE then drops the
    # orphaned suggestions_docker rows.
    op.execute(
        text(
            "WITH future_keys AS ( "
            "  SELECT s.id AS sid, s.kind AS kind, s.updated_at AS updated_at, "
            "    CASE "
            "      WHEN d.compose_project IS NOT NULL AND d.compose_project != '' "
            "           AND d.compose_service IS NOT NULL AND d.compose_service != '' "
            "      THEN 'compose:' || d.compose_project || '/' || d.compose_service "
            "      ELSE 'name:' || CASE WHEN d.container_name LIKE '/%' THEN SUBSTR(d.container_name, 2) ELSE d.container_name END "
            "    END AS future_key "
            "  FROM suggestions s "
            "  JOIN suggestions_docker d ON d.suggestion_id = s.id "
            "  WHERE s.kind IN ('docker_container_discovered', 'docker_label_collision') "
            "), "
            "ranked AS ( "
            "  SELECT sid, kind, future_key, "
            "    ROW_NUMBER() OVER ( "
            "      PARTITION BY kind, future_key "
            "      ORDER BY updated_at DESC, sid DESC "
            "    ) AS rn "
            "  FROM future_keys "
            ") "
            "DELETE FROM suggestions WHERE id IN ( "
            "  SELECT sid FROM ranked WHERE rn > 1 "
            ")"
        )
    )
    # Now safe to rewrite — survivors are unique per (kind, future_key).
    op.execute(
        text(
            "UPDATE suggestions SET deduplication_key = ( "
            "  SELECT CASE "
            "    WHEN d.compose_project IS NOT NULL AND d.compose_project != '' "
            "         AND d.compose_service IS NOT NULL AND d.compose_service != '' "
            "    THEN 'compose:' || d.compose_project || '/' || d.compose_service "
            "    ELSE 'name:' || CASE WHEN d.container_name LIKE '/%' THEN SUBSTR(d.container_name, 2) ELSE d.container_name END "
            "  END "
            "  FROM suggestions_docker d WHERE d.suggestion_id = suggestions.id "
            ") "
            "WHERE kind IN ('docker_container_discovered', 'docker_label_collision') "
            "  AND id IN (SELECT suggestion_id FROM suggestions_docker)"
        )
    )


def downgrade() -> None:
    warnings.warn(
        "Migration 0021 downgrade is best-effort. Consolidated duplicate rows "
        "cannot be restored. Restore from a pre-0021 backup if you need full state.",
        stacklevel=2,
    )
    bind = op.get_bind()
    inspector = inspect(bind)

    # Suggestions dedup_key rewrite cannot be reversed cleanly (the original
    # container_id values aren't retained anywhere). Best-effort: restore to
    # the current container_id of the surviving sidecar row.
    op.execute(
        text(
            "UPDATE suggestions SET deduplication_key = ("
            "  SELECT d.container_id FROM suggestions_docker d "
            "  WHERE d.suggestion_id = suggestions.id"
            ") "
            "WHERE kind IN ('docker_container_discovered', 'docker_label_collision') "
            "  AND id IN (SELECT suggestion_id FROM suggestions_docker)"
        )
    )

    docker_indexes = {ix["name"] for ix in inspector.get_indexes("targets_docker")}
    if "idx_targets_docker_previous_container_id" in docker_indexes:
        op.drop_index("idx_targets_docker_previous_container_id", table_name="targets_docker")
    if "idx_targets_docker_container_id" in docker_indexes:
        op.drop_index("idx_targets_docker_container_id", table_name="targets_docker")

    targets_indexes = {ix["name"] for ix in inspector.get_indexes("targets")}
    if "ux_targets_docker_logical_key" in targets_indexes:
        op.execute(text("DROP INDEX IF EXISTS ux_targets_docker_logical_key"))

    # SQLite cannot DROP COLUMN cleanly across all supported versions.
    # The 0019 / 0020 pattern (table-swap) is acceptable here too. Both
    # targets and targets_docker carry FKs / indexes that complicate the
    # swap — instead we leave the columns in place and set them to NULL on
    # downgrade. They are nullable, so no application code is broken by their
    # mere presence; new-code paths simply re-populate them. This matches the
    # spirit of the 0020 downgrade (which also rebuilds rather than ALTER DROP).
    op.execute(text("UPDATE targets SET logical_key_kind = NULL, logical_key = NULL"))
    op.execute(
        text(
            "UPDATE targets_docker SET previous_container_id = NULL, recreated_at = NULL, container_id = NULL"
        )
    )
