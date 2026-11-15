"""User -> Telegram bot -> admin HTTP -> outbox worker -> Telegram -> admin HTTP."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.types import Message, Update
from sqlalchemy import func, select

from topsbottg.main import create_bot_dispatcher
from topsbottg.models import AdminGrant, ChatMessage, Payout, User
from topsbottg.services import get_admin_events_broadcaster, get_or_create_user
from topsbottg.worker import deliver_chat_outbox


class TelegramSession(BaseSession):
    async def make_request(self, bot, method, timeout=None):
        return Message.model_validate(
            {
                "message_id": 900,
                "date": int(datetime.now(UTC).timestamp()),
                "chat": {"id": int(getattr(method, "chat_id", 0)), "type": "private"},
                "text": getattr(method, "text", "OK"),
            },
            context={"bot": bot},
        )

    async def stream_content(self, *args, **kwargs):
        if False:
            yield b""

    async def close(self):
        return None


class DeliveredTelegram:
    def __init__(self):
        self.calls = []

    async def send_message(self, chat_id, text, **kwargs):
        self.calls.append((chat_id, text, kwargs))
        return SimpleNamespace(message_id=701)


def _update(bot, telegram_id: int, message_id: int, text: str) -> Update:
    return Update.model_validate(
        {
            "update_id": message_id,
            "message": {
                "message_id": message_id,
                "date": int(datetime.now(UTC).timestamp()),
                "chat": {"id": telegram_id, "type": "private"},
                "from": {"id": telegram_id, "first_name": "Test", "is_bot": False},
                "text": text,
            },
        },
        context={"bot": bot},
    )


@pytest.mark.asyncio
async def test_admin_inbox_complete_delivery_and_independent_unread(
    client, session_factory, settings, admin_init_data, dynamic_admin_init_data,
):
    async with session_factory() as session:
        user = await get_or_create_user(session, 82901, "Участник переписки")
        session.add(AdminGrant(
            telegram_id=555, granted_by_telegram_id=123, granted_at=datetime.now(UTC),
        ))
        await session.commit()
        user_id = user.id

    bot = Bot("123:abc", session=TelegramSession())
    dispatcher = create_bot_dispatcher(session_factory, settings)
    events = get_admin_events_broadcaster().subscribe()
    try:
        await dispatcher.feed_update(bot, _update(bot, 82901, 100, "/start"))
        await dispatcher.feed_update(bot, _update(bot, 82901, 101, "Вопрос по расписанию"))
        incoming_event = events.get_nowait()
        assert incoming_event["event"] == "chat.changed"
        conversation_id = incoming_event["payload"]["conversation_id"]
        assert "Вопрос по расписанию" not in str(incoming_event)

        owner = {"X-Telegram-Init-Data": admin_init_data}
        other = {"X-Telegram-Init-Data": dynamic_admin_init_data}
        list_response = await client.get("/api/admin/conversations", headers=owner)
        assert list_response.status_code == 200
        assert list_response.json()["items"][0]["user"]["id"] == user_id
        assert list_response.json()["items"][0]["id"] == conversation_id
        assert list_response.json()["items"][0]["unread_count"] == 1

        history_url = f"/api/admin/conversations/{conversation_id}/messages"
        history = await client.get(history_url, headers=other)
        assert history.status_code == 200
        incoming = history.json()["items"][-1]
        assert incoming["direction"] == "incoming"
        assert incoming["text"] == "Вопрос по расписанию"

        read_url = f"/api/admin/conversations/{conversation_id}/read"
        read = await client.post(read_url, headers=owner, json={
            "last_read_message_id": incoming["id"],
        })
        assert read.status_code == 200
        assert read.json()["unread_count"] == 0
        summary_url = "/api/admin/conversations/summary"
        assert (await client.get(summary_url, headers=owner)).json()["unread_count"] == 0
        assert (await client.get(summary_url, headers=other)).json()["unread_count"] == 1

        send_url = history_url
        send_headers = {**owner, "Idempotency-Key": "inbox-e2e-reply-1"}
        body = {"text": "Расписание обновлено", "reply_to_message_id": incoming["id"]}
        accepted = await client.post(send_url, headers=send_headers, json=body)
        assert accepted.status_code == 202
        outgoing = accepted.json()["message"]
        assert outgoing["delivery_status"] == "queued"
        assert outgoing["author_admin_telegram_id"] == 123

        duplicate = await client.post(send_url, headers=send_headers, json=body)
        assert duplicate.status_code == 202
        assert duplicate.json()["message"]["id"] == outgoing["id"]
        assert (await client.post(send_url, headers=send_headers,
                                  json={"text": "Не тот текст"})).status_code == 409

        telegram = DeliveredTelegram()
        await deliver_chat_outbox(telegram, session_factory)
        assert len(telegram.calls) == 1
        assert telegram.calls[0][0] == 82901
        assert telegram.calls[0][1] == "Расписание обновлено"
        assert telegram.calls[0][2]["reply_parameters"].message_id == 101
        await deliver_chat_outbox(telegram, session_factory)
        assert len(telegram.calls) == 1

        sent_history = await client.get(history_url, headers=other)
        sent = next(item for item in sent_history.json()["items"] if item["id"] == outgoing["id"])
        assert sent["delivery_status"] == "sent"
        assert sent["telegram_message_id"] == 701
        assert sent["reply_to_telegram_message_id"] == 101
        changed_events = []
        while not events.empty():
            changed_events.append(events.get_nowait())
        assert any(event["event"] == "chat.changed" and
                   event["payload"] == {"conversation_id": conversation_id}
                   for event in changed_events)
        assert all(set(event["payload"]) <= {"conversation_id"} for event in changed_events)

        async with session_factory() as session:
            assert await session.scalar(select(func.count(Payout.id))) == 0
            assert await session.scalar(select(func.count(ChatMessage.id))) == 2
            grant = await session.scalar(select(AdminGrant).where(AdminGrant.telegram_id == 555))
            grant.revoked_at = datetime.now(UTC)
            await session.commit()

        assert (await client.get(history_url, headers=other)).status_code == 403
        assert (await client.get(summary_url, headers=other)).status_code == 403
        assert (await client.get(history_url)).status_code == 401
        assert (await client.get(summary_url, headers=owner)).status_code == 200
    finally:
        get_admin_events_broadcaster().unsubscribe(events)
        await bot.session.close()
