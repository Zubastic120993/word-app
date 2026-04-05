"""LearningUnit context sentences; SessionUnit cloze exercise fields.

Revision ID: 20260329_000003
Revises: 20260329_000002
Create Date: 2026-03-29
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260329_000003"
down_revision: Union[str, Sequence[str], None] = "20260329_000002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("learning_units", schema=None) as batch_op:
        batch_op.add_column(sa.Column("context_sentence", sa.String(), nullable=True))
        batch_op.add_column(
            sa.Column("context_sentence_translation", sa.String(), nullable=True)
        )
    with op.batch_alter_table("session_units", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("exercise_type", sa.String(length=16), nullable=False, server_default="recall")
        )
        batch_op.add_column(sa.Column("cloze_prompt", sa.String(), nullable=True))
        batch_op.add_column(
            sa.Column("context_sentence_translation", sa.String(), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("session_units", schema=None) as batch_op:
        batch_op.drop_column("context_sentence_translation")
        batch_op.drop_column("cloze_prompt")
        batch_op.drop_column("exercise_type")
    with op.batch_alter_table("learning_units", schema=None) as batch_op:
        batch_op.drop_column("context_sentence_translation")
        batch_op.drop_column("context_sentence")
