"""Add stability_score column to learning_progress.

Adds stability_score (Float, default=0.0) to represent long-term memory
maturity, complementing the existing confidence_score.

Revision ID: 20260210_000001
Revises: 20260126_000001
Create Date: 2026-02-10
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "20260210_000001"
down_revision: Union[str, Sequence[str], None] = "20260126_000001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add stability_score column to learning_progress."""
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    # Check if learning_progress table exists
    if "learning_progress" not in inspector.get_table_names():
        return  # Nothing to do if table doesn't exist

    # Get existing columns
    existing_columns = {col["name"] for col in inspector.get_columns("learning_progress")}

    # Add stability_score column if it doesn't exist
    if "stability_score" not in existing_columns:
        op.add_column(
            "learning_progress",
            sa.Column("stability_score", sa.Float(), nullable=False, server_default="0.0"),
        )


def downgrade() -> None:
    """Remove stability_score column."""
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    if "learning_progress" not in inspector.get_table_names():
        return

    existing_columns = {col["name"] for col in inspector.get_columns("learning_progress")}

    if "stability_score" in existing_columns:
        op.drop_column("learning_progress", "stability_score")
