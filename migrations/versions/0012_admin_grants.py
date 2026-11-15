"""Create revocable administrator grants, independent from user registrations.

Revision ID: 0012_admin_grants
Revises: 0011_study_groups_registration
"""

import sqlalchemy as sa
from alembic import op

revision = "0012_admin_grants"
down_revision = "0011_study_groups_registration"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "admin_grants",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("granted_by_telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("granted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("telegram_id", name="uq_admin_grants_telegram_id"),
    )


def downgrade():
    op.drop_table("admin_grants")
