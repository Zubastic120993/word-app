"""add practice_events table

Revision ID: 71c88886a5ac
Revises: 20260210_000001
Create Date: 2026-03-10 20:04:44.298067

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '71c88886a5ac'
down_revision: Union[str, Sequence[str], None] = '20260210_000001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "practice_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("event_type", sa.String(length=50), nullable=False),
        sa.Column("theme", sa.String(length=50), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("practice_events")
