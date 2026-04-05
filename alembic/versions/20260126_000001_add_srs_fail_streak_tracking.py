"""Add SRS fail streak tracking columns.

Adds recall_fail_streak and is_blocked columns to learning_progress
for tracking consecutive recall failures and flagging blocked words.

Revision ID: 20260126_000001
Revises: 20260121_000001
Create Date: 2026-01-26
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "20260126_000001"
down_revision: Union[str, Sequence[str], None] = "20260121_000001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add recall_fail_streak and is_blocked columns to learning_progress."""
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    
    # Check if learning_progress table exists
    if "learning_progress" not in inspector.get_table_names():
        return  # Nothing to do if table doesn't exist
    
    # Get existing columns
    existing_columns = {col["name"] for col in inspector.get_columns("learning_progress")}
    
    # Add recall_fail_streak column if it doesn't exist
    if "recall_fail_streak" not in existing_columns:
        op.add_column(
            "learning_progress",
            sa.Column("recall_fail_streak", sa.Integer(), nullable=False, server_default="0"),
        )
    
    # Add is_blocked column if it doesn't exist
    if "is_blocked" not in existing_columns:
        op.add_column(
            "learning_progress",
            sa.Column("is_blocked", sa.Boolean(), nullable=False, server_default="0"),
        )


def downgrade() -> None:
    """Remove recall_fail_streak and is_blocked columns."""
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    
    if "learning_progress" not in inspector.get_table_names():
        return
    
    existing_columns = {col["name"] for col in inspector.get_columns("learning_progress")}
    
    if "is_blocked" in existing_columns:
        op.drop_column("learning_progress", "is_blocked")
    
    if "recall_fail_streak" in existing_columns:
        op.drop_column("learning_progress", "recall_fail_streak")
