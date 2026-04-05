"""learning_sessions.due_only — scope daily due-review cap to due-only practice.

Revision ID: 20260329_000001
Revises: 20260328_000001
Create Date: 2026-03-29
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260329_000001"
down_revision: Union[str, Sequence[str], None] = "20260328_000001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("learning_sessions", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "due_only",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("learning_sessions", schema=None) as batch_op:
        batch_op.drop_column("due_only")
