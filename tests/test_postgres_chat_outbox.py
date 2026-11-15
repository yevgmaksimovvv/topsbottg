"""PostgreSQL concurrency, crash recovery and ownership checks for the chat outbox."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from topsbottg.api import create_app
from topsbottg.chat_outbox import claim_chat_outbox, settle_chat_outbox
from topsbottg.models import Base, ChatMessage, ChatMessageOutbox, Conversation, User
from topsbottg.worker import deliver_chat_outbox


@pytest.fixture
async def postgres_outbox(settings):
    url = os.getenv("POSTGRES_TEST_URL")
    if not url:
        pytest.skip("POSTGRES_TEST_URL required")

    schema = f"ci_chat_outbox_{uuid4().hex}"
    admin_engine = create_async_engine(url)
    engine = None
    try:
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
        app = create_app(settings.model_copy(update={"database_url": url}), factory)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            async with factory() as session:
                user = User(
                    telegram_id=84001, full_name="Получатель", registration_completed_at=datetime.now(UTC)
                )
                session.add(user)
                await session.flush()
                conversation = Conversation(user_id=user.id)
                session.add(conversation)
                await session.commit()
                conversation_id = conversation.id
            yield client, factory, conversation_id
    finally:
        if engine is not None:
            await engine.dispose()
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await admin_engine.dispose()


@pytest.mark.postgres
async def test_pg_concurrent_post_one_message_and_job(postgres_outbox, admin_init_data):
    client, factory, conversation_id = postgres_outbox
    url = f"/api/admin/conversations/{conversation_id}/messages"
    headers = {"X-Telegram-Init-Data": admin_init_data, "Idempotency-Key": "same-key"}
    posts = await asyncio.wait_for(asyncio.gather(*[
        client.post(url, headers=headers, json={"text": "Одно сообщение"})
        for _ in range(8)
    ]), timeout=20)
    assert [post.status_code for post in posts] == [202] * 8
    assert len({post.json()["message"]["id"] for post in posts}) == 1

    async with factory() as session:
        assert await session.scalar(select(func.count(ChatMessage.id))) == 1
        assert await session.scalar(select(func.count(ChatMessageOutbox.id))) == 1
        job = await session.scalar(select(ChatMessageOutbox))
        assert job.status == "queued" and job.attempts == 0

    conflicting = await asyncio.gather(
        client.post(url, headers={**headers, "Idempotency-Key": "new-race"}, json={"text": "A"}),
        client.post(url, headers={**headers, "Idempotency-Key": "new-race"}, json={"text": "B"}),
    )
    assert sorted(p.status_code for p in conflicting) == [202, 409]
    async with factory() as session:
        assert await session.scalar(select(func.count(ChatMessage.id))) == 2
        assert await session.scalar(select(func.count(ChatMessageOutbox.id))) == 2


@pytest.mark.postgres
async def test_pg_skip_locked_claim_and_no_open_transaction_during_telegram_send(
    postgres_outbox, admin_init_data
):
    client, factory, cid = postgres_outbox
    path = f"/api/admin/conversations/{cid}/messages"
    headers = {"X-Telegram-Init-Data": admin_init_data, "Idempotency-Key": "worker-lock"}
    assert (await client.post(path, headers=headers, json={"text": "Задание"})).status_code == 202

    async with factory() as first_session:
        claims = await claim_chat_outbox(first_session)
        assert len(claims) == 1
        assert claims[0].attempts == 1
        async with factory() as second_session:
            # PostgreSQL SKIP LOCKED does not wait on the uncommitted claim.
            second = await asyncio.wait_for(claim_chat_outbox(second_session), timeout=5)
            assert second == []
        await first_session.commit()

    started = asyncio.Event()
    release = asyncio.Event()
    async with factory() as session:
        item = await session.scalar(select(ChatMessageOutbox))
        item.claimed_until = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()

    class PausedBot:
        async def send_message(self, chat_id, text, **kwargs):
            started.set()
            await release.wait()
            return SimpleNamespace(message_id=844)

    task = asyncio.create_task(deliver_chat_outbox(PausedBot(), factory))
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        async with factory() as session:
            row = await session.scalar(
                select(ChatMessageOutbox).with_for_update(nowait=True)
            )
            assert row.status == "sending" and row.attempts == 2
            await session.rollback()
    finally:
        release.set()
    await asyncio.wait_for(task, timeout=5)
    async with factory() as session:
        item = await session.scalar(select(ChatMessageOutbox))
        assert item.status == "sent" and item.telegram_message_id == 844


@pytest.mark.postgres
async def test_pg_crash_after_send_before_commit_may_duplicate_telegram(
    postgres_outbox, admin_init_data
):
    client, factory, cid = postgres_outbox
    path = f"/api/admin/conversations/{cid}/messages"
    headers = {"X-Telegram-Init-Data": admin_init_data, "Idempotency-Key": "crash-after-send"}
    first = await client.post(path, headers=headers, json={"text": "Отправка при сбое"})
    assert first.status_code == 202
    message_id = first.json()["message"]["id"]

    async with factory() as session:
        [claim] = await claim_chat_outbox(session)
        await session.commit()

    telegram_deliveries = [501]  # Telegram accepted #501; worker crashed before committing "sent".
    async with factory() as session:
        job = await session.get(ChatMessageOutbox, claim.id)
        job.claimed_until = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()

    async with factory() as session:
        [reclaimed] = await claim_chat_outbox(session)
        assert reclaimed.claim_token != claim.claim_token
        assert reclaimed.attempts == 2
        await session.commit()

    async with factory() as session:
        assert not await settle_chat_outbox(session, claim, telegram_message_id=501)
        await session.commit()

    telegram_deliveries.append(502)
    async with factory() as session:
        assert await settle_chat_outbox(session, reclaimed, telegram_message_id=502)
        await session.commit()

    async with factory() as session:
        job = await session.get(ChatMessageOutbox, claim.id)
        message = await session.get(ChatMessage, message_id)
        assert job.status == "sent" and job.attempts == 2
        assert job.telegram_message_id == 502 == message.telegram_message_id
        assert await session.scalar(select(func.count(ChatMessage.id))) == 1
        assert await session.scalar(select(func.count(ChatMessageOutbox.id))) == 1
    assert telegram_deliveries == [501, 502]


@pytest.mark.postgres
async def test_pg_post_requires_active_role_after_revocation(
    postgres_outbox, dynamic_admin_init_data
):
    from topsbottg.models import AdminGrant

    client, factory, cid = postgres_outbox
    url = f"/api/admin/conversations/{cid}/messages"
    headers = {"X-Telegram-Init-Data": dynamic_admin_init_data, "Idempotency-Key": "revocable"}
    async with factory() as session:
        grant = AdminGrant(telegram_id=555, granted_at=datetime.now(UTC), granted_by_telegram_id=123)
        session.add(grant)
        await session.commit()
        grant_id = grant.id

    assert (await client.post(url, headers=headers, json={"text": "До отзыва"})).status_code == 202
    async with factory() as session:
        grant = await session.get(AdminGrant, grant_id)
        grant.revoked_at = datetime.now(UTC)
        await session.commit()

    result = await client.post(url, headers=headers, json={"text": "До отзыва"})
    assert result.status_code == 403
    async with factory() as session:
        assert await session.scalar(select(func.count(ChatMessageOutbox.id))) == 1
