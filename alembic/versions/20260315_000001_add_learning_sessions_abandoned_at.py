"""add learning_sessions abandoned_at column

Revision ID: 20260315_000001
Revises: 04f6b0d9aeba
Create Date: 2026-03-15 02:20:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "20260315_000001"
down_revision: Union[str, Sequence[str], None] = "04f6b0d9aeba"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "learning_sessions",
        sa.Column("abandoned_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("learning_sessions", "abandoned_at")
