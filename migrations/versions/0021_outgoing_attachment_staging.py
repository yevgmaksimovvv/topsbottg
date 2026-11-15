"""Add persistent staging references for outgoing attachments."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0021_outgoing_attachment_staging"
down_revision = "0020_incoming_attachments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("chat_attachments") as batch:
        batch.alter_column("telegram_file_id", existing_type=sa.Text(), nullable=True)
        batch.add_column(sa.Column("staging_key", sa.String(64), nullable=True))
    op.create_index("ix_chat_attachments_staging_key", "chat_attachments", ["staging_key"])


def downgrade() -> None:
    op.execute("DELETE FROM chat_attachments WHERE telegram_file_id IS NULL")
    op.drop_index("ix_chat_attachments_staging_key", table_name="chat_attachments")
    with op.batch_alter_table("chat_attachments") as batch:
        batch.drop_column("staging_key")
        batch.alter_column("telegram_file_id", existing_type=sa.Text(), nullable=False)
