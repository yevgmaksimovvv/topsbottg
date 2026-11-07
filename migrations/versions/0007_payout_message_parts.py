import sqlalchemy as sa
from alembic import op

revision = "0007_payout_message_parts"
down_revision = "0006_remove_user_is_active"
branch_labels = None
depends_on = None
def upgrade():
    op.add_column("payouts", sa.Column("message_before_period", sa.Text(), nullable=True))
    op.add_column("payouts", sa.Column("message_after_period", sa.Text(), nullable=True))
def downgrade():
    op.drop_column("payouts", "message_after_period")
    op.drop_column("payouts", "message_before_period")
