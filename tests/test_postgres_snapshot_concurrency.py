from __future__ import annotations

import asyncio
import os
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from topsbottg.models import Base, PaymentProfile, Payout, PayoutRecipient, PayoutStatus, RecipientStatus, User
from topsbottg.services import (
    PayoutClosedConflict,
    SnapshotVersionConflict,
    add_recipients,
    complete_payout,
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
    payment_engine = None
    try:
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_async_engine(
            database_url,
            connect_args={"server_settings": {"search_path": schema}},
        )
        payment_engine = create_async_engine(
            database_url,
            connect_args={
                "server_settings": {
                    "search_path": schema,
                    "application_name": "snapshot-payment-waiter",
                }
            },
        )
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
        payment_sessions = async_sessionmaker(payment_engine, expire_on_commit=False, class_=AsyncSession)

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
            user_id, recipient_id = user.id, recipient.id
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
                user = await session.scalar(select(User).where(User.id == user_id))
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
            async with payment_sessions() as session:
                recipient = await session.get(PayoutRecipient, recipient_id)
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
        deadline = asyncio.get_running_loop().time() + 5
        blocked = False
        while asyncio.get_running_loop().time() < deadline:
            async with admin_engine.connect() as connection:
                blocked = bool(
                    await connection.scalar(
                        text(
                            "SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                            "WHERE application_name = 'snapshot-payment-waiter' "
                            "AND wait_event_type = 'Lock')"
                        )
                    )
                )
            if blocked:
                break
            await asyncio.sleep(0.02)
        assert blocked, "payment operation did not wait on the profile transaction's user lock"
        allow_profile_commit.set()
        await profile_task
        assert await payment_task == "conflict"

        async with sessions() as session:
            updated = await session.get(PayoutRecipient, recipient_id)
            assert updated is not None
            assert updated.status == RecipientStatus.payment_received.value
            assert updated.payment_snapshot_version == 2
            assert updated.payment_profile_snapshot == {"raw_payment_details": "new details"}
    finally:
        if payment_engine is not None:
            await payment_engine.dispose()
        if engine is not None:
            await engine.dispose()
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await admin_engine.dispose()


@pytest.mark.asyncio
async def test_mark_paid_wins_before_profile_update_without_rewriting_paid_snapshot():
    database_url = os.getenv("POSTGRES_TEST_URL")
    if not database_url:
        pytest.skip("POSTGRES_TEST_URL is not configured")

    schema = f"snapshot_paid_first_test_{uuid4().hex}"
    admin_engine = create_async_engine(database_url)
    engine = None
    updater_engine = None
    try:
        async with admin_engine.begin() as connection:
            version = await connection.scalar(text("SELECT current_setting('server_version_num')::int"))
            assert version is not None and version // 10000 == 16, "concurrency proof requires PostgreSQL 16"
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_async_engine(
            database_url,
            connect_args={"server_settings": {"search_path": schema}},
        )
        updater_engine = create_async_engine(
            database_url,
            connect_args={
                "server_settings": {
                    "search_path": schema,
                    "application_name": "snapshot-paid-first-updater",
                }
            },
        )
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
        updater_sessions = async_sessionmaker(updater_engine, expire_on_commit=False, class_=AsyncSession)

        async with sessions() as session:
            user = await get_or_create_user(session, 70002, "Paid first user")
            await upsert_payment_profile(session, user, "reviewed details")
            payout = await create_payout(
                session,
                actor_telegram_id=1,
                payload={"period_start_day": 1, "period_start_month": 1, "period_end_day": 7, "period_end_month": 1},
            )
            [recipient] = await add_recipients(session, payout, [user.id])
            payout.status = PayoutStatus.sent.value
            recipient.status = RecipientStatus.payment_received.value
            recipient.payment_profile_snapshot = {"raw_payment_details": "reviewed details"}
            recipient.payment_snapshot_version = 1
            await session.commit()
            user_id, recipient_id = user.id, recipient.id

        payment_locked = asyncio.Event()
        profile_attempting = asyncio.Event()
        release_profile = asyncio.Event()

        async def mark_payment() -> None:
            async with sessions() as session:
                recipient = await session.get(PayoutRecipient, recipient_id)
                assert recipient is not None
                await mark_paid(session, recipient, actor_telegram_id=2, expected_snapshot_version=1)
                payment_locked.set()
                await release_profile.wait()
                await session.commit()

        async def update_profile() -> None:
            await payment_locked.wait()
            async with updater_sessions() as session:
                user = await session.get(User, user_id)
                assert user is not None
                profile_attempting.set()
                await save_profile_and_snapshot(
                    session,
                    user=user,
                    payload="new profile details",
                    active_recipient=None,
                )
                await session.commit()

        payment_task = asyncio.create_task(mark_payment())
        await asyncio.wait_for(payment_locked.wait(), timeout=5)
        profile_task = asyncio.create_task(update_profile())
        await asyncio.wait_for(profile_attempting.wait(), timeout=5)
        deadline = asyncio.get_running_loop().time() + 5
        blocked = False
        while asyncio.get_running_loop().time() < deadline:
            async with admin_engine.connect() as connection:
                blocked = bool(
                    await connection.scalar(
                        text(
                            "SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                            "WHERE application_name = 'snapshot-paid-first-updater' "
                            "AND wait_event_type = 'Lock')"
                        )
                    )
                )
            if blocked:
                break
            await asyncio.sleep(0.02)
        assert blocked, "profile update did not wait on the payment transaction's user lock"
        release_profile.set()
        await asyncio.wait_for(payment_task, timeout=5)
        await asyncio.wait_for(profile_task, timeout=5)

        async with sessions() as session:
            updated_profile = await session.scalar(select(PaymentProfile).where(PaymentProfile.user_id == user_id))
            paid = await session.get(PayoutRecipient, recipient_id)
            assert updated_profile is not None
            assert updated_profile.raw_payment_details == "new profile details"
            assert paid is not None
            assert paid.status == RecipientStatus.paid.value
            assert paid.payment_snapshot_version == 1
            assert paid.payment_profile_snapshot == {"raw_payment_details": "reviewed details"}
    finally:
        if updater_engine is not None:
            await updater_engine.dispose()
        if engine is not None:
            await engine.dispose()
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await admin_engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("close_first", [True, False], ids=["close-wins", "payment-wins"])
async def test_close_and_mark_paid_serialize_with_payout_lock(close_first):
    """Prove both orders against PostgreSQL row locks, not SQLite scheduling."""
    database_url = os.getenv("POSTGRES_TEST_URL")
    if not database_url:
        pytest.skip("POSTGRES_TEST_URL is not configured")

    schema = f"payout_close_lock_{uuid4().hex}"
    app_name = f"payout-close-contender-{uuid4().hex[:8]}"
    admin_engine = create_async_engine(database_url)
    first_engine = None
    second_engine = None
    tasks = []
    release_first = asyncio.Event()
    try:
        async with admin_engine.begin() as connection:
            version = await connection.scalar(text("SELECT current_setting('server_version_num')::int"))
            assert version is not None and version // 10000 == 16
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))

        first_engine = create_async_engine(
            database_url,
            connect_args={"server_settings": {"search_path": schema}},
        )
        second_engine = create_async_engine(
            database_url,
            connect_args={"server_settings": {"search_path": schema, "application_name": app_name}},
        )
        async with first_engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        first_sessions = async_sessionmaker(first_engine, expire_on_commit=False, class_=AsyncSession)
        second_sessions = async_sessionmaker(second_engine, expire_on_commit=False, class_=AsyncSession)

        async with first_sessions() as session:
            user = await get_or_create_user(session, 77001, "Closing race user")
            payout = await create_payout(
                session, actor_telegram_id=1,
                payload={"period_start_day": 1, "period_start_month": 1,
                         "period_end_day": 7, "period_end_month": 1},
            )
            [recipient] = await add_recipients(session, payout, [user.id])
            payout.status = PayoutStatus.sent.value
            recipient.status = RecipientStatus.payment_received.value
            recipient.payment_snapshot_version = 1
            recipient.payment_profile_snapshot = {"raw_payment_details": "reviewed account"}
            payout_id, recipient_id = payout.id, recipient.id
            await session.commit()

        first_locked = asyncio.Event()

        async def first_transaction():
            async with first_sessions() as session:
                if close_first:
                    payout = await session.get(Payout, payout_id)
                    assert payout is not None
                    await complete_payout(session, payout, actor_telegram_id=2)
                else:
                    recipient = await session.get(PayoutRecipient, recipient_id)
                    assert recipient is not None
                    await mark_paid(session, recipient, actor_telegram_id=2, expected_snapshot_version=1)
                first_locked.set()
                await release_first.wait()
                await session.commit()

        async def competing_transaction():
            await first_locked.wait()
            async with second_sessions() as session:
                if close_first:
                    recipient = await session.get(PayoutRecipient, recipient_id)
                    assert recipient is not None
                    try:
                        await mark_paid(session, recipient, actor_telegram_id=3, expected_snapshot_version=1)
                    except PayoutClosedConflict:
                        await session.rollback()
                        return "rejected"
                    await session.commit()
                    return "unexpectedly_paid"
                payout = await session.get(Payout, payout_id)
                assert payout is not None
                await complete_payout(session, payout, actor_telegram_id=3)
                await session.commit()
                return "closed"

        tasks.append(asyncio.create_task(first_transaction()))
        await asyncio.wait_for(first_locked.wait(), timeout=8)
        tasks.append(asyncio.create_task(competing_transaction()))
        deadline = asyncio.get_running_loop().time() + 8
        waiting = False
        while asyncio.get_running_loop().time() < deadline:
            async with admin_engine.connect() as connection:
                waiting = bool(await connection.scalar(text(
                    "SELECT EXISTS(SELECT 1 FROM pg_stat_activity "
                    "WHERE application_name = :app AND wait_event_type = 'Lock')"
                ), {"app": app_name}))
            if waiting:
                break
            await asyncio.sleep(0.03)
        assert waiting, "concurrent operation did not wait for the payout row lock"
        release_first.set()
        await asyncio.wait_for(tasks[0], timeout=8)
        outcome = await asyncio.wait_for(tasks[1], timeout=8)
        assert outcome == ("rejected" if close_first else "closed")

        async with first_sessions() as session:
            payout = await session.get(Payout, payout_id)
            recipient = await session.get(PayoutRecipient, recipient_id)
            assert payout is not None and payout.status == PayoutStatus.completed.value
            assert recipient is not None
            assert recipient.status == (
                RecipientStatus.payment_received.value if close_first else RecipientStatus.paid.value
            )
            assert recipient.payment_profile_snapshot == {"raw_payment_details": "reviewed account"}
            assert recipient.payment_snapshot_version == 1
    finally:
        release_first.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if second_engine is not None:
            await second_engine.dispose()
        if first_engine is not None:
            await first_engine.dispose()
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await admin_engine.dispose()
