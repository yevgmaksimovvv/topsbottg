"""Track payout readiness notifications discarded after state changes.

Revision ID: 0010_discard_ready_notifications
Revises: 0009_payout_delete_notifications
"""
import sqlalchemy as sa
from alembic import op

revision = "0010_discard_ready_notifications"
down_revision = "0009_payout_delete_notifications"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "payout_ready_notifications",
        sa.Column("discarded_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade():
    op.drop_column("payout_ready_notifications", "discarded_at")
