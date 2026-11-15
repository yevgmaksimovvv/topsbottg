"""Conversation HTTP contract, authorization, and post-commit notifications."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from topsbottg.conversations import get_or_create_conversation, record_incoming_message
from topsbottg.models import AdminGrant, ChatMessage, Conversation, ConversationRead, StudyGroup, User
from topsbottg.services import get_admin_events_broadcaster


async def _users(session_factory):
    async with session_factory() as session:
        group = StudyGroup(display_code="М3О-207СВ-25", normalized_code="М3О-207СВ-25")
        session.add(group)
        await session.flush()
        now = datetime.now(UTC)
        users = [
            User(telegram_id=90001, full_name="Анна Петрова", study_group_id=group.id,
                 registration_completed_at=now),
            User(telegram_id=90002, full_name="Борис Смирнов", registration_completed_at=now),
            User(telegram_id=90003, full_name="Без ФИО"),
            User(telegram_id=90004, full_name="Заполняет группу"),
            User(telegram_id=90005, full_name="Тест Без сообщений", registration_completed_at=now),
        ]
        session.add_all(users)
        session.add(AdminGrant(
            telegram_id=555, granted_by_telegram_id=123, granted_at=now,
        ))
        await session.commit()
        return [user.id for user in users], group.id


async def _write(session_factory, conversation_id, number, text="Обычное сообщение"):
    async with session_factory() as session:
        message, created = await record_incoming_message(
            session, conversation_id=conversation_id, telegram_chat_id=90001,
            telegram_message_id=number, text=text,
            reply_to_telegram_message_id=7 if number == 1 else None,
            source="telegram",
        )
        await session.commit()
        return message.id, created


@pytest.mark.asyncio
async def test_open_requires_completed_registration_and_is_idempotent(
    client, session_factory, admin_init_data, dynamic_admin_init_data
):
    user_ids, _ = await _users(session_factory)
    owner = {"X-Telegram-Init-Data": admin_init_data}
    admin = {"X-Telegram-Init-Data": dynamic_admin_init_data}
    path = "/api/admin/conversations"

    for user_id in (user_ids[2], user_ids[3], 987654):
        result = await client.post(path, headers=owner, json={"user_id": user_id})
        assert result.status_code == 404
    for user_id in (0, -1, "1"):
        result = await client.post(path, headers=owner, json={"user_id": user_id})
        assert result.status_code == 422
    result = await client.post(path, headers=owner, json={"user_id": user_ids[1]})
    assert result.status_code == 201
    identifier = result.json()["conversation_id"]
    repeat = await client.post(path, headers=admin, json={"user_id": user_ids[1]})
    assert repeat.status_code == 200
    assert repeat.json() == {"conversation_id": identifier}

    async with session_factory() as session:
        assert (await session.scalars(select(Conversation))).all() != []
        assert (await session.scalars(select(ChatMessage))).all() == []
    assert (await client.post(path, json={"user_id": user_ids[1]})).status_code == 401


@pytest.mark.asyncio
async def test_conversations_list_search_group_pagination_and_null_activity(
    client, session_factory, admin_init_data, dynamic_admin_init_data
):
    user_ids, group_id = await _users(session_factory)
    headers = {"X-Telegram-Init-Data": admin_init_data}
    ids = []
    for user_id in (user_ids[0], user_ids[1], user_ids[4]):
        res = await client.post("/api/admin/conversations", json={"user_id": user_id}, headers=headers)
        ids.append(res.json()["conversation_id"])
    await _write(session_factory, ids[0], 1, "Привет")
    await _write(session_factory, ids[1], 2, "Вопрос")

    path = "/api/admin/conversations"
    first = await client.get(path, headers=headers, params={"limit": 1})
    assert first.status_code == 200
    page1 = first.json()
    assert [item["id"] for item in page1["items"]] == [ids[1]]
    assert page1["next_cursor"] is not None
    assert page1["items"][0]["unread_count"] == 1
    assert page1["items"][0]["user"]["study_group_name"] is None
    assert page1["items"][0]["last_message_at"].endswith("+00:00")
    second = await client.get(path, headers=headers, params={"cursor": page1["next_cursor"], "limit": 1})
    assert [item["id"] for item in second.json()["items"]] == [ids[0]]
    third = await client.get(path, headers=headers, params={
        "cursor": second.json()["next_cursor"], "limit": 1,
    })
    assert [item["id"] for item in third.json()["items"]] == [ids[2]]
    assert third.json()["items"][0]["last_message"] is None
    assert third.json()["items"][0]["last_message_at"] is None
    assert third.json()["next_cursor"] is None

    owner_search = await client.get(path, headers=headers, params={"query": "АнНа", "study_group_id": group_id})
    admin_search = await client.get(path, headers={"X-Telegram-Init-Data": dynamic_admin_init_data},
                                    params={"query": "АнНа", "study_group_id": group_id})
    assert owner_search.json() == admin_search.json()
    assert len(owner_search.json()["items"]) == 1
    assert owner_search.json()["items"][0]["user"]["full_name"] == "Анна Петрова"
    assert (await client.get(path, headers=headers, params={"query": "Никого"})).json() == {
        "items": [], "next_cursor": None,
    }
    assert (await client.get(path, headers=headers, params={"query": "%"})).json()["items"] == []
    assert (await client.get(path, headers=headers, params={"query": "_"})).json()["items"] == []
    assert (await client.get(path, headers=headers, params={
        "query": "Анна", "cursor": page1["next_cursor"],
    })).status_code == 422
    assert (await client.get(path, headers=headers, params={
        "study_group_id": group_id, "cursor": page1["next_cursor"],
    })).status_code == 422


@pytest.mark.asyncio
async def test_history_message_contract_and_backwards_cursor(
    client, session_factory, admin_init_data, dynamic_admin_init_data
):
    user_ids, _ = await _users(session_factory)
    owner = {"X-Telegram-Init-Data": admin_init_data}
    admin = {"X-Telegram-Init-Data": dynamic_admin_init_data}
    created = await client.post("/api/admin/conversations", headers=owner, json={"user_id": user_ids[0]})
    conv_id = created.json()["conversation_id"]
    ids = [(await _write(session_factory, conv_id, i))[0] for i in range(1, 6)]
    url = f"/api/admin/conversations/{conv_id}/messages"
    newest = await client.get(url, headers=owner, params={"limit": 2})
    assert newest.status_code == 200
    assert [msg["id"] for msg in newest.json()["items"]] == ids[3:]
    msg = newest.json()["items"][-1]
    assert set(msg) == {
        "id", "conversation_id", "direction", "kind", "text", "caption", "created_at",
        "author_admin_telegram_id", "telegram_message_id", "reply_to_telegram_message_id",
        "delivery_status", "delivery_error_code", "attachment",
    }
    assert msg["attachment"] is None
    assert msg["delivery_status"] is None
    assert msg["direction"] == "incoming"
    assert msg["created_at"].endswith("+00:00")
    assert (await client.get(url, headers=admin, params={"limit": 2})).json() == newest.json()
    older = await client.get(url, headers=owner, params={
        "limit": 2, "before": newest.json()["next_cursor"],
    })
    assert [msg["id"] for msg in older.json()["items"]] == ids[1:3]
    oldest = await client.get(url, headers=owner, params={
        "limit": 2, "before": older.json()["next_cursor"],
    })
    assert [msg["id"] for msg in oldest.json()["items"]] == ids[:1]
    assert oldest.json()["next_cursor"] is None


@pytest.mark.asyncio
async def test_read_markers_are_personal_monotone_and_validate_message_ownership(
    client, session_factory, admin_init_data, dynamic_admin_init_data
):
    user_ids, _ = await _users(session_factory)
    owner = {"X-Telegram-Init-Data": admin_init_data}
    admin = {"X-Telegram-Init-Data": dynamic_admin_init_data}
    conv1 = (await client.post("/api/admin/conversations", headers=owner,
                              json={"user_id": user_ids[0]})).json()["conversation_id"]
    conv2 = (await client.post("/api/admin/conversations", headers=owner,
                              json={"user_id": user_ids[1]})).json()["conversation_id"]
    first, _ = await _write(session_factory, conv1, 11)
    second, _ = await _write(session_factory, conv1, 12)
    foreign, _ = await _write(session_factory, conv2, 13)
    summary = "/api/admin/conversations/summary"
    assert (await client.get(summary, headers=owner)).json() == {"unread_count": 3}
    assert (await client.get(summary, headers=admin)).json() == {"unread_count": 3}
    path = f"/api/admin/conversations/{conv1}/read"
    read = await client.post(path, headers=owner, json={"last_read_message_id": second})
    assert read.status_code == 200
    assert read.json() == {"last_read_message_id": second, "unread_count": 0}
    assert (await client.get(summary, headers=owner)).json() == {"unread_count": 1}
    assert (await client.get(summary, headers=admin)).json() == {"unread_count": 3}
    older = await client.post(path, headers=owner, json={"last_read_message_id": first})
    assert older.status_code == 200 and older.json()["last_read_message_id"] == second
    assert (await client.post(path, headers=owner, json={"last_read_message_id": foreign})).status_code == 404
    assert (await client.post(path, headers=owner, json={"last_read_message_id": 9999999})).status_code == 404
    assert (await client.post(path, headers=owner, json={"last_read_message_id": -1})).status_code == 422
    assert (await client.post(path, headers=admin, json={"last_read_message_id": first})).json() == {
        "last_read_message_id": first, "unread_count": 1,
    }
    async with session_factory() as session:
        saved = (await session.scalars(select(ConversationRead).where(
            ConversationRead.conversation_id == conv1
        ))).all()
        assert {(marker.admin_telegram_id, marker.last_read_message_id) for marker in saved} == {
            (123, second), (555, first),
        }


@pytest.mark.asyncio
async def test_conversation_auth_revocation_not_found_and_invalid_cursor(
    client, session_factory, settings, admin_init_data, dynamic_admin_init_data,
    non_admin_init_data, invalid_init_data
):
    user_ids, _ = await _users(session_factory)
    valid = {"X-Telegram-Init-Data": admin_init_data}
    dynamic = {"X-Telegram-Init-Data": dynamic_admin_init_data}
    routes = [
        ("post", "/api/admin/conversations", {"json": {"user_id": user_ids[0]}}),
        ("get", "/api/admin/conversations", {}),
        ("get", "/api/admin/conversations/summary", {}),
        ("get", "/api/admin/conversations/999/messages", {}),
        ("post", "/api/admin/conversations/999/read", {"json": {"last_read_message_id": 1}}),
    ]
    for method, path, kwargs in routes:
        assert (await getattr(client, method)(path, **kwargs)).status_code == 401
        assert (await getattr(client, method)(
            path, headers={"X-Telegram-Init-Data": non_admin_init_data}, **kwargs
        )).status_code == 403
        assert (await getattr(client, method)(
            path, headers={"X-Telegram-Init-Data": invalid_init_data}, **kwargs
        )).status_code == 401
    assert (await client.get("/api/admin/conversations/999/messages", headers=valid)).status_code == 404
    assert (await client.post("/api/admin/conversations/999/read", headers=valid,
                              json={"last_read_message_id": 1})).status_code == 404

    conv = (await client.post("/api/admin/conversations", headers=valid,
                              json={"user_id": user_ids[0]})).json()["conversation_id"]
    url = f"/api/admin/conversations/{conv}/messages"
    assert (await client.get(url, headers=valid)).json() == {"items": [], "next_cursor": None}
    for params in ({"limit": 0}, {"limit": 101}, {"cursor": "@@bad@@"}):
        assert (await client.get("/api/admin/conversations", headers=valid, params=params)).status_code == 422
    for params in ({"limit": 0}, {"limit": 101}, {"before": "***bad***"}):
        assert (await client.get(url, headers=valid, params=params)).status_code == 422

    assert (await client.get("/api/admin/conversations", headers=dynamic)).status_code == 200
    async with session_factory() as session:
        grant = await session.scalar(select(AdminGrant).where(AdminGrant.telegram_id == 555))
        grant.revoked_at = datetime.now(UTC)
        await session.commit()
    for method, path, kwargs in routes:
        assert (await getattr(client, method)(path, headers=dynamic, **kwargs)).status_code == 403


@pytest.mark.asyncio
async def test_chat_changed_event_only_after_commit_and_without_message_body(
    app, session_factory
):
    user_ids, _ = await _users(session_factory)
    broadcaster = get_admin_events_broadcaster()
    queue = broadcaster.subscribe()
    try:
        async with session_factory() as session:
            conv = await get_or_create_conversation(session, user_id=user_ids[0])
            message, inserted = await record_incoming_message(
                session, conversation_id=conv.id, telegram_chat_id=90001,
                telegram_message_id=8001, text="Обычный текст, только в БД",
            )
            assert inserted and message.id > 0
            assert queue.empty()
            await session.rollback()
        assert queue.empty()

        async with session_factory() as session:
            conv = await get_or_create_conversation(session, user_id=user_ids[0])
            message, inserted = await record_incoming_message(
                session, conversation_id=conv.id, telegram_chat_id=90001,
                telegram_message_id=8002, text="Частная переписка",
            )
            assert inserted and queue.empty()
            conv_id = conv.id
            await session.commit()
        event = await asyncio.wait_for(queue.get(), timeout=1)
        assert event == {"event": "chat.changed", "payload": {"conversation_id": conv_id}}
        assert "Частная переписка" not in str(event)
        async with session_factory() as session:
            same, inserted = await record_incoming_message(
                session, conversation_id=conv_id, telegram_chat_id=90001,
                telegram_message_id=8002, text="Дубликат",
            )
            assert not inserted and same.id == message.id
            await session.commit()
        assert queue.empty()
    finally:
        broadcaster.unsubscribe(queue)
