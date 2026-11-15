"""Telegram attachment ingestion and authenticated file proxy."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.types import Update
from httpx import AsyncClient
from sqlalchemy import select

from topsbottg.bot import CHANGE_PAYMENT_DETAILS_BUTTON_TEXT
from topsbottg.main import create_bot_dispatcher
from topsbottg.models import AdminGrant, ChatAttachment, ChatMessage, Conversation, User
from topsbottg.services import get_admin_events_broadcaster, get_or_create_user


class MockTelegramSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.api = SimpleNamespace(file_url=lambda token, path: f"https://api.telegram.org/file/bot{token}/{path}")
        self.requested_url = None
        self.downloads = [b"%PDF-1.7\nprivate content"]
        self.raise_download = False

    async def make_request(self, bot, method, timeout=None):
        return True

    async def stream_content(self, url, *args, **kwargs):
        self.requested_url = url
        if self.raise_download:
            raise ConnectionError("SECRET-FILE-URL-123:abc")
        for chunk in self.downloads:
            yield chunk

    async def close(self):
        return None


class MockBot:
    def __init__(self):
        self.token = "123:private-token"
        self.session = MockTelegramSession()
        self.file_path = "documents/sample.pdf"
        self.file_size = 24
        self.get_file_calls = 0
        self.unavailable = False

    async def get_file(self, file_id):
        self.get_file_calls += 1
        if self.unavailable:
            from aiogram.exceptions import TelegramBadRequest
            from aiogram.methods import GetFile
            raise TelegramBadRequest(GetFile(file_id=file_id), "file not found 123:private-token")
        return SimpleNamespace(file_path=self.file_path, file_size=self.file_size)


def _media_update(bot, *, user_id=411, message_id=1001, kind="photo", caption=None, reply_id=None,
                  chat_type="private", file_size=None):
    item = {
        "message_id": message_id,
        "date": int(datetime.now(UTC).timestamp()),
        "chat": {"id": user_id if chat_type == "private" else -10001, "type": chat_type},
        "from": {"id": user_id, "first_name": "Test", "is_bot": False},
    }
    if caption is not None:
        item["caption"] = caption
    if reply_id is not None:
        item["reply_to_message"] = {
            "message_id": reply_id,
            "date": item["date"],
            "chat": item["chat"],
            "text": "Исходное сообщение",
        }
    if kind == "photo":
        item["photo"] = [
            {"file_id": "small-private-id", "file_unique_id": "small", "width": 90, "height": 90,
             "file_size": 1200},
            {"file_id": "large-private-id", "file_unique_id": "large", "width": 1080, "height": 720,
             "file_size": 120000},
        ]
    elif kind == "document":
        item["document"] = {
            "file_id": "pdf-private-id", "file_unique_id": "pdf-unique",
            "file_name": "document.pdf", "mime_type": "application/pdf",
            "file_size": file_size if file_size is not None else 1024,
        }
    elif kind == "unsupported":
        item["document"] = {
            "file_id": "svg-private-id", "file_unique_id": "svg-unique",
            "file_name": "document.svg", "mime_type": "image/svg+xml",
            "file_size": 123,
        }
    elif kind == "voice":
        item["voice"] = {
            "file_id": f"voice-private-{message_id}", "file_unique_id": f"voice-{message_id}",
            "duration": 7, "mime_type": "audio/ogg",
            "file_size": file_size if file_size is not None else 3456,
        }
    return Update.model_validate({"update_id": message_id, "message": item}, context={"bot": bot})


async def _registered_user(session_factory, telegram_id=411):
    async with session_factory() as session:
        user = await get_or_create_user(session, telegram_id, "Готовый пользователь")
        await session.commit()
        return user.id


@pytest.mark.asyncio
async def test_four_telegram_media_messages_share_one_dialog_and_protect_metadata(
    session_factory, client: AsyncClient, settings, admin_init_data, caplog
):
    caplog.set_level(logging.INFO)
    await _registered_user(session_factory)
    dispatcher = create_bot_dispatcher(session_factory, settings)
    bot = Bot("123:abc", session=MockTelegramSession())
    # The persistent FSM allows media only after this bot has established the context.
    await dispatcher.feed_update(bot, Update.model_validate({
        "update_id": 1,
        "message": {
            "message_id": 1, "date": int(datetime.now(UTC).timestamp()),
            "chat": {"id": 411, "type": "private"},
            "from": {"id": 411, "first_name": "Test", "is_bot": False},
            "text": "/start",
        },
    }, context={"bot": bot}))
    events = get_admin_events_broadcaster().subscribe()
    try:
        await dispatcher.feed_update(bot, _media_update(
            bot, message_id=201, kind="photo", caption="Фото с подписью", reply_id=98,
        ))
        await dispatcher.feed_update(bot, _media_update(bot, message_id=202, kind="document"))
        await dispatcher.feed_update(bot, _media_update(bot, message_id=203, kind="voice"))
        await dispatcher.feed_update(bot, _media_update(bot, message_id=204, kind="voice"))
        # Telegram may redeliver an update after reconnect.
        await dispatcher.feed_update(bot, _media_update(bot, message_id=204, kind="voice"))
        assert events.qsize() == 4
        assert all((events.get_nowait()["payload"]).keys() == {"conversation_id"} for _ in range(4))
    finally:
        get_admin_events_broadcaster().unsubscribe(events)

    async with session_factory() as session:
        convs = (await session.scalars(select(Conversation))).all()
        assert len(convs) == 1
        messages = (await session.scalars(select(ChatMessage).order_by(ChatMessage.id))).all()
        assert [row.kind for row in messages] == ["photo", "document", "voice", "voice"]
        assert messages[0].caption == "Фото с подписью"
        assert messages[0].reply_to_telegram_message_id == 98
        attachments = (await session.scalars(select(ChatAttachment).order_by(ChatAttachment.chat_message_id))).all()
        assert len(attachments) == 4
        assert attachments[0].telegram_file_id == "large-private-id"
        assert attachments[0].width == 1080 and attachments[0].height == 720

    headers = {"X-Telegram-Init-Data": admin_init_data}
    response = await client.get(f"/api/admin/conversations/{convs[0].id}/messages", headers=headers)
    assert response.status_code == 200, response.text
    rows = response.json()["items"]
    assert [row["kind"] for row in rows] == ["photo", "document", "voice", "voice"]
    assert rows[0]["attachment"] == {
        "kind": "photo", "filename": None, "size": 120000, "mime_type": "image/jpeg",
        "duration": None, "width": 1080, "height": 720, "available": True,
    }
    assert rows[0]["caption"] == "Фото с подписью"
    assert rows[2]["attachment"]["duration"] == 7
    listed = await client.get("/api/admin/conversations", headers=headers)
    assert listed.json()["items"][0]["last_message"]["kind"] == "voice"
    raw = json.dumps(response.json()) + json.dumps(listed.json())
    assert "private-id" not in raw and "private-token" not in raw
    assert "api.telegram.org" not in raw and "Фото с подписью" not in caplog.text


@pytest.mark.asyncio
async def test_media_does_not_enter_chat_during_payment_fsm(session_factory, settings):
    await _registered_user(session_factory)
    dispatcher = create_bot_dispatcher(session_factory, settings)
    bot = Bot("123:abc", session=MockTelegramSession())
    async def incoming_text(value, message_id):
        return Update.model_validate({
            "update_id": message_id,
            "message": {
                "message_id": message_id, "date": int(datetime.now(UTC).timestamp()),
                "chat": {"id": 411, "type": "private"},
                "from": {"id": 411, "first_name": "Test", "is_bot": False},
                "text": value,
            },
        }, context={"bot": bot})
    await dispatcher.feed_update(bot, await incoming_text("/start", 101))
    await dispatcher.feed_update(bot, await incoming_text(CHANGE_PAYMENT_DETAILS_BUTTON_TEXT, 102))
    await dispatcher.feed_update(bot, _media_update(bot, message_id=103, kind="document"))
    await dispatcher.feed_update(bot, _media_update(bot, message_id=104, kind="photo"))
    async with session_factory() as session:
        assert (await session.scalars(select(ChatMessage))).all() == []
        assert (await session.scalars(select(ChatAttachment))).all() == []


@pytest.mark.asyncio
async def test_media_unknown_user_groups_and_disabled_entrypoint_are_ignored(session_factory, settings):
    await _registered_user(session_factory)
    bot = Bot("123:abc", session=MockTelegramSession())
    dispatcher = create_bot_dispatcher(session_factory, settings)
    await dispatcher.feed_update(bot, _media_update(bot, message_id=300, user_id=999))
    await dispatcher.feed_update(bot, _media_update(bot, message_id=301, chat_type="supergroup"))
    await dispatcher.feed_update(bot, _media_update(bot, message_id=302, caption="/start"))
    hidden = create_bot_dispatcher(session_factory, settings.model_copy(update={"chat_inbox_enabled": False}))
    await hidden.feed_update(bot, _media_update(bot, message_id=303))
    async with session_factory() as session:
        assert (await session.scalars(select(ChatMessage))).all() == []


@pytest.mark.asyncio
async def test_download_enforces_auth_revocation_and_safe_headers(
    session_factory, app, client: AsyncClient, admin_init_data, dynamic_admin_init_data, settings
):
    user_id = await _registered_user(session_factory)
    async with session_factory() as session:
        session.add(AdminGrant(
            telegram_id=555, granted_by_telegram_id=123, granted_at=datetime.now(UTC),
        ))
        await session.flush()
        conversation = Conversation(user_id=user_id)
        session.add(conversation)
        await session.flush()
        message = ChatMessage(conversation_id=conversation.id, direction="incoming", kind="document")
        session.add(message)
        await session.flush()
        session.add(ChatAttachment(
            chat_message_id=message.id, kind="document", telegram_file_id="never-public-id",
            telegram_file_unique_id="unique", filename="../../evil\".pdf\r\nSet-Cookie: x",
            size=24, mime_type="application/pdf", available=True,
        ))
        await session.commit()
        message_id = message.id

    fake = MockBot()
    app.state.bot = fake
    path = f"/api/admin/chat-messages/{message_id}/attachment"
    assert (await client.get(path)).status_code == 401
    assert (await client.get(path, headers={"X-Telegram-Init-Data": "invalid"})).status_code == 401
    assert (await client.get(path, headers={"X-Telegram-Init-Data": "auth_date=0"})).status_code == 401
    assert (await client.get(path, headers={"X-Telegram-Init-Data": dynamic_admin_init_data})).status_code == 200
    assert fake.get_file_calls == 1

    response = await client.get(path, headers={"X-Telegram-Init-Data": admin_init_data})
    assert response.status_code == 200
    assert response.content == b"%PDF-1.7\nprivate content"
    assert response.headers["content-type"] == "application/octet-stream"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["content-disposition"].startswith("attachment;")
    assert "\r" not in response.headers["content-disposition"]
    assert "\n" not in response.headers["content-disposition"]
    assert "Set-Cookie" not in response.headers
    assert "private-token" not in json.dumps(dict(response.headers))
    assert "api.telegram.org" not in response.text
    assert fake.session.requested_url.endswith("documents/sample.pdf")
    assert "private-token" in fake.session.requested_url

    async with session_factory() as session:
        grant = await session.scalar(select(AdminGrant).where(AdminGrant.telegram_id == 555))
        grant.revoked_at = datetime.now(UTC)
        await session.commit()
    previous_calls = fake.get_file_calls
    assert (await client.get(path, headers={
        "X-Telegram-Init-Data": dynamic_admin_init_data
    })).status_code == 403
    assert fake.get_file_calls == previous_calls
    assert (await client.get("/api/admin/chat-messages/999999/attachment", headers={
        "X-Telegram-Init-Data": admin_init_data
    })).status_code == 404


@pytest.mark.asyncio
async def test_download_failure_size_and_unsupported_format_do_not_break_history(
    session_factory, app, client: AsyncClient, admin_init_data
):
    user_id = await _registered_user(session_factory)
    async with session_factory() as session:
        conv = Conversation(user_id=user_id)
        session.add(conv)
        await session.flush()
        rows = []
        for kind, mime, size in [
            ("document", "application/pdf", 25),
            ("document", "image/svg+xml", 25),
            ("voice", "audio/ogg", 20_000_001),
            ("voice", "audio/ogg", None),
        ]:
            msg = ChatMessage(conversation_id=conv.id, direction="incoming", kind=kind)
            session.add(msg)
            await session.flush()
            session.add(ChatAttachment(
                chat_message_id=msg.id, kind=kind, telegram_file_id="unavailable-private",
                telegram_file_unique_id="unique", filename="a.svg" if "svg" in mime else "a.pdf",
                size=size, mime_type=mime, available=True,
            ))
            rows.append(msg.id)
        await session.commit()
        conv_id = conv.id

    fake = MockBot()
    app.state.bot = fake
    headers = {"X-Telegram-Init-Data": admin_init_data}
    base = "/api/admin/chat-messages"
    assert (await client.get(f"{base}/{rows[1]}/attachment", headers=headers)).status_code == 415
    assert (await client.get(f"{base}/{rows[2]}/attachment", headers=headers)).status_code == 413
    fake.unavailable = True
    assert (await client.get(f"{base}/{rows[0]}/attachment", headers=headers)).status_code == 404
    fake.unavailable = False
    assert (await client.get(f"{base}/{rows[0]}/attachment", headers=headers)).status_code == 404
    fake.session.downloads = [b"abc" * 7_000_000]
    fake.file_size = None
    assert (await client.get(f"{base}/{rows[3]}/attachment", headers=headers)).status_code == 413

    history = await client.get(f"/api/admin/conversations/{conv_id}/messages", headers=headers)
    assert history.status_code == 200
    assert [msg["id"] for msg in history.json()["items"]] == rows
    assert history.json()["items"][0]["attachment"]["available"] is False
    assert all("telegram_file_id" not in json.dumps(x) for x in history.json()["items"])

