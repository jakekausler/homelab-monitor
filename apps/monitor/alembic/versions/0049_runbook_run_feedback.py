"""STAGE-009-009: runbook_run_feedback table for Claude→user improvement channel.

The fixer emits ``<pid>.feedback.json`` sentinel files in the transcript dir
during a run. The orchestrator scans for new sentinels after ``_exec_claude``
returns, parses them, and persists one row per feedback item. A malformed
sentinel yields a single synthetic ``parse_error`` row plus an
``autofix.feedback_parse_error`` audit event.

Schema mirrors the ``runbook_run_approvals`` convention: TEXT PK (UUIDv7), TEXT
FK to ``runbook_runs.id`` with NO ondelete cascade, JSON-as-TEXT for the
structured hint, ISO-8601 UTC ``created_at``. One index on ``runbook_run_id``
so per-run feedback listing is O(log n).

Revision ID: 0049
Revises: 0048
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0049"
down_revision: str | None = "0048"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "runbook_run_feedback",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("runbook_run_id", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("suggestion_text", sa.Text(), nullable=False),
        sa.Column("structured_hint", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["runbook_run_id"],
            ["runbook_runs.id"],
            name="fk_runbook_run_feedback_runbook_run_id",
        ),
    )
    op.create_index(
        "ix_runbook_run_feedback_runbook_run_id",
        "runbook_run_feedback",
        ["runbook_run_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_runbook_run_feedback_runbook_run_id",
        table_name="runbook_run_feedback",
    )
    op.drop_table("runbook_run_feedback")
