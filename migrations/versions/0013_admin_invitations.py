"""Remember Telegram profiles and allow confirmed admin invitations.

Revision ID: 0013_admin_invitations
Revises: 0012_admin_grants
"""

import sqlalchemy as sa
from alembic import op

revision = "0013_admin_invitations"
down_revision = "0012_admin_grants"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("telegram_username", sa.String(32), nullable=True))
        batch.add_column(sa.Column("telegram_display_name", sa.String(256), nullable=True))
        batch.add_column(sa.Column("telegram_profile_seen_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_users_telegram_username_lower", "users", [sa.text("lower(telegram_username)")])

    op.create_table(
        "admin_invitations",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("target_telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("created_by_owner_id", sa.BigInteger(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivery_attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("next_delivery_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_delivery_error", sa.String(80), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index(
        "uq_admin_invitations_one_pending",
        "admin_invitations",
        ["target_telegram_id"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
        sqlite_where=sa.text("status = 'pending'"),
    )
    op.create_index(
        "ix_admin_invitations_delivery", "admin_invitations", ["status", "next_delivery_at"]
    )


def downgrade():
    op.drop_index("ix_admin_invitations_delivery", table_name="admin_invitations")
    op.drop_index("uq_admin_invitations_one_pending", table_name="admin_invitations")
    op.drop_table("admin_invitations")
    op.drop_index("ix_users_telegram_username_lower", table_name="users")
    with op.batch_alter_table("users") as batch:
        batch.drop_column("telegram_profile_seen_at")
        batch.drop_column("telegram_display_name")
        batch.drop_column("telegram_username")
