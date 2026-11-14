"""Upgrade an actual legacy PostgreSQL database without losing payout data.

Unlike a clean-schema Alembic smoke test, this verifies that existing payout
rows, payment snapshots, and user registrations survive every later revision.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine


@pytest.mark.postgres
async def test_postgres_legacy_payout_survives_upgrade_to_head():
    base_url = os.getenv("POSTGRES_TEST_URL")
    if not base_url:
        pytest.skip("POSTGRES_TEST_URL not configured")

    db_name = f"ci_migration_{uuid4().hex[:16]}"
    admin_engine = create_async_engine(base_url, isolation_level="AUTOCOMMIT")
    db_url = make_url(base_url).set(database=db_name).render_as_string(hide_password=False)
    db_engine = None
    try:
        async with admin_engine.connect() as connection:
            await connection.execute(text(f'CREATE DATABASE "{db_name}"'))

        repository = Path(__file__).resolve().parents[1]
        env = {**os.environ, "DATABASE_URL": db_url}

        def upgrade(revision: str) -> None:
            result = subprocess.run(
                [sys.executable, "-m", "alembic", "upgrade", revision],
                env=env,
                cwd=repository,
                capture_output=True,
                text=True,
                timeout=40,
            )
            assert result.returncode == 0, (
                f"Alembic upgrade {revision} failed:\n{result.stdout}\n{result.stderr}"
            )

        upgrade("0006_remove_user_is_active")
        db_engine = create_async_engine(db_url)

        async with db_engine.begin() as connection:
            await connection.execute(text("""
                INSERT INTO users (id, telegram_id, full_name, created_at, updated_at)
                VALUES (1, 987654321, 'Existing user', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """))
            await connection.execute(text("""
                INSERT INTO users (id, telegram_id, full_name, created_at, updated_at)
                VALUES (2, 987654322, 'Без ФИО', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """))
            await connection.execute(text("""
                INSERT INTO payouts (
                    id, message_template, status, created_by_telegram_id,
                    period_start_day, period_start_month,
                    period_end_day, period_end_month, created_at, updated_at
                ) VALUES (
                    1, 'Legacy payout {period_label}', 'sending', 123,
                    1, 2, 28, 2, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
            """))
            await connection.execute(text("""
                INSERT INTO payout_recipients (
                    id, payout_id, user_id, status,
                    payment_profile_snapshot, created_at, updated_at
                ) VALUES (
                    1, 1, 1, 'sending',
                    '{"raw_payment_details":"Legacy details"}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
            """))

        await db_engine.dispose()
        db_engine = None
        upgrade("head")
        db_engine = create_async_engine(db_url)

        async with db_engine.connect() as connection:
            user = (await connection.execute(text(
                "SELECT telegram_id, full_name FROM users WHERE id = 1"
            ))).one()
            assert user.telegram_id == 987654321
            assert user.full_name == "Existing user"

            registered = (await connection.execute(text(
                "SELECT study_group_id, registration_completed_at FROM users WHERE id = 1"
            ))).one()
            assert registered.study_group_id is None
            assert registered.registration_completed_at is not None
            draft = (await connection.execute(text(
                "SELECT study_group_id, registration_completed_at FROM users WHERE id = 2"
            ))).one()
            assert draft.study_group_id is None
            assert draft.registration_completed_at is None

            payout = (await connection.execute(text("""
                SELECT message_template, message_before_period,
                       message_after_period, status
                FROM payouts WHERE id = 1
            """))).one()
            assert payout.message_template == "Legacy payout {period_label}"
            assert payout.message_before_period is None
            assert payout.message_after_period is None
            assert payout.status == "sending"

            recipient = (await connection.execute(text("""
                SELECT status, payment_profile_snapshot,
                       payment_snapshot_version, payment_snapshot_updated_at
                FROM payout_recipients WHERE id = 1
            """))).one()
            assert recipient.status == "sending"
            assert recipient.payment_profile_snapshot == {"raw_payment_details": "Legacy details"}
            assert recipient.payment_snapshot_version == 1
            assert recipient.payment_snapshot_updated_at is not None

            revision = (await connection.execute(text(
                "SELECT version_num FROM alembic_version"
            ))).scalar_one()
            migration_head = ScriptDirectory.from_config(Config(str(repository / "alembic.ini"))).get_current_head()
            assert revision == migration_head
    finally:
        if db_engine is not None:
            await db_engine.dispose()
        try:
            async with admin_engine.connect() as connection:
                await connection.execute(text(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)'))
        finally:
            await admin_engine.dispose()
