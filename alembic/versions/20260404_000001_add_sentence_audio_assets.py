"""Add sentence_audio_assets table for cloze mode TTS caching.

Content-addressed cache for full context sentences (not tied to a specific
LearningUnit). Keyed on (audio_hash, engine, voice, language).

Revision ID: 20260404_000001
Revises: 20260331_000002
Create Date: 2026-04-04
"""

from alembic import op
import sqlalchemy as sa

revision = "20260404_000001"
down_revision = "20260331_000002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sentence_audio_assets",
        sa.Column("id", sa.Integer, primary_key=True, index=True),
        sa.Column("audio_hash", sa.String, nullable=False, index=True),
        sa.Column("engine", sa.String, nullable=False),
        sa.Column("voice", sa.String, nullable=False),
        sa.Column("language", sa.String, nullable=False),
        sa.Column("file_path", sa.String, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime,
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "audio_hash",
            "engine",
            "voice",
            "language",
            name="uq_sentence_audio_hash_engine_voice_lang",
        ),
    )


def downgrade() -> None:
    op.drop_table("sentence_audio_assets")
