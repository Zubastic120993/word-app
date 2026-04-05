"""Add FSRS fields to learning_progress.

Adds fsrs_stability, fsrs_difficulty, fsrs_last_review — Phase 1 of FSRS
integration. Fields are populated by the backfill script but not yet used
for scheduling (that is Phase 3).

Revision ID: 20260331_000001
Revises: 20260329_000003
Create Date: 2026-03-31
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260331_000001"
down_revision: Union[str, Sequence[str], None] = "20260329_000003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    if "learning_progress" not in inspector.get_table_names():
        return

    existing = {col["name"] for col in inspector.get_columns("learning_progress")}

    with op.batch_alter_table("learning_progress", schema=None) as batch_op:
        if "fsrs_stability" not in existing:
            batch_op.add_column(sa.Column("fsrs_stability", sa.Float(), nullable=True))
        if "fsrs_difficulty" not in existing:
            batch_op.add_column(sa.Column("fsrs_difficulty", sa.Float(), nullable=True))
        if "fsrs_last_review" not in existing:
            batch_op.add_column(sa.Column("fsrs_last_review", sa.DateTime(), nullable=True))


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    if "learning_progress" not in inspector.get_table_names():
        return

    existing = {col["name"] for col in inspector.get_columns("learning_progress")}

    with op.batch_alter_table("learning_progress", schema=None) as batch_op:
        if "fsrs_last_review" in existing:
            batch_op.drop_column("fsrs_last_review")
        if "fsrs_difficulty" in existing:
            batch_op.drop_column("fsrs_difficulty")
        if "fsrs_stability" in existing:
            batch_op.drop_column("fsrs_stability")
