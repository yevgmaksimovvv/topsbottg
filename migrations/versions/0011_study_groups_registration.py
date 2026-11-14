"""Add optional study groups and a persisted registration completion marker.

Revision ID: 0011_study_groups_registration
Revises: 0010_discard_ready_notifications
"""

import sqlalchemy as sa
from alembic import op

revision = "0011_study_groups_registration"
down_revision = "0010_discard_ready_notifications"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "study_groups",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("display_code", sa.String(length=32), nullable=False),
        sa.Column("normalized_code", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("normalized_code", name="uq_study_groups_normalized_code"),
    )
    op.add_column("users", sa.Column("study_group_id", sa.Integer(), nullable=True))
    op.add_column("users", sa.Column("registration_completed_at", sa.DateTime(timezone=True), nullable=True))
    op.create_foreign_key(
        "fk_users_study_group_id", "users", "study_groups", ["study_group_id"], ["id"], ondelete="SET NULL"
    )
    op.create_index("ix_users_study_group_id", "users", ["study_group_id"])

    # Preserve all existing named registrations. The timestamp means "recognized by this
    # migration", not a fabricated historical registration date. Unnamed drafts stay incomplete.
    op.execute(
        """
        UPDATE users
        SET registration_completed_at = CURRENT_TIMESTAMP
        WHERE NULLIF(BTRIM(full_name), '') IS NOT NULL
          AND BTRIM(full_name) <> 'Без ФИО'
        """
    )


def downgrade():
    op.drop_index("ix_users_study_group_id", table_name="users")
    op.drop_constraint("fk_users_study_group_id", "users", type_="foreignkey")
    op.drop_column("users", "registration_completed_at")
    op.drop_column("users", "study_group_id")
    op.drop_table("study_groups")
