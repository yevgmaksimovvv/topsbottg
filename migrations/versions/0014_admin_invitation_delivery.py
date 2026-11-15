"""Track uncertain Telegram invitation delivery without duplicate sends.

Revision ID: 0014_admin_invitation_delivery
Revises: 0013_admin_invitations
"""
import sqlalchemy as sa
from alembic import op

revision = "0014_admin_invitation_delivery"
down_revision = "0013_admin_invitations"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "admin_invitations",
        sa.Column("delivery_state", sa.String(16), nullable=False, server_default="queued"),
    )
    op.execute(
        "UPDATE admin_invitations SET delivery_state = 'delivered' WHERE delivered_at IS NOT NULL"
    )
    # Existing attempts may have succeeded before the process stopped.
    op.execute(
        "UPDATE admin_invitations SET delivery_state = 'unknown' "
        "WHERE delivered_at IS NULL AND delivery_attempts > 0"
    )


def downgrade():
    op.drop_column("admin_invitations", "delivery_state")
