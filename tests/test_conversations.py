"""Conversation storage, pagination, idempotency and personal read markers."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from topsbottg.conversations import (
    advance_read_marker,
    get_or_create_conversation,
    get_unread_count,
    list_conversation_messages,
    record_incoming_message,
)
from topsbottg.models import ChatMessage, Conversation, ConversationRead, User


@pytest.mark.asyncio
async def test_conversation_is_permanent_and_messages_use_keyset_order(session_factory):
    async with session_factory() as session:
        user = User(telegram_id=10101, full_name="Тестовый пользователь")
        session.add(user)
        await session.flush()
        conversation = await get_or_create_conversation(session, user_id=user.id)
        first_id = conversation.id
        for telegram_id in range(1, 6):
            message, created = await record_incoming_message(
                session,
                conversation_id=conversation.id,
                telegram_chat_id=10101,
                telegram_message_id=telegram_id,
                text=f"Сообщение {telegram_id}",
                reply_to_telegram_message_id=telegram_id - 1 if telegram_id > 1 else None,
                source="telegram",
            )
            assert created is True
            assert message.telegram_message_id == telegram_id
        await session.commit()

    async with session_factory() as session:
        assert (await get_or_create_conversation(session, user_id=user.id)).id == first_id
        assert (await session.scalar(select(func.count(Conversation.id)))) == 1
        assert (await session.scalar(select(func.count(ChatMessage.id)))) == 5
        conversation = await session.get(Conversation, first_id)
        assert conversation is not None and conversation.last_message_at is not None

        last_page, cursor = await list_conversation_messages(session, conversation_id=first_id, limit=2)
        assert [msg.telegram_message_id for msg in last_page] == [4, 5]
        assert cursor == last_page[0].id

        middle, cursor2 = await list_conversation_messages(
            session, conversation_id=first_id, before_id=cursor, limit=2
        )
        assert [msg.telegram_message_id for msg in middle] == [2, 3]
        earliest, final_cursor = await list_conversation_messages(
            session, conversation_id=first_id, before_id=cursor2, limit=2
        )
        assert [msg.telegram_message_id for msg in earliest] == [1]
        assert final_cursor is None
        assert len({msg.id for msg in last_page + middle + earliest}) == 5
        assert earliest[0].reply_to_telegram_message_id is None
        assert middle[0].reply_to_telegram_message_id == 1


@pytest.mark.asyncio
async def test_incoming_replay_does_not_insert_or_update_activity(session_factory):
    async with session_factory() as session:
        user = User(telegram_id=10102, full_name="Тестовый пользователь")
        session.add(user)
        await session.flush()
        conv = await get_or_create_conversation(session, user_id=user.id)
        original, created = await record_incoming_message(
            session, conversation_id=conv.id, telegram_chat_id=10102,
            telegram_message_id=77, text="Первое", source="telegram",
        )
        assert created
        await session.commit()
        original_id = original.id
        timestamp = await session.scalar(select(Conversation.last_message_at).where(Conversation.id == conv.id))

    async with session_factory() as session:
        replay, created = await record_incoming_message(
            session, conversation_id=conv.id, telegram_chat_id=10102,
            telegram_message_id=77, text="Не перезаписывать", source="other",
        )
        assert created is False
        assert replay.id == original_id
        assert replay.text == "Первое"
        replay_timestamp = await session.scalar(
            select(Conversation.last_message_at).where(Conversation.id == conv.id)
        )
        assert replay_timestamp == timestamp
        await session.commit()
        assert (await session.scalar(select(func.count(ChatMessage.id)))) == 1


@pytest.mark.asyncio
async def test_unread_is_incoming_only_and_independent_for_admins(session_factory):
    async with session_factory() as session:
        user = User(telegram_id=10103, full_name="Тестовый пользователь")
        session.add(user)
        await session.flush()
        conv = await get_or_create_conversation(session, user_id=user.id)
        incoming = []
        for num in range(1, 6):
            message, _ = await record_incoming_message(
                session, conversation_id=conv.id, telegram_chat_id=10103,
                telegram_message_id=num, text=f"Текст {num}",
            )
            incoming.append(message.id)
        session.add(ChatMessage(conversation_id=conv.id, direction="outgoing", text="Ответ", source="admin"))
        await session.flush()
        outgoing_id = (await session.scalar(
            select(ChatMessage.id).where(ChatMessage.direction == "outgoing")
        ))
        assert outgoing_id is not None
        assert await get_unread_count(session, conversation_id=conv.id, admin_telegram_id=123) == 5
        assert await get_unread_count(session, conversation_id=conv.id, admin_telegram_id=456) == 5

        assert await advance_read_marker(
            session, conversation_id=conv.id, admin_telegram_id=123, message_id=incoming[1]
        ) == incoming[1]
        assert await get_unread_count(session, conversation_id=conv.id, admin_telegram_id=123) == 3
        assert await get_unread_count(session, conversation_id=conv.id, admin_telegram_id=456) == 5

        assert await advance_read_marker(
            session, conversation_id=conv.id, admin_telegram_id=123, message_id=incoming[0]
        ) == incoming[1]
        assert await advance_read_marker(
            session, conversation_id=conv.id, admin_telegram_id=456, message_id=outgoing_id
        ) == outgoing_id
        assert await get_unread_count(session, conversation_id=conv.id, admin_telegram_id=456) == 0
        assert await get_unread_count(session, conversation_id=conv.id, admin_telegram_id=123) == 3
        await session.commit()

    async with session_factory() as session:
        reads = list((await session.scalars(
            select(ConversationRead).order_by(ConversationRead.admin_telegram_id)
        )).all())
        assert [(row.admin_telegram_id, row.last_read_message_id) for row in reads] == [
            (123, incoming[1]), (456, outgoing_id)
        ]


@pytest.mark.asyncio
async def test_read_marker_requires_message_from_same_conversation(session_factory):
    async with session_factory() as session:
        first = User(telegram_id=10104, full_name="Первый")
        second = User(telegram_id=10105, full_name="Второй")
        session.add_all([first, second])
        await session.flush()
        one = await get_or_create_conversation(session, user_id=first.id)
        two = await get_or_create_conversation(session, user_id=second.id)
        message, _ = await record_incoming_message(
            session, conversation_id=one.id, telegram_chat_id=10104,
            telegram_message_id=1, text="Первое",
        )
        with pytest.raises(ValueError, match="existing message"):
            await advance_read_marker(
                session, conversation_id=two.id, admin_telegram_id=123, message_id=message.id
            )
        with pytest.raises(ValueError, match="existing message"):
            await advance_read_marker(
                session, conversation_id=one.id, admin_telegram_id=123, message_id=999999
            )
        with pytest.raises(ValueError, match="limit"):
            await list_conversation_messages(session, conversation_id=one.id, limit=101)
        with pytest.raises(ValueError, match="before_id"):
            await list_conversation_messages(session, conversation_id=one.id, before_id=0)
        assert await get_unread_count(session, conversation_id=two.id, admin_telegram_id=123) == 0
        assert (await session.scalar(select(func.count(ConversationRead.id)))) == 0
