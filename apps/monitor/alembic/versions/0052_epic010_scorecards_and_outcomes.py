"""EPIC-010 scorecards and outcomes.

Revision ID: 0052
Revises: 0051
Create Date: 2026-07-06T00:00:00

Replaces the STAGE-001 scaffolding stub of ``tool_scorecards`` (id, tool,
created_at) with the full 5-dimension x 3-window scorecard shape, and adds
four new EPIC-010 tables: alert_overlap_groups, shadow_rule_results,
recommendations, rule_annotations.

"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0052"
down_revision: str | None = "0051"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # 1. Drop the STAGE-001 scaffolding stub.
    op.drop_table("tool_scorecards")

    # 2. Recreate tool_scorecards with the full 5-dim x 3-window shape.
    op.create_table(
        "tool_scorecards",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("dimension", sa.Text(), nullable=False),
        sa.Column("dimension_value", sa.Text(), nullable=False),
        sa.Column("window", sa.Text(), nullable=False),
        sa.Column("alerts_emitted", sa.Integer(), nullable=False),
        sa.Column("action_rate", sa.Float(), nullable=False),
        sa.Column("dedup_overlap", sa.Float(), nullable=False),
        sa.Column("unique_share", sa.Float(), nullable=False),
        sa.Column("computed_at", sa.Text(), nullable=False),
    )
    op.create_index(
        "ix_tool_scorecards_dim_value_window",
        "tool_scorecards",
        ["dimension", "dimension_value", "window"],
        unique=True,
    )

    # 3. alert_overlap_groups
    op.create_table(
        "alert_overlap_groups",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("fingerprint", sa.Text(), nullable=False),
        sa.Column("group_key", sa.Text(), nullable=False),
        sa.Column("alert_ids", sa.Text(), nullable=False),
        sa.Column("group_started_at", sa.Text(), nullable=False),
        sa.Column("computed_at", sa.Text(), nullable=False),
    )
    op.create_index(
        "ix_alert_overlap_groups_fingerprint_started",
        "alert_overlap_groups",
        ["fingerprint", "group_started_at"],
    )

    # 4. shadow_rule_results
    op.create_table(
        "shadow_rule_results",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("pair_name", sa.Text(), nullable=False),
        sa.Column("window", sa.Text(), nullable=False),
        sa.Column("rule_a_hits", sa.Integer(), nullable=False),
        sa.Column("rule_b_hits", sa.Integer(), nullable=False),
        sa.Column("both_hits", sa.Integer(), nullable=False),
        sa.Column("either_hits", sa.Integer(), nullable=False),
        sa.Column("disagreement_count", sa.Integer(), nullable=False),
        sa.Column("computed_at", sa.Text(), nullable=False),
    )
    op.create_index(
        "ix_shadow_rule_results_pair_window",
        "shadow_rule_results",
        ["pair_name", "window"],
        unique=True,
    )

    # 5. recommendations
    op.create_table(
        "recommendations",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("dimension", sa.Text(), nullable=False),
        sa.Column("dimension_value", sa.Text(), nullable=False),
        sa.Column("rule_name", sa.Text(), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("severity", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("decided_at", sa.Text(), nullable=True),
        sa.Column("decided_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("applied_action_id", sa.Text(), nullable=True),
    )
    op.create_index(
        "ix_recommendations_status_created",
        "recommendations",
        ["status", "created_at"],
    )

    # 6. rule_annotations
    op.create_table(
        "rule_annotations",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("annotation_kind", sa.Text(), nullable=False),
        sa.Column("target_source_tool", sa.Text(), nullable=True),
        sa.Column("target_alertgroup", sa.Text(), nullable=True),
        sa.Column("target_alertname", sa.Text(), nullable=True),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.CheckConstraint(
            "target_source_tool IS NOT NULL OR target_alertgroup IS NOT NULL "
            "OR target_alertname IS NOT NULL",
            name="ck_rule_annotations_target_nonnull",
        ),
    )


def downgrade() -> None:
    # Reverse order of upgrade().
    op.drop_table("rule_annotations")

    op.drop_index("ix_recommendations_status_created", table_name="recommendations")
    op.drop_table("recommendations")

    op.drop_index("ix_shadow_rule_results_pair_window", table_name="shadow_rule_results")
    op.drop_table("shadow_rule_results")

    op.drop_index("ix_alert_overlap_groups_fingerprint_started", table_name="alert_overlap_groups")
    op.drop_table("alert_overlap_groups")

    op.drop_index("ix_tool_scorecards_dim_value_window", table_name="tool_scorecards")
    op.drop_table("tool_scorecards")

    # Restore the STAGE-001 scaffolding stub shape.
    op.create_table(
        "tool_scorecards",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("tool", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
    )
