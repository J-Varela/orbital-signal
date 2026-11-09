"""Retain collection attempts without rewriting existing checkpoints.

Revision ID: d84fc8a61b35
Revises: c961fd3e5a22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d84fc8a61b35"
down_revision: str | Sequence[str] | None = "c961fd3e5a22"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add audit rows and preserve only the known last legacy attempt snapshot."""

    attempts = op.create_table(
        "collection_attempts",
        sa.Column("id", sa.String(96), nullable=False),
        sa.Column("window_id", sa.String(64), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("pages_fetched", sa.Integer(), server_default="0", nullable=False),
        sa.Column("fetched_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("relevant_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("stored_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("duplicate_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("has_more", sa.Boolean(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("origin", sa.String(32), server_default="recorded", nullable=False),
        sa.Column("counts_complete", sa.Boolean(), server_default="true", nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'failed', 'incomplete', 'split')",
            name=op.f("ck_collection_attempts_status_valid"),
        ),
        sa.CheckConstraint(
            "attempt_number >= 1 AND pages_fetched >= 0 AND fetched_count >= 0 "
            "AND relevant_count >= 0 AND stored_count >= 0 AND duplicate_count >= 0",
            name=op.f("ck_collection_attempts_counts_nonnegative"),
        ),
        sa.CheckConstraint(
            "origin IN ('recorded', 'legacy_snapshot')",
            name=op.f("ck_collection_attempts_origin_valid"),
        ),
        sa.CheckConstraint(
            "completed_at IS NULL OR started_at IS NULL OR completed_at >= started_at",
            name=op.f("ck_collection_attempts_timestamps_ordered"),
        ),
        sa.CheckConstraint(
            "status != 'running' OR completed_at IS NULL",
            name=op.f("ck_collection_attempts_running_unfinished"),
        ),
        sa.ForeignKeyConstraint(
            ["window_id"],
            ["collection_windows.id"],
            ondelete="CASCADE",
            name=op.f("fk_collection_attempts_window_id_collection_windows"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_collection_attempts")),
        sa.UniqueConstraint(
            "window_id", "attempt_number", name=op.f("uq_collection_attempts_window_id")
        ),
    )
    windows = sa.table(
        "collection_windows",
        sa.column("id", sa.String(64)),
        sa.column("attempts", sa.Integer()),
        sa.column("status", sa.String(32)),
        sa.column("pages_fetched", sa.Integer()),
        sa.column("fetched_count", sa.Integer()),
        sa.column("relevant_count", sa.Integer()),
        sa.column("stored_count", sa.Integer()),
        sa.column("duplicate_count", sa.Integer()),
        sa.column("has_more", sa.Boolean()),
        sa.column("error_message", sa.Text()),
        sa.column("started_at", sa.DateTime(timezone=True)),
        sa.column("completed_at", sa.DateTime(timezone=True)),
    )
    completed_at = sa.case(
        (windows.c.status == "running", sa.null()),
        (windows.c.completed_at < windows.c.started_at, windows.c.started_at),
        else_=windows.c.completed_at,
    )
    op.execute(
        attempts.insert().from_select(
            [
                "id",
                "window_id",
                "attempt_number",
                "status",
                "pages_fetched",
                "fetched_count",
                "relevant_count",
                "stored_count",
                "duplicate_count",
                "has_more",
                "error_message",
                "started_at",
                "completed_at",
                "origin",
                "counts_complete",
            ],
            sa.select(
                windows.c.id + sa.literal(":") + sa.cast(windows.c.attempts, sa.String()),
                windows.c.id,
                windows.c.attempts,
                windows.c.status,
                windows.c.pages_fetched,
                windows.c.fetched_count,
                windows.c.relevant_count,
                windows.c.stored_count,
                windows.c.duplicate_count,
                windows.c.has_more,
                windows.c.error_message,
                windows.c.started_at,
                completed_at,
                sa.literal("legacy_snapshot"),
                sa.false(),
            ).where(windows.c.attempts >= 1),
        )
    )


def downgrade() -> None:
    """Remove the audit table while retaining checkpoints and collected evidence."""

    op.drop_table("collection_attempts")
