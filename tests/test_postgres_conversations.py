"""PostgreSQL constraints and competing conversation writers."""

from __future__ import annotations

import asyncio
import os
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from topsbottg.conversations import (
    advance_read_marker,
    get_or_create_conversation,
    get_unread_count,
    list_conversation_messages,
    record_incoming_message,
)
from topsbottg.models import Base, ChatMessage, Conversation, ConversationRead, User


@pytest.fixture
async def postgres_conversations():
    url = os.getenv("POSTGRES_TEST_URL")
    if not url:
        pytest.skip("POSTGRES_TEST_URL required")

    schema = f"ci_conversations_{uuid4().hex}"
    admin_engine = create_async_engine(url)
    engine = None
    try:
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        yield async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    finally:
        if engine is not None:
            await engine.dispose()
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await admin_engine.dispose()


@pytest.mark.postgres
async def test_postgres_concurrent_conversation_creation_and_duplicate_update(postgres_conversations):
    factory = postgres_conversations
    async with factory() as session:
        user = User(telegram_id=20201, full_name="Первый")
        session.add(user)
        await session.commit()
        user_id = user.id

    async def open_conversation():
        async with factory() as session:
            conv = await get_or_create_conversation(session, user_id=user_id)
            await session.commit()
            return conv.id

    ids = await asyncio.gather(*(open_conversation() for _ in range(5)))
    assert len(set(ids)) == 1

    async def receive(num):
        async with factory() as session:
            message, inserted = await record_incoming_message(
                session, conversation_id=ids[0], telegram_chat_id=20201,
                telegram_message_id=num, text=f"Сообщение {num}",
            )
            await session.commit()
            return message.id, inserted

    duplicate_ids = await asyncio.gather(*(receive(100) for _ in range(5)))
    assert len({row[0] for row in duplicate_ids}) == 1
    assert sum(row[1] for row in duplicate_ids) == 1
    five = await asyncio.gather(*(receive(num) for num in range(101, 105)))
    assert len({row[0] for row in five}) == 4

    async with factory() as session:
        assert await session.scalar(select(func.count(Conversation.id))) == 1
        assert await session.scalar(select(func.count(ChatMessage.id))) == 5
        messages, cursor = await list_conversation_messages(session, conversation_id=ids[0], limit=10)
        assert cursor is None
        assert [message.id for message in messages] == sorted(message.id for message in messages)
        assert {message.telegram_message_id for message in messages} == {100, 101, 102, 103, 104}
        assert await get_unread_count(session, conversation_id=ids[0], admin_telegram_id=123) == 5


@pytest.mark.postgres
async def test_postgres_read_markers_never_move_backward_in_racing_transactions(postgres_conversations):
    factory = postgres_conversations
    async with factory() as session:
        user = User(telegram_id=20202, full_name="Второй")
        session.add(user)
        await session.flush()
        conv = await get_or_create_conversation(session, user_id=user.id)
        ids = []
        for num in range(1, 6):
            msg, _ = await record_incoming_message(
                session, conversation_id=conv.id, telegram_chat_id=20202,
                telegram_message_id=num, text=str(num),
            )
            ids.append(msg.id)
        await session.commit()
        conv_id = conv.id

    async def read(message_id):
        async with factory() as session:
            value = await advance_read_marker(
                session, conversation_id=conv_id, admin_telegram_id=123, message_id=message_id
            )
            await session.commit()
            return value

    await asyncio.gather(read(ids[4]), read(ids[1]), read(ids[3]))
    async with factory() as session:
        assert await get_unread_count(session, conversation_id=conv_id, admin_telegram_id=123) == 0
        assert await advance_read_marker(
            session, conversation_id=conv_id, admin_telegram_id=123, message_id=ids[0]
        ) == ids[4]
        assert await session.scalar(select(func.count(ConversationRead.id))) == 1


@pytest.mark.postgres
async def test_postgres_foreign_keys_unique_and_direction_constraint(postgres_conversations):
    factory = postgres_conversations
    async with factory() as session:
        session.add(Conversation(user_id=9999999))
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()

        user = User(telegram_id=20203, full_name="Третий")
        session.add(user)
        await session.flush()
        conv = await get_or_create_conversation(session, user_id=user.id)
        msg, _ = await record_incoming_message(
            session, conversation_id=conv.id, telegram_chat_id=20203,
            telegram_message_id=7, text="Текст",
        )
        await session.commit()
        conv_id, message_id = conv.id, msg.id

    async with factory() as session:
        session.add(ChatMessage(
            conversation_id=conv_id, direction="incoming",
            telegram_chat_id=20203, telegram_message_id=7, text="Повтор",
        ))
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()

        session.add(ChatMessage(conversation_id=conv_id, direction="wrong", text="Ошибка"))
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()

        session.add(ChatMessage(conversation_id=123456789, direction="incoming", text="Ошибка"))
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()

        session.add_all([
            ConversationRead(conversation_id=conv_id, admin_telegram_id=123, last_read_message_id=message_id),
            ConversationRead(conversation_id=conv_id, admin_telegram_id=123, last_read_message_id=message_id),
        ])
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()

        session.add_all([
            ChatMessage(conversation_id=conv_id, direction="outgoing", source="admin", text="Первый"),
            ChatMessage(conversation_id=conv_id, direction="outgoing", source="admin", text="Второй"),
        ])
        await session.commit()
        assert await session.scalar(select(func.count(ChatMessage.id))) == 3
        assert await get_unread_count(session, conversation_id=conv_id, admin_telegram_id=123) == 1
