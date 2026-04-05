"""Add vocabulary_groups table and vocabularies.group_id

This migration:
1. Creates the vocabulary_groups table
2. Adds group_id column to vocabularies table
3. Creates default groups based on filename patterns
4. Auto-assigns vocabularies to their groups

Revision ID: 20260121_000001
Revises: 20260115_000002
Create Date: 2026-01-21
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "20260121_000001"
down_revision: Union[str, Sequence[str], None] = "20260115_000002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Default user key for single-user mode
DEFAULT_USER_KEY = "local"

# Group definitions: (name, description, display_order, filename_pattern)
GROUPS = [
    ("Czytaj Po Polsku - Level 1", "Stories for beginners (A1)", 1, "czytaj_01_"),
    ("Czytaj Po Polsku - Level 2", "Stories for elementary learners (A2)", 2, "czytaj_02_"),
    ("Czytaj Po Polsku - Level 3", "Stories for intermediate learners (B1)", 3, "czytaj_03_"),
    ("Czytaj Po Polsku - Level 4", "Stories for upper-intermediate learners (B2)", 4, "czytaj_04_"),
    ("Polish-Ukrainian Dictionary", "Structured vocabulary lessons", 5, "polish_ukrainian_dictionary_"),
]


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    existing_tables = set(inspector.get_table_names())

    # ----------------
    # Create vocabulary_groups table
    # ----------------
    if "vocabulary_groups" not in existing_tables:
        op.create_table(
            "vocabulary_groups",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("user_key", sa.String(), nullable=False),
            sa.Column("name", sa.String(), nullable=False),
            sa.Column("description", sa.String(), nullable=True),
            sa.Column("display_order", sa.Integer(), nullable=False, server_default="0"),
            sa.Column(
                "created_at",
                sa.DateTime(),
                server_default=sa.text("(CURRENT_TIMESTAMP)"),
                nullable=False,
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("user_key", "name", name="uq_vocabulary_groups_user_key_name"),
        )
        op.create_index(op.f("ix_vocabulary_groups_id"), "vocabulary_groups", ["id"], unique=False)
        op.create_index(op.f("ix_vocabulary_groups_user_key"), "vocabulary_groups", ["user_key"], unique=False)
        op.create_index(op.f("ix_vocabulary_groups_name"), "vocabulary_groups", ["name"], unique=False)

    # ----------------
    # Add group_id column to vocabularies
    # ----------------
    if "vocabularies" in existing_tables:
        cols = {c["name"] for c in inspector.get_columns("vocabularies")}
        if "group_id" not in cols:
            with op.batch_alter_table("vocabularies") as batch_op:
                batch_op.add_column(sa.Column("group_id", sa.Integer(), nullable=True))
                batch_op.create_index("ix_vocabularies_group_id", ["group_id"], unique=False)

    # ----------------
    # Create default groups and assign vocabularies
    # ----------------
    # Insert groups
    for name, description, display_order, pattern in GROUPS:
        conn.execute(
            sa.text(
                "INSERT OR IGNORE INTO vocabulary_groups (user_key, name, description, display_order) "
                "VALUES (:user_key, :name, :description, :display_order)"
            ),
            {"user_key": DEFAULT_USER_KEY, "name": name, "description": description, "display_order": display_order},
        )

    # Assign vocabularies to groups based on filename patterns
    for name, description, display_order, pattern in GROUPS:
        # Get the group ID
        result = conn.execute(
            sa.text("SELECT id FROM vocabulary_groups WHERE user_key = :user_key AND name = :name"),
            {"user_key": DEFAULT_USER_KEY, "name": name},
        )
        row = result.fetchone()
        if row:
            group_id = row[0]
            # Update vocabularies matching the pattern
            conn.execute(
                sa.text(
                    "UPDATE vocabularies SET group_id = :group_id "
                    "WHERE name LIKE :pattern AND group_id IS NULL"
                ),
                {"group_id": group_id, "pattern": f"{pattern}%"},
            )


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    existing_tables = set(inspector.get_table_names())

    # Remove group_id from vocabularies
    if "vocabularies" in existing_tables:
        cols = {c["name"] for c in inspector.get_columns("vocabularies")}
        if "group_id" in cols:
            with op.batch_alter_table("vocabularies") as batch_op:
                batch_op.drop_index("ix_vocabularies_group_id")
                batch_op.drop_column("group_id")

    # Drop vocabulary_groups table
    if "vocabulary_groups" in existing_tables:
        op.drop_index(op.f("ix_vocabulary_groups_name"), table_name="vocabulary_groups")
        op.drop_index(op.f("ix_vocabulary_groups_user_key"), table_name="vocabulary_groups")
        op.drop_index(op.f("ix_vocabulary_groups_id"), table_name="vocabulary_groups")
        op.drop_table("vocabulary_groups")
