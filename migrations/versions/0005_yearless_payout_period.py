from __future__ import annotations

from datetime import date

import sqlalchemy as sa
from alembic import op

revision = "0005_yearless_payout_period"
down_revision = "0004_drop_legacy_payment_fields"
branch_labels = None
depends_on = None


NEW_COLUMNS = (
    ("period_start_day", sa.Integer()),
    ("period_start_month", sa.Integer()),
    ("period_end_day", sa.Integer()),
    ("period_end_month", sa.Integer()),
)

OLD_COLUMNS = (
    ("title", sa.Text()),
    ("period_from", sa.Date()),
    ("period_to", sa.Date()),
)


def _fill_payouts_from_old_columns(bind) -> None:
    rows = bind.execute(
        sa.text("SELECT id, period_from, period_to FROM payouts").columns(
            id=sa.Integer(),
            period_from=sa.Date(),
            period_to=sa.Date(),
        )
    ).mappings().all()
    for row in rows:
        period_from = row["period_from"]
        period_to = row["period_to"]
        bind.execute(
            sa.text(
                """
                UPDATE payouts
                SET period_start_day = :period_start_day,
                    period_start_month = :period_start_month,
                    period_end_day = :period_end_day,
                    period_end_month = :period_end_month
                WHERE id = :id
                """
            ),
            {
                "id": row["id"],
                "period_start_day": period_from.day,
                "period_start_month": period_from.month,
                "period_end_day": period_to.day,
                "period_end_month": period_to.month,
            },
        )


def _restore_payouts_to_old_columns(bind) -> None:
    rows = bind.execute(
        sa.text(
            "SELECT id, period_start_day, period_start_month, period_end_day, period_end_month FROM payouts"
        ).columns(
            id=sa.Integer(),
            period_start_day=sa.Integer(),
            period_start_month=sa.Integer(),
            period_end_day=sa.Integer(),
            period_end_month=sa.Integer(),
        )
    ).mappings().all()
    for row in rows:
        period_from = date(2000, row["period_start_month"], row["period_start_day"])
        period_to = date(2000, row["period_end_month"], row["period_end_day"])
        title = (
            f"Выплата {row['period_start_day']:02d}.{row['period_start_month']:02d}"
            f"—{row['period_end_day']:02d}.{row['period_end_month']:02d}"
        )
        bind.execute(
            sa.text(
                """
                UPDATE payouts
                SET title = :title,
                    period_from = :period_from,
                    period_to = :period_to
                WHERE id = :id
                """
            ),
            {
                "id": row["id"],
                "title": title,
                "period_from": period_from,
                "period_to": period_to,
            },
        )


def upgrade() -> None:
    for column_name, column_type in NEW_COLUMNS:
        op.add_column("payouts", sa.Column(column_name, column_type, nullable=True))

    bind = op.get_bind()
    _fill_payouts_from_old_columns(bind)

    with op.batch_alter_table("payouts") as batch:
        batch.alter_column("period_start_day", nullable=False)
        batch.alter_column("period_start_month", nullable=False)
        batch.alter_column("period_end_day", nullable=False)
        batch.alter_column("period_end_month", nullable=False)
        batch.create_check_constraint("ck_payouts_period_start_day", "period_start_day BETWEEN 1 AND 31")
        batch.create_check_constraint("ck_payouts_period_start_month", "period_start_month BETWEEN 1 AND 12")
        batch.create_check_constraint("ck_payouts_period_end_day", "period_end_day BETWEEN 1 AND 31")
        batch.create_check_constraint("ck_payouts_period_end_month", "period_end_month BETWEEN 1 AND 12")
        batch.drop_column("title")
        batch.drop_column("period_from")
        batch.drop_column("period_to")


def downgrade() -> None:
    for column_name, column_type in OLD_COLUMNS:
        op.add_column("payouts", sa.Column(column_name, column_type, nullable=True))

    bind = op.get_bind()
    _restore_payouts_to_old_columns(bind)

    # The downgraded schema uses the technical year 2000 because the new model stores no year at all.
    with op.batch_alter_table("payouts") as batch:
        batch.alter_column("title", nullable=False)
        batch.alter_column("period_from", nullable=False)
        batch.alter_column("period_to", nullable=False)
        batch.drop_constraint("ck_payouts_period_start_day", type_="check")
        batch.drop_constraint("ck_payouts_period_start_month", type_="check")
        batch.drop_constraint("ck_payouts_period_end_day", type_="check")
        batch.drop_constraint("ck_payouts_period_end_month", type_="check")
        batch.drop_column("period_start_day")
        batch.drop_column("period_start_month")
        batch.drop_column("period_end_day")
        batch.drop_column("period_end_month")
