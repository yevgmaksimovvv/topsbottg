"""Cross-component chat rollout tests on PostgreSQL with no Telegram network calls."""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramRetryAfter
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import AnswerCallbackQuery, GetFile, SendDocument, SendMessage, SendPhoto, SendVoice
from aiogram.types import File, Message, Update
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from topsbottg.api import create_app
from topsbottg.attachment_staging import staging_path
from topsbottg.bot import (
    CHANGE_GROUP_BUTTON_TEXT,
    CHANGE_NAME_BUTTON_TEXT,
    CHANGE_PAYMENT_DETAILS_BUTTON_TEXT,
    CHAT_ENTRYPOINT_TEXT,
    MY_DATA_BUTTON_TEXT,
    build_router,
)
from topsbottg.chat_outbox import claim_chat_outbox, settle_chat_outbox
from topsbottg.main import create_bot_dispatcher
from topsbottg.models import (
    AdminGrant,
    Base,
    BotFSMState,
    BotSensitiveMessage,
    ChatAttachment,
    ChatMessage,
    ChatMessageOutbox,
    Conversation,
    ConversationRead,
    PaymentProfile,
    Payout,
    PayoutRecipient,
    PayoutStatus,
    RecipientStatus,
    User,
)
from topsbottg.services import (
    add_recipients,
    create_payout,
    get_admin_events_broadcaster,
    get_or_create_user,
)
from topsbottg.worker import deliver_chat_outbox

PDF = b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\n%%EOF\n"
JPEG = b"\xff\xd8\xff\xe0" + b"0" * 32 + b"\xff\xd9"
VOICE = b"OggS" + b"\x00" * 28 + b"OpusHead" + b"\x00" * 40
TOKEN = "123:FAKE-NO-REAL-TELEGRAM-API"


class FakeTelegramSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.api = SimpleNamespace(file_url=lambda token, path: f"https://fake.invalid/{path}")
        self.methods = []
        self.next_id = 5000
        self.fail_next_send: Exception | None = None
        self.download_calls = []
        self.get_file_calls = []

    async def make_request(self, bot: Bot, method, timeout=None):
        self.methods.append(method)
        if isinstance(method, GetFile):
            self.get_file_calls.append(method.file_id)
            return File(
                file_id=method.file_id, file_unique_id="unique-sandbox",
                file_path="documents/report.pdf", file_size=len(PDF),
            )
        if isinstance(method, AnswerCallbackQuery):
            return True
        if isinstance(method, SendMessage) and self.fail_next_send is not None:
            error, self.fail_next_send = self.fail_next_send, None
            raise error

        self.next_id += 1
        payload = {
            "message_id": self.next_id,
            "date": int(datetime.now(UTC).timestamp()),
            "chat": {"id": int(getattr(method, "chat_id", 0)), "type": "private"},
            "from": {"id": 9000, "is_bot": True, "first_name": "Sandbox bot"},
        }
        if isinstance(method, SendMessage):
            payload["text"] = method.text
        elif isinstance(method, SendPhoto):
            assert Path(method.photo.path).is_file()
            payload["photo"] = [{
                "file_id": f"out-photo-{self.next_id}",
                "file_unique_id": f"unique-{self.next_id}",
                "width": 100, "height": 100, "file_size": 100,
            }]
        elif isinstance(method, SendDocument):
            assert Path(method.document.path).is_file()
            payload["document"] = {
                "file_id": f"out-document-{self.next_id}",
                "file_unique_id": f"unique-{self.next_id}",
                "file_name": "report.pdf",
                "mime_type": "application/pdf", "file_size": len(PDF),
            }
        elif isinstance(method, SendVoice):
            assert Path(method.voice.path).is_file()
            payload["voice"] = {
                "file_id": f"out-voice-{self.next_id}",
                "file_unique_id": f"unique-{self.next_id}",
                "duration": 3, "mime_type": "audio/ogg", "file_size": len(VOICE),
            }
        return Message.model_validate(payload, context={"bot": bot})

    async def stream_content(self, url, *args, **kwargs):
        self.download_calls.append(url)
        for fragment in (PDF[:9], PDF[9:]):
            yield fragment

    async def close(self):
        return None


@dataclass
class ChatStack:
    factory: async_sessionmaker
    settings: object
    app: object
    client: AsyncClient
    bot: Bot
    telegram: FakeTelegramSession

    def dispatcher(self, *, inbox: bool = True, entrypoint: bool = True):
        effective = self.settings.model_copy(update={
            "chat_inbox_enabled": inbox, "chat_entrypoint_enabled": entrypoint,
        })
        return create_bot_dispatcher(self.factory, effective)


