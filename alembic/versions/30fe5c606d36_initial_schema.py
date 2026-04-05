"""initial schema

Revision ID: 30fe5c606d36
Revises: 
Create Date: 2026-01-11 15:46:09.915045

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import sqlite

# revision identifiers, used by Alembic.
revision: str = '30fe5c606d36'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema - create all tables if they don't exist."""
    # Note: This migration reflects the current schema baseline.
    # For existing databases: Tables already exist, so this will be a no-op
    # (marked as applied with 'alembic stamp head' without running).
    # For new databases: This will create all tables.
    
    # Check which tables exist (using connection to inspect)
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    existing_tables = set(inspector.get_table_names())
    
    # Only create tables that don't exist
    if 'settings' not in existing_tables:
        op.create_table(
            'settings',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('offline_mode', sa.Boolean(), nullable=False),
            sa.Column('ai_provider', sa.String(), nullable=False),
            sa.Column('ollama_model', sa.String(), nullable=True),
            sa.Column('strict_mode', sa.Boolean(), nullable=False),
            sa.Column('source_language', sa.String(), nullable=False),
            sa.Column('target_language', sa.String(), nullable=False),
            sa.PrimaryKeyConstraint('id')
        )
    
    if 'learning_units' not in existing_tables:
        # Create learning_units table
        op.create_table(
            'learning_units',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('text', sa.String(), nullable=False),
            sa.Column('type', sa.String(), nullable=False),  # Enum stored as string in SQLite
            sa.Column('part_of_speech', sa.String(), nullable=True),
            sa.Column('translation', sa.String(), nullable=False),
            sa.Column('source_pdf', sa.String(), nullable=False),
            sa.Column('page_number', sa.Integer(), nullable=True),
            sa.Column('lesson_title', sa.String(), nullable=True),
            sa.Column('created_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
            sa.Column('normalized_text', sa.String(), nullable=True),
            sa.Column('normalized_translation', sa.String(), nullable=True),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('normalized_text', 'normalized_translation', name='uq_learning_unit_normalized')
        )
        op.create_index(op.f('ix_learning_units_id'), 'learning_units', ['id'], unique=False)
        op.create_index(op.f('ix_learning_units_normalized_text'), 'learning_units', ['normalized_text'], unique=False)
        op.create_index(op.f('ix_learning_units_normalized_translation'), 'learning_units', ['normalized_translation'], unique=False)
    
    if 'learning_progress' not in existing_tables:
        # Create learning_progress table
        op.create_table(
            'learning_progress',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('unit_id', sa.Integer(), nullable=False),
            sa.Column('times_seen', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('times_correct', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('times_failed', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('confidence_score', sa.Float(), nullable=False, server_default='0.0'),
            sa.Column('last_seen', sa.DateTime(), nullable=True),
            sa.Column('last_recall_result', sa.String(), nullable=True),  # Enum stored as string in SQLite
            sa.Column('next_review_at', sa.DateTime(), nullable=True),
            sa.Column('introduced_at', sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(['unit_id'], ['learning_units.id'], ),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('unit_id')
        )
        op.create_index(op.f('ix_learning_progress_id'), 'learning_progress', ['id'], unique=False)
    
    if 'learning_sessions' not in existing_tables:
        # Create learning_sessions table
        op.create_table(
            'learning_sessions',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('created_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
            sa.Column('mode', sa.String(), nullable=False),  # Enum stored as string in SQLite
            sa.Column('locked', sa.Boolean(), nullable=False, server_default='1'),
            sa.Column('completed', sa.Boolean(), nullable=False, server_default='0'),
            sa.Column('completed_at', sa.DateTime(), nullable=True),
            sa.Column('summary_total_units', sa.Integer(), nullable=True),
            sa.Column('summary_answered_units', sa.Integer(), nullable=True),
            sa.Column('summary_correct_count', sa.Integer(), nullable=True),
            sa.Column('summary_partial_count', sa.Integer(), nullable=True),
            sa.Column('summary_failed_count', sa.Integer(), nullable=True),
            sa.PrimaryKeyConstraint('id')
        )
        op.create_index(op.f('ix_learning_sessions_id'), 'learning_sessions', ['id'], unique=False)
    
    if 'session_units' not in existing_tables:
        # Create session_units table
        op.create_table(
            'session_units',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('session_id', sa.Integer(), nullable=False),
            sa.Column('unit_id', sa.Integer(), nullable=False),
            sa.Column('position', sa.Integer(), nullable=False),
            sa.Column('answered', sa.Boolean(), nullable=False, server_default='0'),
            sa.Column('is_correct', sa.Boolean(), nullable=True),
            sa.Column('recall_result', sa.String(), nullable=True),  # Enum stored as string in SQLite
            sa.Column('user_input', sa.String(), nullable=True),
            sa.Column('answered_at', sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(['session_id'], ['learning_sessions.id'], ),
            sa.ForeignKeyConstraint(['unit_id'], ['learning_units.id'], ),
            sa.PrimaryKeyConstraint('id')
        )
        op.create_index(op.f('ix_session_units_id'), 'session_units', ['id'], unique=False)
    
    if 'audio_assets' not in existing_tables:
        # Create audio_assets table
        op.create_table(
            'audio_assets',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('unit_id', sa.Integer(), nullable=False),
            sa.Column('engine', sa.String(), nullable=False),
            sa.Column('voice', sa.String(), nullable=False),
            sa.Column('language', sa.String(), nullable=False),
            sa.Column('audio_hash', sa.String(), nullable=False),
            sa.Column('file_path', sa.String(), nullable=False),
            sa.Column('created_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
            sa.ForeignKeyConstraint(['unit_id'], ['learning_units.id'], ondelete='CASCADE'),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('unit_id', 'engine', 'voice', 'language', name='uq_audio_asset_unit_engine_voice_language')
        )
        op.create_index(op.f('ix_audio_assets_id'), 'audio_assets', ['id'], unique=False)
        op.create_index(op.f('ix_audio_assets_unit_id'), 'audio_assets', ['unit_id'], unique=False)
        op.create_index(op.f('ix_audio_assets_audio_hash'), 'audio_assets', ['audio_hash'], unique=False)


def downgrade() -> None:
    """Downgrade schema - drop all tables."""
    # Note: This will drop all tables. Use with caution.
    op.drop_index(op.f('ix_audio_assets_audio_hash'), table_name='audio_assets')
    op.drop_index(op.f('ix_audio_assets_unit_id'), table_name='audio_assets')
    op.drop_index(op.f('ix_audio_assets_id'), table_name='audio_assets')
    op.drop_table('audio_assets')
    op.drop_index(op.f('ix_session_units_id'), table_name='session_units')
    op.drop_table('session_units')
    op.drop_index(op.f('ix_learning_sessions_id'), table_name='learning_sessions')
    op.drop_table('learning_sessions')
    op.drop_index(op.f('ix_learning_progress_id'), table_name='learning_progress')
    op.drop_table('learning_progress')
    op.drop_index(op.f('ix_learning_units_normalized_translation'), table_name='learning_units')
    op.drop_index(op.f('ix_learning_units_normalized_text'), table_name='learning_units')
    op.drop_index(op.f('ix_learning_units_id'), table_name='learning_units')
    op.drop_table('learning_units')
    op.drop_table('settings')
