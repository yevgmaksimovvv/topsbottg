"""Add persistent staging references for outgoing attachments."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0021_outgoing_attachment_staging"
down_revision = "0020_incoming_attachments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("chat_attachments", "telegram_file_id", existing_type=sa.Text(), nullable=True)
    op.add_column("chat_attachments", sa.Column("staging_key", sa.String(64), nullable=True))


def downgrade() -> None:
    op.execute("DELETE FROM chat_attachments WHERE telegram_file_id IS NULL")
    op.drop_column("chat_attachments", "staging_key")
    op.alter_column("chat_attachments", "telegram_file_id", existing_type=sa.Text(), nullable=False)