@pytest.fixture
async def chat_stack(settings):
    url = os.getenv("POSTGRES_TEST_URL")
    if not url:
        pytest.skip("POSTGRES_TEST_URL required")
    schema = f"ci_chat_rollout_{uuid4().hex}"
    admin_engine = create_async_engine(url)
    engine = None
    bot = None
    try:
        async with admin_engine.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_async_engine(
            url, connect_args={"server_settings": {"search_path": schema}},
        )
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
        enabled = settings.model_copy(update={
            "database_url": url, "chat_inbox_enabled": True, "chat_entrypoint_enabled": True,
        })
        app = create_app(enabled, factory)
        fake = FakeTelegramSession()
        bot = Bot(TOKEN, session=fake)
        app.state.bot = bot
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            yield ChatStack(factory, enabled, app, client, bot, fake)
    finally:
        if bot is not None:
            await bot.session.close()
        if engine is not None:
            await engine.dispose()
        async with admin_engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await admin_engine.dispose()


async def register(stack: ChatStack, telegram_id: int) -> int:
    async with stack.factory() as session:
        user = await get_or_create_user(session, telegram_id, "Пользователь для чата")
        await session.commit()
        return user.id


async def grant(stack: ChatStack, telegram_id=555) -> None:
    async with stack.factory() as session:
        session.add(AdminGrant(
            telegram_id=telegram_id, granted_by_telegram_id=123,
            granted_at=datetime.now(UTC),
        ))
        await session.commit()


def headers(init_data: str, key: str | None = None) -> dict[str, str]:
    result = {"X-Telegram-Init-Data": init_data}
    if key is not None:
        result["Idempotency-Key"] = key
    return result


def update(
    bot: Bot, telegram_id: int, text_value: str | None, number: int,
    *, kind: str = "text", chat_type: str = "private", reply_to: int | None = None,
) -> Update:
    message = {
        "message_id": number,
        "date": int(datetime.now(UTC).timestamp()),
        "chat": {
            "id": telegram_id if chat_type == "private" else -100999,
            "type": chat_type,
        },
        "from": {"id": telegram_id, "is_bot": False, "first_name": "Sandbox"},
    }
    if kind == "text":
        message["text"] = text_value
    elif kind == "photo":
        message["photo"] = [
            {"file_id": "in-photo-low", "file_unique_id": "photo-low",
             "width": 20, "height": 20, "file_size": 20},
            {"file_id": "in-photo-hi", "file_unique_id": "photo-hi",
             "width": 100, "height": 100, "file_size": 150},
        ]
    elif kind == "document":
        message["document"] = {
            "file_id": "in-private-pdf", "file_unique_id": "private-pdf",
            "file_name": "report.pdf", "mime_type": "application/pdf", "file_size": len(PDF),
        }
    elif kind == "voice":
        message["voice"] = {
            "file_id": "in-private-voice", "file_unique_id": "voice-unique",
            "mime_type": "audio/ogg", "duration": 3, "file_size": len(VOICE),
        }
    if kind != "text" and text_value is not None:
        message["caption"] = text_value
    if reply_to is not None:
        message["reply_to_message"] = {
            "message_id": reply_to, "date": message["date"],
            "chat": message["chat"], "text": "Ранее",
        }
    return Update.model_validate(
        {"update_id": number, "message": message}, context={"bot": bot},
    )


def message_methods(stack: ChatStack, name: type = SendMessage) -> list:
    return [method for method in stack.telegram.methods if isinstance(method, name)]


async def feed(stack: ChatStack, dispatcher: Dispatcher, telegram_id: int, text_value: str | None,
               number: int, **kwargs) -> None:
    await dispatcher.feed_update(stack.bot, update(stack.bot, telegram_id, text_value, number, **kwargs))


