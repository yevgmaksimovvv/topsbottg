"""Store per-user conversations, Telegram messages, and per-admin read markers."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0016_conversation_storage"
down_revision = "0015_admin_directory_cleanup"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "conversations",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("user_id", name="uq_conversations_user_id"),
    )
    op.create_index(
        "ix_conversations_last_message_at_id",
        "conversations",
        [sa.text("last_message_at DESC"), sa.text("id DESC")],
    )

    op.create_table(
        "chat_messages",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "conversation_id", sa.Integer(), sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("direction", sa.String(8), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False, server_default="text"),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("telegram_chat_id", sa.BigInteger(), nullable=True),
        sa.Column("telegram_message_id", sa.BigInteger(), nullable=True),
        sa.Column("reply_to_telegram_message_id", sa.BigInteger(), nullable=True),
        sa.Column("author_admin_telegram_id", sa.BigInteger(), nullable=True),
        sa.Column("source", sa.String(32), nullable=False, server_default="telegram"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("direction IN ('incoming', 'outgoing')", name="ck_chat_messages_direction"),
    )
    op.create_index(
        "ix_chat_messages_conversation_id_id", "chat_messages", ["conversation_id", "id"]
    )
    op.create_index(
        "uq_chat_messages_telegram_direction",
        "chat_messages",
        ["direction", "telegram_chat_id", "telegram_message_id"],
        unique=True,
        postgresql_where=sa.text("telegram_chat_id IS NOT NULL AND telegram_message_id IS NOT NULL"),
    )

    op.create_table(
        "conversation_reads",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "conversation_id", sa.Integer(), sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("admin_telegram_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "last_read_message_id", sa.Integer(), sa.ForeignKey("chat_messages.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.UniqueConstraint("conversation_id", "admin_telegram_id", name="uq_conversation_reads_admin"),
    )


def downgrade() -> None:
    op.drop_table("conversation_reads")
    op.drop_index("uq_chat_messages_telegram_direction", table_name="chat_messages")
    op.drop_index("ix_chat_messages_conversation_id_id", table_name="chat_messages")
    op.drop_table("chat_messages")
    op.drop_index("ix_conversations_last_message_at_id", table_name="conversations")
    op.drop_table("conversations")
