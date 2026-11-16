"""Trust chat inbox only after /start on the persisted FSM."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0023_chat_inbox_start_trust"
down_revision = "0022_message_campaigns"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "bot_fsm_states",
        sa.Column("inbox_trusted_after_start", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("bot_fsm_states", "inbox_trusted_after_start")
