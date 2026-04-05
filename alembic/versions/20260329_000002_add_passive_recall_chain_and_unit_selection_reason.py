"""passive_recall_chain on learning_sessions; selection_reason on session_units.

Revision ID: 20260329_000002
Revises: 20260329_000001
Create Date: 2026-03-29
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260329_000002"
down_revision: Union[str, Sequence[str], None] = "20260329_000001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("learning_sessions", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("passive_recall_chain", sa.String(length=16), nullable=True)
        )
    with op.batch_alter_table("session_units", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("selection_reason", sa.String(length=32), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("session_units", schema=None) as batch_op:
        batch_op.drop_column("selection_reason")
    with op.batch_alter_table("learning_sessions", schema=None) as batch_op:
        batch_op.drop_column("passive_recall_chain")
