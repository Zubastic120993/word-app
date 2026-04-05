"""Repair migration for stamped-but-empty databases.

Some local setups may have an SQLite DB where Alembic versions were stamped as applied
without actually creating the baseline tables. This migration is idempotent and will
create any missing tables/indexes needed by the current app schema.

Revision ID: 20260115_000002
Revises: 20260115_000001
Create Date: 2026-01-15
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "20260115_000002"
down_revision: Union[str, Sequence[str], None] = "20260115_000001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    existing_tables = set(inspector.get_table_names())

    # ----------------
    # vocabularies
    # ----------------
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

    # ----------------
    # settings
    # ----------------
    if "settings" not in existing_tables:
        op.create_table(
            "settings",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("offline_mode", sa.Boolean(), nullable=False),
            sa.Column("ai_provider", sa.String(), nullable=False),
            sa.Column("ollama_model", sa.String(), nullable=True),
            sa.Column("strict_mode", sa.Boolean(), nullable=False),
            sa.Column("source_language", sa.String(), nullable=False),
            sa.Column("target_language", sa.String(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )

    # ----------------
    # learning_units
    # ----------------
    if "learning_units" not in existing_tables:
        op.create_table(
            "learning_units",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("text", sa.String(), nullable=False),
            sa.Column("type", sa.String(), nullable=False),
            sa.Column("part_of_speech", sa.String(), nullable=True),
            sa.Column("translation", sa.String(), nullable=False),
            sa.Column("source_pdf", sa.String(), nullable=False),
            sa.Column("vocabulary_id", sa.Integer(), nullable=True),
            sa.Column("page_number", sa.Integer(), nullable=True),
            sa.Column("lesson_title", sa.String(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(),
                server_default=sa.text("(CURRENT_TIMESTAMP)"),
                nullable=False,
            ),
            sa.Column("normalized_text", sa.String(), nullable=True),
            sa.Column("normalized_translation", sa.String(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("normalized_text", "normalized_translation", name="uq_learning_unit_normalized"),
        )
        op.create_index(op.f("ix_learning_units_id"), "learning_units", ["id"], unique=False)
        op.create_index(op.f("ix_learning_units_normalized_text"), "learning_units", ["normalized_text"], unique=False)
        op.create_index(
            op.f("ix_learning_units_normalized_translation"),
            "learning_units",
            ["normalized_translation"],
            unique=False,
        )
        op.create_index("ix_learning_units_vocabulary_id", "learning_units", ["vocabulary_id"], unique=False)
    else:
        cols = {c["name"] for c in inspector.get_columns("learning_units")}
        if "vocabulary_id" not in cols:
            with op.batch_alter_table("learning_units") as batch_op:
                batch_op.add_column(sa.Column("vocabulary_id", sa.Integer(), nullable=True))
                batch_op.create_index("ix_learning_units_vocabulary_id", ["vocabulary_id"], unique=False)

    # ----------------
    # learning_progress
    # ----------------
    if "learning_progress" not in existing_tables:
        op.create_table(
            "learning_progress",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("unit_id", sa.Integer(), nullable=False),
            sa.Column("times_seen", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("times_correct", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("times_failed", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("confidence_score", sa.Float(), nullable=False, server_default="0.0"),
            sa.Column("last_seen", sa.DateTime(), nullable=True),
            sa.Column("last_recall_result", sa.String(), nullable=True),
            sa.Column("next_review_at", sa.DateTime(), nullable=True),
            sa.Column("introduced_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(["unit_id"], ["learning_units.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("unit_id"),
        )
        op.create_index(op.f("ix_learning_progress_id"), "learning_progress", ["id"], unique=False)

    # ----------------
    # learning_sessions
    # ----------------
    if "learning_sessions" not in existing_tables:
        op.create_table(
            "learning_sessions",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(),
                server_default=sa.text("(CURRENT_TIMESTAMP)"),
                nullable=False,
            ),
            sa.Column("mode", sa.String(), nullable=False),
            sa.Column("locked", sa.Boolean(), nullable=False, server_default="1"),
            sa.Column("completed", sa.Boolean(), nullable=False, server_default="0"),
            sa.Column("completed_at", sa.DateTime(), nullable=True),
            sa.Column("summary_total_units", sa.Integer(), nullable=True),
            sa.Column("summary_answered_units", sa.Integer(), nullable=True),
            sa.Column("summary_correct_count", sa.Integer(), nullable=True),
            sa.Column("summary_partial_count", sa.Integer(), nullable=True),
            sa.Column("summary_failed_count", sa.Integer(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(op.f("ix_learning_sessions_id"), "learning_sessions", ["id"], unique=False)

    # ----------------
    # session_units
    # ----------------
    if "session_units" not in existing_tables:
        op.create_table(
            "session_units",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("session_id", sa.Integer(), nullable=False),
            sa.Column("unit_id", sa.Integer(), nullable=False),
            sa.Column("position", sa.Integer(), nullable=False),
            sa.Column("answered", sa.Boolean(), nullable=False, server_default="0"),
            sa.Column("is_correct", sa.Boolean(), nullable=True),
            sa.Column("recall_result", sa.String(), nullable=True),
            sa.Column("user_input", sa.String(), nullable=True),
            sa.Column("answered_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(["session_id"], ["learning_sessions.id"]),
            sa.ForeignKeyConstraint(["unit_id"], ["learning_units.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(op.f("ix_session_units_id"), "session_units", ["id"], unique=False)

    # ----------------
    # audio_assets
    # ----------------
    if "audio_assets" not in existing_tables:
        op.create_table(
            "audio_assets",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("unit_id", sa.Integer(), nullable=False),
            sa.Column("engine", sa.String(), nullable=False),
            sa.Column("voice", sa.String(), nullable=False),
            sa.Column("language", sa.String(), nullable=False),
            sa.Column("audio_hash", sa.String(), nullable=False),
            sa.Column("file_path", sa.String(), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(),
                server_default=sa.text("(CURRENT_TIMESTAMP)"),
                nullable=False,
            ),
            sa.ForeignKeyConstraint(["unit_id"], ["learning_units.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "unit_id",
                "engine",
                "voice",
                "language",
                name="uq_audio_asset_unit_engine_voice_language",
            ),
        )
        op.create_index(op.f("ix_audio_assets_id"), "audio_assets", ["id"], unique=False)
        op.create_index(op.f("ix_audio_assets_unit_id"), "audio_assets", ["unit_id"], unique=False)
        op.create_index(op.f("ix_audio_assets_audio_hash"), "audio_assets", ["audio_hash"], unique=False)


def downgrade() -> None:
    # Intentionally non-destructive: this migration only repairs missing tables.
    pass

