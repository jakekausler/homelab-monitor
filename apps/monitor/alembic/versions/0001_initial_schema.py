"""initial schema (19 tables, 2 indexes)

Revision ID: 0001
Revises:
Create Date: 2026-05-05

Stage: STAGE-001-004 (per design decision: option (a), all tables up-front).
Behavioural columns added by later stages via additive migrations.

Tables fully defined per spec §6.1:
    users, sessions, audit_log, api_tokens

Tables created as minimal stubs (id, name/key, created_at):
    targets, collectors, crons, heartbeats_state, alerts, alert_outcomes,
    runbooks, runbook_runs, secrets, channels, routing_rules, digest_configs,
    maintenance_windows, suggestions, tool_scorecards

Indexes created per spec §6.1 (only those whose columns exist in this migration):
    idx_alerts_fingerprint, idx_targets_name
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ---------- fully-defined tables ----------
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("username", sa.Text(), nullable=False, unique=True),
        sa.Column("bcrypt_hash", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
    )

    op.create_table(
        "sessions",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.Text(), nullable=False),
        sa.Column("created_ip", sa.Text(), nullable=False),
        sa.Column("csrf_token", sa.Text(), nullable=False),
    )

    op.create_table(
        "audit_log",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("who", sa.Text(), nullable=False),
        sa.Column("what", sa.Text(), nullable=False),
        sa.Column("when", sa.Text(), nullable=False),  # quoted at SQL emit by Alembic
        sa.Column("before_json", sa.Text(), nullable=True),
        sa.Column("after_json", sa.Text(), nullable=True),
        sa.Column("ip", sa.Text(), nullable=True),
    )

    op.create_table(
        "api_tokens",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False, unique=True),
        sa.Column("hash", sa.Text(), nullable=False),
        sa.Column("scopes", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("last_used_at", sa.Text(), nullable=True),
        sa.Column("rotated_at", sa.Text(), nullable=True),
    )

    # ---------- minimal-schema stubs (id, name/key column, created_at) ----------
    op.create_table(
        "targets",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
    )

    op.create_table(
        "collectors",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("config", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=False),
    )

    op.create_table(
        "crons",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("command", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
    )

    op.create_table(
        "heartbeats_state",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
    )

    op.create_table(
        "alerts",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("fingerprint", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
    )

    op.create_table(
        "alert_outcomes",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "alert_id",
            sa.Text(),
            sa.ForeignKey("alerts.id"),
            nullable=False,
        ),
        sa.Column("created_at", sa.Text(), nullable=False),
    )

    op.create_table(
        "runbooks",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
    )

    op.create_table(
        "runbook_runs",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "runbook_id",
            sa.Text(),
            sa.ForeignKey("runbooks.id"),
            nullable=False,
        ),
        sa.Column("created_at", sa.Text(), nullable=False),
    )

    op.create_table(
        "secrets",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
    )

    op.create_table(
        "channels",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
    )

    op.create_table(
        "routing_rules",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("severity", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
    )

    op.create_table(
        "digest_configs",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("recipient", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
    )

    op.create_table(
        "maintenance_windows",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
    )

    op.create_table(
        "suggestions",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
    )

    op.create_table(
        "tool_scorecards",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("tool", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
    )

    # ---------- indexes whose target columns exist in this migration ----------
    op.create_index("idx_alerts_fingerprint", "alerts", ["fingerprint"])
    op.create_index("idx_targets_name", "targets", ["name"])


def downgrade() -> None:
    # Drop indexes first.
    op.drop_index("idx_targets_name", table_name="targets")
    op.drop_index("idx_alerts_fingerprint", table_name="alerts")

    # Drop FK-dependent tables before their parents.
    op.drop_table("tool_scorecards")
    op.drop_table("suggestions")
    op.drop_table("maintenance_windows")
    op.drop_table("digest_configs")
    op.drop_table("routing_rules")
    op.drop_table("channels")
    op.drop_table("secrets")
    op.drop_table("runbook_runs")
    op.drop_table("runbooks")
    op.drop_table("alert_outcomes")
    op.drop_table("alerts")
    op.drop_table("heartbeats_state")
    op.drop_table("crons")
    op.drop_table("collectors")
    op.drop_table("targets")
    op.drop_table("api_tokens")
    op.drop_table("audit_log")
    op.drop_table("sessions")
    op.drop_table("users")
