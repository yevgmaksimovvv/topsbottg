"""Persist Telegram attachment metadata without exposing Telegram file identifiers."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0019_incoming_attachments"
down_revision = "0018_persistent_bot_fsm"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("chat_messages", sa.Column("caption", sa.Text(), nullable=True))
    op.create_table(
        "chat_attachments",
        sa.Column(
            "chat_message_id", sa.Integer(),
            sa.ForeignKey("chat_messages.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("telegram_file_id", sa.Text(), nullable=False),
        sa.Column("telegram_file_unique_id", sa.Text(), nullable=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("filename", sa.Text(), nullable=True),
        sa.Column("size", sa.BigInteger(), nullable=True),
        sa.Column("mime_type", sa.String(255), nullable=True),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("duration", sa.Integer(), nullable=True),
        sa.Column("available", sa.Boolean(), nullable=False, server_default=sa.true()),
    )


def downgrade() -> None:
    op.drop_table("chat_attachments")
    op.drop_column("chat_messages", "caption")
