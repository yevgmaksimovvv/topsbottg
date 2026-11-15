"""Outgoing conversation HTTP contract and Telegram delivery retry tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.methods import SendMessage
from sqlalchemy import func, select

from topsbottg.chat_outbox import (
    MAX_CHAT_ATTEMPTS,
    claim_chat_outbox,
    create_outgoing_message,
    settle_chat_outbox,
)
from topsbottg.conversations import record_incoming_message
from topsbottg.models import (
    AdminGrant,
    ChatMessage,
    ChatMessageOutbox,
    Conversation,
    Payout,
    User,
)
from topsbottg.services import get_admin_events_broadcaster
from topsbottg.worker import deliver_chat_outbox


async def _conversation(session_factory, telegram_id: int = 81001) -> tuple[int, int]:
    async with session_factory() as session:
        user = User(
            telegram_id=telegram_id, full_name="Тестовый пользователь",
            registration_completed_at=datetime.now(UTC),
        )
        session.add(user)
        await session.flush()
        conversation = Conversation(user_id=user.id)
        session.add(conversation)
        await session.commit()
        return conversation.id, user.telegram_id


async def _outgoing(session_factory, conversation_id: int, *, key: str = "test-key") -> int:
    async with session_factory() as session:
        msg, _ = await create_outgoing_message(
            session, conversation_id=conversation_id, admin_telegram_id=123,
            idempotency_key=key, text="Привет",
        )
        await session.commit()
        return msg.id


class FakeTelegram:
    def __init__(self, responses: list[object] | None = None):
        self.responses = list(responses or [])
        self.calls: list[tuple[int, str, dict]] = []

    async def send_message(self, chat_id: int, text: str, **kwargs):
        self.calls.append((chat_id, text, kwargs))
        value = self.responses.pop(0) if self.responses else SimpleNamespace(message_id=4000 + len(self.calls))
        if isinstance(value, Exception):
            raise value
        return value


@pytest.mark.asyncio
async def test_outgoing_api_reuses_key_and_publishes_only_after_commit(
    client, session_factory, admin_init_data
):
    conversation_id, chat_id = await _conversation(session_factory)
    headers = {"X-Telegram-Init-Data": admin_init_data, "Idempotency-Key": "request:123-1"}
    url = f"/api/admin/conversations/{conversation_id}/messages"
    subscriber = get_admin_events_broadcaster().subscribe()
    try:
        response = await client.post(url, headers=headers, json={"text": "Вопрос администратору"})
        assert response.status_code == 202
        message = response.json()["message"]
        assert message["direction"] == "outgoing"
        assert message["text"] == "Вопрос администратору"
        assert message["delivery_status"] == "queued"
        assert message["delivery_error_code"] is None
        assert message["author_admin_telegram_id"] == 123
        assert message["telegram_message_id"] is None
        assert subscriber.get_nowait() == {
            "event": "chat.changed", "payload": {"conversation_id": conversation_id},
        }
        assert subscriber.empty()

        repeated = await client.post(url, headers=headers, json={"text": "Вопрос администратору"})
        assert repeated.status_code == 202
        assert repeated.json() == response.json()
        assert subscriber.empty()

        for changed in ({"text": "Новый вопрос"}, {"text": "Вопрос администратору", "reply_to_message_id": 33}):
            assert (await client.post(url, headers=headers, json=changed)).status_code == 409
        assert subscriber.empty()
    finally:
        get_admin_events_broadcaster().unsubscribe(subscriber)

    async with session_factory() as session:
        assert await session.scalar(select(func.count(ChatMessage.id))) == 1
        assert await session.scalar(select(func.count(ChatMessageOutbox.id))) == 1
        outbox = await session.scalar(select(ChatMessageOutbox))
        assert outbox.status == "queued" and outbox.attempts == 0
        assert outbox.reply_to_message_id is None
        assert len(outbox.payload_fingerprint) == 64

    history = await client.get(url, headers=headers)
    assert history.status_code == 200
    assert history.json()["items"][0]["delivery_status"] == "queued"
    listing = await client.get("/api/admin/conversations", headers=headers)
    assert listing.json()["items"][0]["last_message"]["delivery_status"] == "queued"
    assert listing.json()["items"][0]["user"]["telegram_id"] == chat_id


@pytest.mark.asyncio
async def test_outgoing_api_auth_validation_and_revocation(
    client, session_factory, admin_init_data, dynamic_admin_init_data, non_admin_init_data
):
    cid, _ = await _conversation(session_factory)
    path = f"/api/admin/conversations/{cid}/messages"
    headers = {"X-Telegram-Init-Data": admin_init_data, "Idempotency-Key": "safe-key"}
    body = {"text": "Привет"}
    assert (await client.post(path, headers={"Idempotency-Key": "safe-key"}, json=body)).status_code == 401
    assert (await client.post(path, headers={
        "X-Telegram-Init-Data": non_admin_init_data, "Idempotency-Key": "safe-key"
    }, json=body)).status_code == 403
    assert (await client.post(path, headers={
        "X-Telegram-Init-Data": dynamic_admin_init_data, "Idempotency-Key": "safe-key"
    }, json=body)).status_code == 403
    assert (await client.post(path, headers={"X-Telegram-Init-Data": admin_init_data}, json=body)).status_code == 422
    for invalid in ("bad key", "", "x" * 129):
        response = await client.post(path, headers={**headers, "Idempotency-Key": invalid}, json=body)
        assert response.status_code == 422
    for invalid_body in ({}, {"text": "  "}, {"text": "x" * 4097},
                         {"text": "x", "reply_to_message_id": -1},
                         {"text": "x", "reply_to_message_id": "1"}):
        assert (await client.post(path, headers=headers, json=invalid_body)).status_code == 422
    assert (await client.post("/api/admin/conversations/999999/messages", headers=headers, json=body)
            ).status_code == 404
    assert (await client.post(path, headers=headers, json={"text": "x", "reply_to_message_id": 999})
            ).status_code == 404

    async with session_factory() as session:
        session.add(AdminGrant(telegram_id=555, granted_by_telegram_id=123, granted_at=datetime.now(UTC)))
        await session.commit()
    dynamic = {"X-Telegram-Init-Data": dynamic_admin_init_data, "Idempotency-Key": "dynamic"}
    assert (await client.post(path, headers=dynamic, json=body)).status_code == 202
    async with session_factory() as session:
        grant = await session.scalar(select(AdminGrant).where(AdminGrant.telegram_id == 555))
        grant.revoked_at = datetime.now(UTC)
        await session.commit()
    assert (await client.post(path, headers={**dynamic, "Idempotency-Key": "new-key"}, json=body)
            ).status_code == 403


@pytest.mark.asyncio
async def test_worker_sends_reply_and_updates_delivery_status(
    client, session_factory, admin_init_data
):
    cid, telegram_id = await _conversation(session_factory)
    async with session_factory() as session:
        reply, _ = await record_incoming_message(
            session, conversation_id=cid, telegram_chat_id=telegram_id,
            telegram_message_id=31, text="Исходный вопрос",
        )
        await session.commit()
        reply_id = reply.id
        payout_count = await session.scalar(select(func.count(Payout.id)))
    headers = {"X-Telegram-Init-Data": admin_init_data, "Idempotency-Key": "reply-1"}
    path = f"/api/admin/conversations/{cid}/messages"
    response = await client.post(
        path, headers=headers,
        json={"text": "Ответ", "reply_to_message_id": reply_id},
    )
    assert response.status_code == 202
    outgoing_id = response.json()["message"]["id"]
    bot = FakeTelegram()
    await deliver_chat_outbox(bot, session_factory)
    assert bot.calls[0][0] == telegram_id
    assert bot.calls[0][1] == "Ответ"
    assert bot.calls[0][2]["reply_parameters"].message_id == 31
    async with session_factory() as session:
        outgoing = await session.get(ChatMessage, outgoing_id)
        assert outgoing.telegram_message_id == 4001
        assert outgoing.reply_to_telegram_message_id == 31
        outbox = await session.scalar(select(ChatMessageOutbox))
        assert outbox.status == "sent" and outbox.attempts == 1
        assert outbox.telegram_message_id == 4001
        assert outbox.error_code is None
        assert await session.scalar(select(func.count(Payout.id))) == payout_count
    history = (await client.get(path, headers=headers)).json()["items"]
    assert history[-1]["delivery_status"] == "sent"
    assert history[-1]["telegram_message_id"] == 4001
    await deliver_chat_outbox(bot, session_factory)
    assert len(bot.calls) == 1


@pytest.mark.asyncio
async def test_retry_429_timeout_and_terminal_blocked(session_factory):
    cid, chat_id = await _conversation(session_factory)
    await _outgoing(session_factory, cid)
    method = SendMessage(chat_id=chat_id, text="Привет")
    bot = FakeTelegram([
        TelegramRetryAfter(method=method, message="Too Many Requests", retry_after=42),
        TimeoutError(),
        SimpleNamespace(message_id=987),
    ])
    await deliver_chat_outbox(bot, session_factory)
    async with session_factory() as session:
        row = await session.scalar(select(ChatMessageOutbox))
        assert row.status == "queued" and row.attempts == 1 and row.error_code == "rate_limited"
        assert row.retry_at is not None
        assert row.retry_at.replace(tzinfo=UTC) > datetime.now(UTC) + timedelta(seconds=30)
        row.retry_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()
    await deliver_chat_outbox(bot, session_factory)
    async with session_factory() as session:
        row = await session.scalar(select(ChatMessageOutbox))
        assert row.status == "queued" and row.attempts == 2 and row.error_code == "timeout"
        row.retry_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()
    await deliver_chat_outbox(bot, session_factory)
    async with session_factory() as session:
        row = await session.scalar(select(ChatMessageOutbox))
        assert row.status == "sent" and row.attempts == 3 and row.error_code is None

    await _outgoing(session_factory, cid, key="blocked")
    forbidden = FakeTelegram([
        TelegramForbiddenError(method=method, message="Forbidden: bot was blocked by the user")
    ])
    await deliver_chat_outbox(forbidden, session_factory)
    async with session_factory() as session:
        blocked = await session.scalar(select(ChatMessageOutbox).where(
            ChatMessageOutbox.idempotency_key == "blocked"
        ))
        assert blocked.status == "failed" and blocked.error_code == "blocked"
        assert blocked.retry_at is None
    assert len(forbidden.calls) == 1


@pytest.mark.asyncio
async def test_retry_attempts_bounded_and_bad_chat_is_terminal(session_factory):
    cid, chat_id = await _conversation(session_factory)
    await _outgoing(session_factory, cid)
    bot = FakeTelegram([TimeoutError() for _ in range(MAX_CHAT_ATTEMPTS)])
    for attempt in range(1, MAX_CHAT_ATTEMPTS + 1):
        await deliver_chat_outbox(bot, session_factory)
        async with session_factory() as session:
            row = await session.scalar(select(ChatMessageOutbox))
            assert row.attempts == attempt
            assert row.status == ("failed" if attempt == MAX_CHAT_ATTEMPTS else "queued")
            if row.status == "queued":
                row.retry_at = datetime.now(UTC) - timedelta(seconds=1)
            await session.commit()
    await deliver_chat_outbox(bot, session_factory)
    assert len(bot.calls) == MAX_CHAT_ATTEMPTS

    await _outgoing(session_factory, cid, key="not-found")
    bad_request = FakeTelegram([
        TelegramBadRequest(
            method=SendMessage(chat_id=chat_id, text="Привет"), message="Bad Request: chat not found"
        )
    ])
    await deliver_chat_outbox(bad_request, session_factory)
    async with session_factory() as session:
        row = await session.scalar(select(ChatMessageOutbox).where(
            ChatMessageOutbox.idempotency_key == "not-found"
        ))
        assert row.status == "failed" and row.error_code == "chat_not_found"


@pytest.mark.asyncio
async def test_reply_to_pending_outgoing_waits_without_using_delivery_attempt(session_factory):
    cid, _ = await _conversation(session_factory)
    async with session_factory() as session:
        parent, created = await create_outgoing_message(
            session, conversation_id=cid, admin_telegram_id=123,
            idempotency_key="parent", text="Первый",
        )
        assert created
        child, created = await create_outgoing_message(
            session, conversation_id=cid, admin_telegram_id=123,
            idempotency_key="child", text="Ответ на первый", reply_to_message_id=parent.id,
        )
        assert created
        await session.commit()
        parent_id, child_id = parent.id, child.id

    async with session_factory() as session:
        [claim] = await claim_chat_outbox(session, limit=2)
        assert claim.message_id == parent_id
        waiting = await session.scalar(
            select(ChatMessageOutbox).where(ChatMessageOutbox.message_id == child_id)
        )
        assert waiting.status == "queued" and waiting.attempts == 0
        assert waiting.retry_at is not None
        await session.commit()

    async with session_factory() as session:
        assert await settle_chat_outbox(session, claim, telegram_message_id=678)
        await session.commit()

    async with session_factory() as session:
        waiting = await session.scalar(
            select(ChatMessageOutbox).where(ChatMessageOutbox.message_id == child_id)
        )
        waiting.retry_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()

    bot = FakeTelegram([SimpleNamespace(message_id=679)])
    await deliver_chat_outbox(bot, session_factory)
    assert len(bot.calls) == 1
    assert bot.calls[0][2]["reply_parameters"].message_id == 678
    async with session_factory() as session:
        waiting = await session.scalar(
            select(ChatMessageOutbox).where(ChatMessageOutbox.message_id == child_id)
        )
        assert waiting.status == "sent" and waiting.attempts == 1


@pytest.mark.asyncio
async def test_reply_to_failed_outgoing_becomes_terminal_without_send(session_factory):
    cid, _ = await _conversation(session_factory)
    async with session_factory() as session:
        parent, _ = await create_outgoing_message(
            session, conversation_id=cid, admin_telegram_id=123,
            idempotency_key="failed-parent", text="Первый",
        )
        child, _ = await create_outgoing_message(
            session, conversation_id=cid, admin_telegram_id=123,
            idempotency_key="failed-child", text="Ответ на первый", reply_to_message_id=parent.id,
        )
        await session.commit()
        child_id = child.id

    async with session_factory() as session:
        [parent_claim] = await claim_chat_outbox(session, limit=2)
        await session.commit()

    async with session_factory() as session:
        assert await settle_chat_outbox(
            session, parent_claim, error_code="blocked", permanent=True
        )
        child_row = await session.scalar(
            select(ChatMessageOutbox).where(ChatMessageOutbox.message_id == child_id)
        )
        child_row.retry_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()

    bot = FakeTelegram()
    await deliver_chat_outbox(bot, session_factory)
    assert bot.calls == []
    async with session_factory() as session:
        child_row = await session.scalar(
            select(ChatMessageOutbox).where(ChatMessageOutbox.message_id == child_id)
        )
        assert child_row.status == "failed"
        assert child_row.error_code == "reply_unavailable"
        assert child_row.attempts == 0
