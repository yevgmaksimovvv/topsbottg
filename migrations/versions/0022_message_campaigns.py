"""Create text message campaigns with frozen recipients and outbox references."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0022_message_campaigns"
down_revision = "0021_outgoing_attachment_staging"
branch_labels = None
depends_on = None

json_type = sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "message_campaigns",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("admin_telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("payload_fingerprint", sa.String(64), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("user_ids", json_type, nullable=False),
        sa.Column("study_group_ids", json_type, nullable=False),
        sa.Column("recipient_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("admin_telegram_id", "idempotency_key", name="uq_message_campaign_actor_key"),
    )
    op.create_table(
        "campaign_recipients",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("campaign_id", sa.Integer(), sa.ForeignKey("message_campaigns.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("full_name", sa.Text(), nullable=False),
        sa.Column("study_group_name", sa.Text(), nullable=True),
        sa.Column("conversation_id", sa.Integer(), sa.ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("message_id", sa.Integer(), sa.ForeignKey("chat_messages.id", ondelete="CASCADE"), unique=True, nullable=False),
        sa.Column("outbox_id", sa.Integer(), sa.ForeignKey("chat_message_outbox.id", ondelete="CASCADE"), unique=True, nullable=False),
        sa.Column("status", sa.String(8), server_default="queued", nullable=False),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("retry_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("campaign_id", "user_id", name="uq_campaign_recipients_user"),
        sa.CheckConstraint("status IN ('queued', 'sending', 'sent', 'failed')", name="ck_campaign_recipients_status"),
    )
    op.create_index(
        "ix_campaign_recipients_campaign_status", "campaign_recipients", ["campaign_id", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_campaign_recipients_campaign_status", table_name="campaign_recipients")
    op.drop_table("campaign_recipients")
    op.drop_table("message_campaigns")