@pytest.mark.postgres
async def test_pg_end_to_end_text_dispatcher_api_read_markers_and_worker_restart(
    chat_stack, admin_init_data, dynamic_admin_init_data,
):
    stack = chat_stack
    user_id = await register(stack, 71001)
    await grant(stack)
    events = get_admin_events_broadcaster().subscribe()
    try:
        first = stack.dispatcher()
        await feed(stack, first, 71001, "/start", 1001)
        async with stack.factory() as session:
            assert (await session.scalar(select(BotFSMState))).inbox_trusted_after_start is True
        await feed(stack, first, 71001, "Первый вопрос", 1002)
        assert events.get_nowait()["event"] == "chat.changed"
        assert events.empty()

        conversations = await stack.client.get(
            "/api/admin/conversations", headers=headers(admin_init_data),
        )
        assert conversations.status_code == 200, conversations.text
        conversations_data = conversations.json()
        assert len(conversations_data["items"]) == 1
        cid = conversations_data["items"][0]["id"]
        assert conversations_data["items"][0]["user"]["id"] == user_id
        assert conversations_data["items"][0]["unread_count"] == 1
        history_path = f"/api/admin/conversations/{cid}/messages"
        first_history = (await stack.client.get(history_path, headers=headers(admin_init_data))).json()
        assert len(first_history["items"]) == 1
        received_id = first_history["items"][0]["id"]
        assert first_history["items"][0]["text"] == "Первый вопрос"
        assert (await stack.client.get(
            f"/api/admin/conversations/{cid}/read",
            headers=headers(admin_init_data),
        )).status_code == 405

        own_read = await stack.client.post(
            f"/api/admin/conversations/{cid}/read",
            json={"last_read_message_id": received_id},
            headers=headers(admin_init_data),
        )
        assert own_read.status_code == 200 and own_read.json()["unread_count"] == 0
        other_summary = await stack.client.get(
            "/api/admin/conversations/summary", headers=headers(dynamic_admin_init_data),
        )
        assert other_summary.status_code == 200
        assert other_summary.json()["unread_count"] == 1
        other_read = await stack.client.post(
            f"/api/admin/conversations/{cid}/read",
            json={"last_read_message_id": received_id},
            headers=headers(dynamic_admin_init_data),
        )
        assert other_read.status_code == 200
        assert other_read.json()["unread_count"] == 0

        # Recreating the dispatcher must preserve persisted trust and read history.
        restarted = stack.dispatcher()
        await feed(stack, restarted, 71001, "Второй вопрос", 1003)
        await feed(stack, restarted, 71001, "Второй вопрос", 1003)
        assert events.get_nowait()["event"] == "chat.changed"
        assert events.empty()
        other_unread = await stack.client.get(
            "/api/admin/conversations/summary", headers=headers(dynamic_admin_init_data),
        )
        assert other_unread.json()["unread_count"] == 1
        assert (await stack.client.get(
            "/api/admin/conversations/summary", headers=headers(admin_init_data),
        )).json()["unread_count"] == 1

        path = f"/api/admin/conversations/{cid}/messages"
        post = await stack.client.post(
            path, json={"text": "Ответ администратора", "reply_to_message_id": received_id},
            headers=headers(admin_init_data, "outgoing-once"),
        )
        assert post.status_code == 202, post.text
        outgoing_id = post.json()["message"]["id"]
        assert post.json()["message"]["delivery_status"] == "queued"
        repeated = await stack.client.post(
            path, json={"text": "Ответ администратора", "reply_to_message_id": received_id},
            headers=headers(admin_init_data, "outgoing-once"),
        )
        assert repeated.status_code == 202 and repeated.json()["message"]["id"] == outgoing_id
        conflict = await stack.client.post(
            path, json={"text": "Другое содержание", "reply_to_message_id": received_id},
            headers=headers(admin_init_data, "outgoing-once"),
        )
        assert conflict.status_code == 409
        await deliver_chat_outbox(stack.bot, stack.factory, stack.settings.chat_staging_dir)
        reply = message_methods(stack)[-1]
        assert reply.chat_id == 71001
        assert reply.text == "Ответ администратора"
        assert reply.reply_parameters.message_id == 1002
        await feed(stack, stack.dispatcher(), 71001, "Спасибо", 1004, reply_to=stack.telegram.next_id)
        messages = (await stack.client.get(path, headers=headers(dynamic_admin_init_data))).json()["items"]
        assert [m["direction"] for m in messages] == ["incoming", "incoming", "outgoing", "incoming"]
        assert messages[2]["id"] == outgoing_id and messages[2]["delivery_status"] == "sent"
        assert messages[3]["source"] == "telegram_reply"
        assert messages[3]["reply_to_telegram_message_id"] == stack.telegram.next_id
        async with stack.factory() as session:
            assert await session.scalar(select(func.count(Conversation.id))) == 1
            assert await session.scalar(select(func.count(ChatMessage.id))) == 4
            assert await session.scalar(select(func.count(ChatMessageOutbox.id))) == 1
            assert await session.scalar(select(func.count(ConversationRead.id))) == 2
    finally:
        get_admin_events_broadcaster().unsubscribe(events)


