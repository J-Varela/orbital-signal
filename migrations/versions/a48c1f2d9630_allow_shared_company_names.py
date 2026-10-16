"""Allow distinct recipients to share a company name.

Revision ID: a48c1f2d9630
Revises: 6f520f8eed69
"""

from collections.abc import Sequence

from alembic import op

revision: str = "a48c1f2d9630"
down_revision: str | Sequence[str] | None = "6f520f8eed69"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Use UEI as the unique identity while retaining name lookup support."""

    with op.batch_alter_table("companies") as batch_op:
        batch_op.drop_constraint("uq_companies_normalized_name", type_="unique")
        batch_op.create_index("ix_companies_normalized_name", ["normalized_name"])


def downgrade() -> None:
    """Restore name uniqueness when the existing data permits it."""

    with op.batch_alter_table("companies") as batch_op:
        batch_op.drop_index("ix_companies_normalized_name")
        batch_op.create_unique_constraint("uq_companies_normalized_name", ["normalized_name"])
