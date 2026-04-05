"""add track_type and lesson_index to vocabularies

Revision ID: 20260328_000001
Revises: 20260315_000001
Create Date: 2026-03-28

Curriculum metadata: PL–UA track + optional lesson index (Phase 1 schema only).
Uses batch_alter_table so SQLite can apply a CHECK constraint safely.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260328_000001"
down_revision: Union[str, Sequence[str], None] = "20260315_000001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("vocabularies", schema=None) as batch_op:
        batch_op.add_column(sa.Column("track_type", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("lesson_index", sa.Integer(), nullable=True))
        batch_op.create_check_constraint(
            "ck_vocab_track_type",
            sa.text(
                "track_type IN ('plua', 'czytaj', 'other') OR track_type IS NULL"
            ),
        )


def downgrade() -> None:
    with op.batch_alter_table("vocabularies", schema=None) as batch_op:
        batch_op.drop_constraint("ck_vocab_track_type", type_="check")
        batch_op.drop_column("lesson_index")
        batch_op.drop_column("track_type")
