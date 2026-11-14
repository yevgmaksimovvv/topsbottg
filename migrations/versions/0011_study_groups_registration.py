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
    # SQLite needs the table-rebuild path for a new FK; PostgreSQL uses normal ALTER TABLE.
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("study_group_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("registration_completed_at", sa.DateTime(timezone=True), nullable=True))
        batch.create_foreign_key(
            "fk_users_study_group_id", "study_groups", ["study_group_id"], ["id"], ondelete="SET NULL"
        )
        batch.create_index("ix_users_study_group_id", ["study_group_id"])

    # Preserve all existing named registrations. The timestamp means "recognized by this
    # migration", not a fabricated historical registration date. Unnamed drafts stay incomplete.
    op.execute(
        """
        UPDATE users
        SET registration_completed_at = CURRENT_TIMESTAMP
        WHERE NULLIF(TRIM(full_name), '') IS NOT NULL
          AND TRIM(full_name) <> 'Без ФИО'
        """
    )


def downgrade():
    with op.batch_alter_table("users") as batch:
        batch.drop_index("ix_users_study_group_id")
        batch.drop_constraint("fk_users_study_group_id", type_="foreignkey")
        batch.drop_column("registration_completed_at")
        batch.drop_column("study_group_id")
    op.drop_table("study_groups")
