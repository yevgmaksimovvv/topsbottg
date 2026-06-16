from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002_paid_fields"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("payout_recipients", sa.Column("paid_by_admin_id", sa.BigInteger(), nullable=True))
    op.add_column("payout_recipients", sa.Column("paid_note", sa.Text(), nullable=True))

    op.execute("UPDATE payout_recipients SET status = 'payment_required' WHERE status = 'waiting_payment_details'")
    op.execute("UPDATE payout_recipients SET status = 'payment_received' WHERE status = 'payment_details_received'")


def downgrade() -> None:
    op.drop_column("payout_recipients", "paid_note")
    op.drop_column("payout_recipients", "paid_by_admin_id")
    op.execute("UPDATE payout_recipients SET status = 'waiting_payment_details' WHERE status = 'payment_required'")
    op.execute("UPDATE payout_recipients SET status = 'payment_details_received' WHERE status = 'payment_received'")
