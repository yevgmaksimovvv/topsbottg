from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004_drop_legacy_payment_fields"
down_revision = "0003_raw_payment_details"
branch_labels = None
depends_on = None


LEGACY_COLUMNS = [
    "recipient_type",
    "recipient_full_name",
    "phone",
    "bank_name",
    "comment",
]


def upgrade() -> None:
    with op.batch_alter_table("payment_profiles") as batch:
        for column_name in LEGACY_COLUMNS:
            batch.drop_column(column_name)


def downgrade() -> None:
    with op.batch_alter_table("payment_profiles") as batch:
        batch.add_column(sa.Column("recipient_type", sa.String(length=16), nullable=True))
        batch.add_column(sa.Column("recipient_full_name", sa.Text(), nullable=True))
        batch.add_column(sa.Column("phone", sa.Text(), nullable=True))
        batch.add_column(sa.Column("bank_name", sa.Text(), nullable=True))
        batch.add_column(sa.Column("comment", sa.Text(), nullable=True))