@pytest.mark.postgres
async def test_pg_media_history_download_and_upload_idempotency(
    chat_stack, admin_init_data, dynamic_admin_init_data,
):
    stack = chat_stack
    await register(stack, 71002)
    await grant(stack)
    dispatcher = stack.dispatcher()
    await feed(stack, dispatcher, 71002, "/start", 2001)
    for i, kind in enumerate(("photo", "document", "voice"), start=2002):
        await feed(stack, dispatcher, 71002, f"{kind} caption", i, kind=kind)
    async with stack.factory() as session:
        messages = (await session.scalars(select(ChatMessage).order_by(ChatMessage.id))).all()
        assert len(messages) == 3
        assert [m.kind for m in messages] == ["photo", "document", "voice"]
        assert all(m.conversation_id == messages[0].conversation_id for m in messages)
        cid = messages[0].conversation_id
        document_id = messages[1].id
        assert messages[1].attachment.telegram_file_id == "in-private-pdf"

    history_path = f"/api/admin/conversations/{cid}/messages"
    history = await stack.client.get(history_path, headers=headers(admin_init_data))
    assert history.status_code == 200
    assert [m["kind"] for m in history.json()["items"]] == ["photo", "document", "voice"]
    assert "in-private-pdf" not in history.text
    assert TOKEN not in history.text
    download = f"/api/admin/chat-messages/{document_id}/attachment"
    assert (await stack.client.get(download)).status_code == 401
    assert (await stack.client.get(download, headers=headers(dynamic_admin_init_data))).status_code == 200
    received = await stack.client.get(download, headers=headers(admin_init_data))
    assert received.status_code == 200 and received.content == PDF
    assert received.headers["cache-control"] == "no-store"
    assert received.headers["x-content-type-options"] == "nosniff"
    assert received.headers["content-type"] == "application/octet-stream"
    assert TOKEN not in str(received.headers)
    assert "fake.invalid" not in received.text
    assert stack.telegram.get_file_calls == ["in-private-pdf", "in-private-pdf"]

    uploads = [
        ("photo", "photo.jpg", JPEG, "image/jpeg"),
        ("document", "report.pdf", PDF, "application/pdf"),
        ("voice", "recording.ogg", VOICE, "audio/ogg"),
    ]
    outbound_ids = []
    for kind, filename, content, mime in uploads:
        key = f"upload-{kind}"
        route = f"/api/admin/conversations/{cid}/attachments"
        form = {"kind": kind, "caption": f"Reply {kind}"}
        files = {"file": (filename, content, mime)}
        response = await stack.client.post(
            route, data=form, files=files, headers=headers(admin_init_data, key),
        )
        assert response.status_code == 202, response.text
        message_id = response.json()["message"]["id"]
        outbound_ids.append(message_id)
        repeat = await stack.client.post(
            route, data=form, files=files, headers=headers(admin_init_data, key),
        )
        assert repeat.status_code == 202
        assert repeat.json()["message"]["id"] == message_id
        await deliver_chat_outbox(stack.bot, stack.factory, stack.settings.chat_staging_dir)

    assert len(message_methods(stack, SendPhoto)) == 1
    assert len(message_methods(stack, SendDocument)) == 1
    assert len(message_methods(stack, SendVoice)) == 1
    after = (await stack.client.get(history_path, headers=headers(admin_init_data))).json()
    assert len(after["items"]) == 6
    assert [m["delivery_status"] for m in after["items"][-3:]] == ["sent"] * 3
    assert "out-document-" not in str(after)
    async with stack.factory() as session:
        assert await session.scalar(select(func.count(ChatMessageOutbox.id))) == 3
        assert await session.scalar(select(func.count(ChatMessage.id))) == 6
        for mid in outbound_ids:
            attachment = await session.get(ChatAttachment, mid)
            assert attachment.available and attachment.staging_key is None
            assert attachment.telegram_file_id.startswith("out-")
    async with stack.factory() as session:
        grant_row = await session.scalar(select(AdminGrant).where(AdminGrant.telegram_id == 555))
        grant_row.revoked_at = datetime.now(UTC)
        await session.commit()
    assert (await stack.client.get(history_path, headers=headers(dynamic_admin_init_data))).status_code == 403
    previous_downloads = len(stack.telegram.get_file_calls)
    assert (await stack.client.get(download, headers=headers(dynamic_admin_init_data))).status_code == 403
    assert len(stack.telegram.get_file_calls) == previous_downloads
    assert (await stack.client.post(
        f"/api/admin/conversations/{cid}/messages",
        json={"text": "Нельзя отправлять"},
        headers=headers(dynamic_admin_init_data, "revoked"),
    )).status_code == 403
    assert (await stack.client.post(
        f"/api/admin/conversations/{cid}/attachments",
        data={"kind": "document"},
        files={"file": ("report.pdf", PDF, "application/pdf")},
        headers=headers(dynamic_admin_init_data, "revoked-upload"),
    )).status_code == 403
    assert (await stack.client.get(download, headers=headers(admin_init_data))).status_code == 200
