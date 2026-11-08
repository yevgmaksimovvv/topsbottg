from __future__ import annotations

import asyncio
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from topsbottg.models import Payout, PayoutRecipient, RecipientStatus
from topsbottg.worker import process_recipient


class RecordingBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int | str, text: str, reply_markup=None) -> None:  # noqa: ANN001
        self.sent.append((int(chat_id), text))


def test_migration_0007_preserves_and_sends_existing_payout(tmp_path: Path) -> None:
    database_url = f"sqlite+aiosqlite:///{(tmp_path / 'pre-0007.db').as_posix()}"
    repository_root = Path(__file__).resolve().parents[1]
    migration_env = {**os.environ, "DATABASE_URL": database_url}

    def upgrade(revision: str) -> None:
        subprocess.run(
            [
                sys.executable,
                "-c",
                "from alembic.config import Config; from alembic import command; "
                "import sys; command.upgrade(Config('alembic.ini'), sys.argv[1])",
                revision,
            ],
            check=True,
            cwd=repository_root,
            env=migration_env,
            capture_output=True,
            text=True,
        )

    upgrade("0006_remove_user_is_active")
    with sqlite3.connect(tmp_path / "pre-0007.db") as connection:
        connection.execute(
            """
            INSERT INTO users (id, telegram_id, full_name, created_at, updated_at)
            VALUES (1, 12345, 'Existing user', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """
        )
        connection.execute(
            """
            INSERT INTO payouts (
                id, message_template, status, created_by_telegram_id,
                period_start_day, period_start_month, period_end_day, period_end_month,
                created_at, updated_at
            ) VALUES (1, 'Payout period {period_label}', 'sending', 123,
                      1, 2, 28, 2, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """
        )
        connection.execute(
            """
            INSERT INTO payout_recipients (
                id, payout_id, user_id, status, payment_profile_snapshot, created_at, updated_at
            ) VALUES (
                1, 1, 1, 'sending', '{"raw_payment_details":"Legacy details"}',
                CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
            )
            """
        )

    upgrade("head")
    bot = RecordingBot()
    engine = create_async_engine(database_url, future=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    async def send_existing_payout() -> None:
        async with session_factory() as session:
            payout = await session.get(Payout, 1)
            assert payout is not None
            assert payout.message_template == "Payout period {period_label}"
            assert payout.message_before_period is None
            assert payout.message_after_period is None
            recipient = await session.get(PayoutRecipient, 1)
            assert recipient is not None
            assert recipient.payment_snapshot_version == 1
            assert recipient.payment_snapshot_updated_at is not None
            assert recipient.payment_profile_snapshot == {"raw_payment_details": "Legacy details"}

        await process_recipient(bot, session_factory, None, 1)

        async with session_factory() as session:
            recipient = await session.get(PayoutRecipient, 1)
            assert recipient is not None
            assert recipient.status == RecipientStatus.payment_required.value

        await engine.dispose()

    asyncio.run(send_existing_payout())

    assert bot.sent == [(12345, "Payout period 01.02 \u2013 28.02")]
