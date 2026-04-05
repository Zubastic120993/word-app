"""add chat_state table"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "20260405_000001"
down_revision = "20260404_000001"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "chat_state",
        sa.Column("id", sa.Integer(), primary_key=True),

        sa.Column("conversation_json", sa.JSON(), nullable=True),
        sa.Column("explained_bases_json", sa.JSON(), nullable=True),
        sa.Column("user_produced_json", sa.JSON(), nullable=True),
        sa.Column("assistant_exposed_json", sa.JSON(), nullable=True),

        sa.Column("session_vocab_json", sa.JSON(), nullable=True),
        sa.Column("session_vocab_active", sa.Boolean(), nullable=False, server_default=sa.false()),

        sa.Column("theme_user_messages", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("current_theme", sa.Text(), nullable=True),
        sa.Column("checkpoint_done", sa.Boolean(), nullable=False, server_default=sa.false()),

        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )


def downgrade():
    op.drop_table("chat_state")
