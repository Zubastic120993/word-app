"""Add vocabularies table and learning_units.vocabulary_id

Revision ID: 20260115_000001
Revises: 30fe5c606d36
Create Date: 2026-01-15
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "20260115_000001"
down_revision: Union[str, Sequence[str], None] = "30fe5c606d36"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    existing_tables = set(inspector.get_table_names())

    if "vocabularies" not in existing_tables:
        op.create_table(
            "vocabularies",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("user_key", sa.String(), nullable=False),
            sa.Column("name", sa.String(), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(),
                server_default=sa.text("(CURRENT_TIMESTAMP)"),
                nullable=False,
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("user_key", "name", name="uq_vocabularies_user_key_name"),
        )
        op.create_index(op.f("ix_vocabularies_id"), "vocabularies", ["id"], unique=False)
        op.create_index(op.f("ix_vocabularies_user_key"), "vocabularies", ["user_key"], unique=False)
        op.create_index(op.f("ix_vocabularies_name"), "vocabularies", ["name"], unique=False)

    # Add learning_units.vocabulary_id if missing
    if "learning_units" in existing_tables:
        cols = {c["name"] for c in inspector.get_columns("learning_units")}
        if "vocabulary_id" not in cols:
            with op.batch_alter_table("learning_units") as batch_op:
                batch_op.add_column(sa.Column("vocabulary_id", sa.Integer(), nullable=True))
                batch_op.create_index("ix_learning_units_vocabulary_id", ["vocabulary_id"], unique=False)


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    existing_tables = set(inspector.get_table_names())

    if "learning_units" in existing_tables:
        cols = {c["name"] for c in inspector.get_columns("learning_units")}
        if "vocabulary_id" in cols:
            with op.batch_alter_table("learning_units") as batch_op:
                batch_op.drop_index("ix_learning_units_vocabulary_id")
                batch_op.drop_column("vocabulary_id")

    if "vocabularies" in existing_tables:
        op.drop_index(op.f("ix_vocabularies_name"), table_name="vocabularies")
        op.drop_index(op.f("ix_vocabularies_user_key"), table_name="vocabularies")
        op.drop_index(op.f("ix_vocabularies_id"), table_name="vocabularies")
        op.drop_table("vocabularies")
