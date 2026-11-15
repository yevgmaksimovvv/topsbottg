"""PostgreSQL API tests for keyset stability and concurrent requests."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from topsbottg.api import create_app
from topsbottg.conversations import record_incoming_message
from topsbottg.models import Base, ChatMessage, Conversation, ConversationRead, User


@pytest.fixture
async def postgres_chat_client(settings):
    url = os.getenv("POSTGRES_TEST_URL")
    if not url:
        pytest.skip("POSTGRES_TEST_URL required")

    schema = f"ci_chat_api_{uuid4().hex}"
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
            yield client, factory
    finally:
        if engine is not None:
            await engine.dispose()
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await admin_engine.dispose()


@pytest.mark.postgres
async def test_concurrent_open_returns_one_201_and_no_messages(postgres_chat_client, admin_init_data):
    client, factory = postgres_chat_client
    async with factory() as session:
        user = User(
            telegram_id=71001, full_name="Участник",
            registration_completed_at=datetime.now(UTC),
        )
        session.add(user)
        await session.commit()
        user_id = user.id

    headers = {"X-Telegram-Init-Data": admin_init_data}
    responses = await asyncio.gather(*(
        client.post("/api/admin/conversations", headers=headers, json={"user_id": user_id})
        for _ in range(6)
    ))
    assert sorted(response.status_code for response in responses) == [200] * 5 + [201]
    assert len({response.json()["conversation_id"] for response in responses}) == 1
    async with factory() as session:
        assert await session.scalar(select(func.count(Conversation.id))) == 1
        assert await session.scalar(select(func.count(ChatMessage.id))) == 0


@pytest.mark.postgres
async def test_keyset_list_survives_new_message_in_unseen_conversation(
    postgres_chat_client, admin_init_data
):
    client, factory = postgres_chat_client
    headers = {"X-Telegram-Init-Data": admin_init_data}
    async with factory() as session:
        now = datetime.now(UTC)
        users = [
            User(telegram_id=72000 + index, full_name=f"Абонент {index}",
                 registration_completed_at=now)
            for index in range(3)
        ]
        session.add_all(users)
        await session.commit()
        user_ids = [user.id for user in users]

    conv_ids = []
    for user_id in user_ids:
        response = await client.post("/api/admin/conversations", json={"user_id": user_id}, headers=headers)
        assert response.status_code == 201
        conv_ids.append(response.json()["conversation_id"])
    for index, conv_id in enumerate(conv_ids):
        async with factory() as session:
            _, inserted = await record_incoming_message(
                session, conversation_id=conv_id, telegram_chat_id=72000 + index,
                telegram_message_id=1, text=f"Исходное {index}",
            )
            assert inserted
            await session.commit()

    first = (await client.get("/api/admin/conversations", headers=headers,
                              params={"limit": 1})).json()
    first_id = first["items"][0]["id"]
    assert first_id == conv_ids[-1]
    assert first["next_cursor"] is not None

    async with factory() as session:
        _, inserted = await record_incoming_message(
            session, conversation_id=conv_ids[1], telegram_chat_id=72001,
            telegram_message_id=2, text="Новое после первой страницы",
        )
        assert inserted
        await session.commit()

    second = (await client.get("/api/admin/conversations", headers=headers, params={
        "limit": 1, "cursor": first["next_cursor"],
    })).json()
    assert [item["id"] for item in second["items"]] == [conv_ids[1]]
    assert second["items"][0]["last_message"]["text"] == "Исходное 1"
    third = (await client.get("/api/admin/conversations", headers=headers, params={
        "limit": 1, "cursor": second["next_cursor"],
    })).json()
    assert [item["id"] for item in third["items"]] == [conv_ids[0]]
    assert third["next_cursor"] is None
    assert len({first_id, second["items"][0]["id"], third["items"][0]["id"]}) == 3

    fresh = (await client.get("/api/admin/conversations", headers=headers)).json()
    assert fresh["items"][0]["id"] == conv_ids[1]
    assert fresh["items"][0]["last_message"]["text"] == "Новое после первой страницы"


@pytest.mark.postgres
async def test_pg_personal_read_markers_remain_monotonic_under_races(
    postgres_chat_client, admin_init_data, dynamic_admin_init_data,
):
    client, factory = postgres_chat_client
    owner = {"X-Telegram-Init-Data": admin_init_data}
    admin = {"X-Telegram-Init-Data": dynamic_admin_init_data}
    async with factory() as session:
        from topsbottg.models import AdminGrant

        session.add(AdminGrant(
            telegram_id=555, granted_by_telegram_id=123, granted_at=datetime.now(UTC),
        ))
        user = User(
            telegram_id=73001, full_name="Пользователь без группы",
            registration_completed_at=datetime.now(UTC),
        )
        session.add(user)
        await session.commit()
        user_id = user.id

    conv_id = (await client.post("/api/admin/conversations", headers=owner,
                                 json={"user_id": user_id})).json()["conversation_id"]
    messages = []
    async with factory() as session:
        for index in range(1, 5):
            message, inserted = await record_incoming_message(
                session, conversation_id=conv_id, telegram_chat_id=73001,
                telegram_message_id=index, text=f"Текст {index}",
            )
            assert inserted
            messages.append(message.id)
        await session.commit()

    path = f"/api/admin/conversations/{conv_id}/read"
    results = await asyncio.gather(*(
        client.post(path, headers=owner, json={"last_read_message_id": message_id})
        for message_id in [messages[3], messages[0], messages[2]]
    ))
    assert all(response.status_code == 200 for response in results)
    admin_read = await client.post(path, headers=admin, json={
        "last_read_message_id": messages[0],
    })
    assert admin_read.status_code == 200
    assert admin_read.json()["unread_count"] == 3
    assert (await client.get("/api/admin/conversations/summary", headers=owner)).json() == {"unread_count": 0}
    assert (await client.get("/api/admin/conversations/summary", headers=admin)).json() == {"unread_count": 3}

    async with factory() as session:
        markers = (await session.scalars(
            select(ConversationRead).where(ConversationRead.conversation_id == conv_id)
        )).all()
        assert {(marker.admin_telegram_id, marker.last_read_message_id) for marker in markers} == {
            (123, messages[-1]), (555, messages[0]),
        }
