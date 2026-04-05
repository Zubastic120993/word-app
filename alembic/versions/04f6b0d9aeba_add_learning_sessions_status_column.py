"""add learning_sessions status column

Revision ID: 04f6b0d9aeba
Revises: 71c88886a5ac
Create Date: 2026-03-15 02:13:24.165399

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '04f6b0d9aeba'
down_revision: Union[str, Sequence[str], None] = '71c88886a5ac'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "learning_sessions",
        sa.Column("status", sa.String(), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("learning_sessions", "status")
