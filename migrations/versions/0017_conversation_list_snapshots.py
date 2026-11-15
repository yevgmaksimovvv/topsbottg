"""Materialized, expiring keyset snapshots for conversation list pagination."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0017_conversation_list_snapshots"
down_revision = "0016_conversation_storage"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "conversation_list_snapshots",
        sa.Column("id", sa.String(length=48), primary_key=True),
        sa.Column("admin_telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("search_query", sa.Text(), nullable=False),
        sa.Column("study_group_id", sa.Integer(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_conversation_list_snapshots_expires_at",
        "conversation_list_snapshots", ["expires_at"],
    )
    op.create_table(
        "conversation_list_snapshot_items",
        sa.Column(
            "snapshot_id", sa.String(length=48),
            sa.ForeignKey("conversation_list_snapshots.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("position", sa.Integer(), primary_key=True),
        sa.Column("conversation_id", sa.Integer(), nullable=False),
        sa.Column("last_message_id", sa.Integer(), nullable=True),
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("conversation_list_snapshot_items")
    op.drop_index("ix_conversation_list_snapshots_expires_at", table_name="conversation_list_snapshots")
    op.drop_table("conversation_list_snapshots")
