"""Soft delete payouts and persistent ready notifications.

Revision ID: 0009_payout_delete_notifications
Revises: 0008_payout_workflow_snapshots
"""
import sqlalchemy as sa
from alembic import op

revision = "0009_payout_delete_notifications"
down_revision = "0008_payout_workflow_snapshots"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("payouts", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_payouts_deleted_id", "payouts", ["deleted_at", "id"])
    op.create_table("payout_ready_notifications",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("payout_id", sa.Integer(), sa.ForeignKey("payouts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claimed_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("payout_id", name="uq_payout_ready_payout"))
    op.create_index("ix_payout_ready_pending", "payout_ready_notifications", ["delivered_at", "next_attempt_at"])
    op.create_index("ix_payment_replies_recipient", "payment_replies", ["payout_recipient_id", "id"])


def downgrade():
    op.drop_index("ix_payment_replies_recipient", table_name="payment_replies")
    op.drop_index("ix_payout_ready_pending", table_name="payout_ready_notifications")
    op.drop_table("payout_ready_notifications")
    op.drop_index("ix_payouts_deleted_id", table_name="payouts")
    op.drop_column("payouts", "deleted_at")
