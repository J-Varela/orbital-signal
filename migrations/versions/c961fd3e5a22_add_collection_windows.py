"""Add resumable monthly collection checkpoints.

Revision ID: c961fd3e5a22
Revises: a48c1f2d9630
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c961fd3e5a22"
down_revision: str | Sequence[str] | None = "a48c1f2d9630"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add checkpoint storage without changing existing evidence tables."""

    op.create_table(
        "collection_windows",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("agency", sa.String(500), nullable=False),
        sa.Column("month_start", sa.Date(), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(32), server_default="pending", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("pages_fetched", sa.Integer(), server_default="0", nullable=False),
        sa.Column("fetched_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("relevant_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("stored_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("duplicate_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("has_more", sa.Boolean(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "end_date >= start_date", name=op.f("ck_collection_windows_dates_ordered")
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'failed', 'incomplete', 'split')",
            name=op.f("ck_collection_windows_status_valid"),
        ),
        sa.CheckConstraint(
            "attempts >= 0 AND pages_fetched >= 0 AND fetched_count >= 0 "
            "AND relevant_count >= 0 AND stored_count >= 0 AND duplicate_count >= 0",
            name=op.f("ck_collection_windows_counts_nonnegative"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_collection_windows")),
        sa.UniqueConstraint(
            "source", "agency", "start_date", "end_date", name=op.f("uq_collection_windows_source")
        ),
    )
    op.create_index("ix_collection_windows_month_start", "collection_windows", ["month_start"])


def downgrade() -> None:
    """Remove checkpoint storage, leaving award and signal records in place."""

    op.drop_index("ix_collection_windows_month_start", table_name="collection_windows")
    op.drop_table("collection_windows")
