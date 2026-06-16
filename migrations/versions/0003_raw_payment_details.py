from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_raw_payment_details"
down_revision = "0002_paid_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("payment_profiles", sa.Column("raw_payment_details", sa.Text(), nullable=True))
    with op.batch_alter_table("payment_profiles") as batch:
        batch.alter_column("recipient_full_name", existing_type=sa.Text(), nullable=True)
        batch.alter_column("phone", existing_type=sa.Text(), nullable=True)
        batch.alter_column("bank_name", existing_type=sa.Text(), nullable=True)


def downgrade() -> None:
    with op.batch_alter_table("payment_profiles") as batch:
        batch.alter_column("bank_name", existing_type=sa.Text(), nullable=False)
        batch.alter_column("phone", existing_type=sa.Text(), nullable=False)
        batch.alter_column("recipient_full_name", existing_type=sa.Text(), nullable=False)
    op.drop_column("payment_profiles", "raw_payment_details")
