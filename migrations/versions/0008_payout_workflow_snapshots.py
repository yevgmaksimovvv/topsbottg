import sqlalchemy as sa
from alembic import op

revision = "0008_payout_workflow_snapshots"
down_revision = "0007_payout_message_parts"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "payout_recipients",
        sa.Column("payment_snapshot_version", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "payout_recipients",
        sa.Column("payment_snapshot_updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        "UPDATE payout_recipients "
        "SET payment_snapshot_version = 1, payment_snapshot_updated_at = updated_at "
        "WHERE payment_profile_snapshot IS NOT NULL"
    )


def downgrade():
    op.drop_column("payout_recipients", "payment_snapshot_updated_at")
    op.drop_column("payout_recipients", "payment_snapshot_version")
