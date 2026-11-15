"""Persist aiogram FSM state and processed sensitive Telegram message identifiers."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0017_persistent_bot_fsm"
down_revision = "0016_conversation_storage"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "bot_fsm_states",
        sa.Column("storage_key", sa.String(512), primary_key=True),
        sa.Column("state", sa.String(128), nullable=True),
        sa.Column("data", sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql"), nullable=False),
    )
    op.create_table(
        "bot_sensitive_messages",
        sa.Column("telegram_chat_id", sa.BigInteger(), primary_key=True),
        sa.Column("telegram_message_id", sa.BigInteger(), primary_key=True),
    )


def downgrade() -> None:
    op.drop_table("bot_sensitive_messages")
    op.drop_table("bot_fsm_states")
