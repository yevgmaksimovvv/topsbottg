from __future__ import annotations

import asyncio
import os
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from topsbottg.models import Base, PayoutRecipient, PayoutStatus, RecipientStatus, User
from topsbottg.services import (
    SnapshotVersionConflict,
    add_recipients,
    create_payout,
    get_or_create_user,
    mark_paid,
    save_profile_and_snapshot,
    upsert_payment_profile,
)


@pytest.mark.asyncio
async def test_profile_update_and_mark_paid_serialize_with_postgres_row_locks():
    database_url = os.getenv("POSTGRES_TEST_URL")
    if not database_url:
        pytest.skip("POSTGRES_TEST_URL is not configured")

    schema = f"snapshot_lock_test_{uuid4().hex}"
    admin_engine = create_async_engine(database_url)
    engine = None
    try:
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_async_engine(
            database_url,
            connect_args={"server_settings": {"search_path": schema}},
        )
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

        async with sessions() as session:
            user = await get_or_create_user(session, 70001, "Lock test user")
            await upsert_payment_profile(session, user, "old details")
            payout = await create_payout(
                session,
                actor_telegram_id=1,
                payload={
                    "period_start_day": 1,
                    "period_start_month": 1,
                    "period_end_day": 7,
                    "period_end_month": 1,
                },
            )
            [recipient] = await add_recipients(session, payout, [user.id])
            payout.status = PayoutStatus.sent.value
            recipient.status = RecipientStatus.payment_received.value
            recipient.payment_profile_snapshot = {"raw_payment_details": "old details"}
            recipient.payment_snapshot_version = 1
            await session.commit()

        profile_saved = asyncio.Event()
        allow_profile_commit = asyncio.Event()
        payment_started = asyncio.Event()

        async def update_profile() -> None:
            async with sessions() as session:
                user = await session.scalar(select(User).where(User.id == 1))
                assert user is not None
                await save_profile_and_snapshot(
                    session,
                    user=user,
                    payload="new details",
                    active_recipient=None,
                )
                profile_saved.set()
                await allow_profile_commit.wait()
                await session.commit()

        async def mark_payment() -> str:
            await profile_saved.wait()
            async with sessions() as session:
                recipient = await session.get(PayoutRecipient, 1)
                assert recipient is not None
                payment_started.set()
                try:
                    await mark_paid(
                        session,
                        recipient,
                        actor_telegram_id=2,
                        expected_snapshot_version=1,
                    )
                except SnapshotVersionConflict:
                    await session.rollback()
                    return "conflict"
                await session.commit()
                return "paid"

        profile_task = asyncio.create_task(update_profile())
        await profile_saved.wait()
        payment_task = asyncio.create_task(mark_payment())
        await payment_started.wait()
        await asyncio.sleep(0.05)
        allow_profile_commit.set()
        await profile_task
        assert await payment_task == "conflict"

        async with sessions() as session:
            updated = await session.get(PayoutRecipient, 1)
            assert updated is not None
            assert updated.status == RecipientStatus.payment_received.value
            assert updated.payment_snapshot_version == 2
            assert updated.payment_profile_snapshot == {"raw_payment_details": "new details"}
    finally:
        if engine is not None:
            await engine.dispose()
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await admin_engine.dispose()
