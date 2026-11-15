"""Queue outgoing conversation messages for reliable Telegram delivery."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0019_chat_message_outbox"
down_revision = "0018_persistent_bot_fsm"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "chat_message_outbox",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "message_id", sa.Integer(),
            sa.ForeignKey("chat_messages.id", ondelete="CASCADE"), nullable=False, unique=True,
        ),
        sa.Column(
            "conversation_id", sa.Integer(),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("admin_telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("payload_fingerprint", sa.String(length=64), nullable=False),
        sa.Column(
            "reply_to_message_id", sa.Integer(),
            sa.ForeignKey("chat_messages.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column("status", sa.String(length=8), server_default="queued", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claimed_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claim_token", sa.String(length=48), nullable=True),
        sa.Column("telegram_message_id", sa.BigInteger(), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "status IN ('queued', 'sending', 'sent', 'failed')", name="ck_chat_outbox_status",
        ),
        sa.CheckConstraint("attempts >= 0", name="ck_chat_outbox_attempts"),
        sa.UniqueConstraint(
            "conversation_id", "admin_telegram_id", "idempotency_key",
            name="uq_chat_outbox_idempotency",
        ),
    )
    op.create_index(
        "ix_chat_outbox_claim", "chat_message_outbox",
        ["status", "retry_at", "claimed_until"],
    )


def downgrade() -> None:
    op.drop_index("ix_chat_outbox_claim", table_name="chat_message_outbox")
    op.drop_table("chat_message_outbox")
