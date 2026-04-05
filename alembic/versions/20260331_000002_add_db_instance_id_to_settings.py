"""Add db_instance_id to settings.

Adds a persistent UUID marker so logs can identify which DB file is in use,
even if the on-disk path stays the same but the file contents change.

Revision ID: 20260331_000002
Revises: 20260331_000001
Create Date: 2026-03-31
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260331_000002"
down_revision: Union[str, Sequence[str], None] = "20260331_000001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    if "settings" not in inspector.get_table_names():
        return

    existing = {col["name"] for col in inspector.get_columns("settings")}

    with op.batch_alter_table("settings", schema=None) as batch_op:
        if "db_instance_id" not in existing:
            batch_op.add_column(sa.Column("db_instance_id", sa.String(), nullable=True))


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    if "settings" not in inspector.get_table_names():
        return

    existing = {col["name"] for col in inspector.get_columns("settings")}

    with op.batch_alter_table("settings", schema=None) as batch_op:
        if "db_instance_id" in existing:
            batch_op.drop_column("db_instance_id")

