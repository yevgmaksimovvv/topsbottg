"""Remove the Telegram profile cache and unused invitation retry counters.

Revision ID: 0015_mtproto_admin_lookup
Revises: 0014_admin_invitation_delivery
"""
import sqlalchemy as sa
from alembic import op

revision = "0015_mtproto_admin_lookup"
down_revision = "0014_admin_invitation_delivery"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_index("ix_users_telegram_username_lower", table_name="users")
    op.drop_column("users", "telegram_username")
    op.drop_column("users", "telegram_display_name")
    op.drop_column("users", "telegram_profile_seen_at")
    op.drop_index("ix_admin_invitations_delivery", table_name="admin_invitations")
    op.drop_column("admin_invitations", "next_delivery_at")
    op.drop_column("admin_invitations", "delivery_attempts")


def downgrade():
    op.add_column("users", sa.Column("telegram_username", sa.String(32), nullable=True))
    op.add_column("users", sa.Column("telegram_display_name", sa.String(256), nullable=True))
    op.add_column("users", sa.Column("telegram_profile_seen_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_users_telegram_username_lower", "users", [sa.text("lower(telegram_username)")])
    op.add_column(
        "admin_invitations",
        sa.Column("delivery_attempts", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("admin_invitations", sa.Column("next_delivery_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index(
        "ix_admin_invitations_delivery", "admin_invitations", ["status", "next_delivery_at"]
    )
