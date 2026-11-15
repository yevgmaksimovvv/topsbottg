"""Admin uploads: validation, idempotency and durable media delivery."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from topsbottg.attachment_staging import (
    ORPHAN_TTL,
    cleanup_staging,
    staging_path,
)
from topsbottg.conversations import record_incoming_message
from topsbottg.models import (
    AdminGrant,
    ChatAttachment,
    ChatMessage,
    ChatMessageOutbox,
    Conversation,
    Payout,
    User,
)
from topsbottg.worker import deliver_chat_outbox

PDF = b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\n%%EOF\n"
JPEG = b"\xff\xd8\xff\xe0" + b"0" * 32 + b"\xff\xd9"
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 40
VOICE = b"OggS" + b"\x00" * 28 + b"OpusHead" + b"\x00" * 40


async def _conversation(factory, telegram_id=84501):
    async with factory() as session:
        user = User(
            telegram_id=telegram_id, full_name="Получатель",
            registration_completed_at=datetime.now(UTC),
        )
        session.add(user)
        await session.flush()
        conversation = Conversation(user_id=user.id)
        session.add(conversation)
        await session.commit()
        return conversation.id, telegram_id


class FakeMediaBot:
    def __init__(self):
        self.calls = []
        self.next_id = 1000

    async def _send(self, kind, chat_id, media, **kwargs):
        assert Path(media.path).is_file()
        self.calls.append((kind, chat_id, media.filename, kwargs))
        self.next_id += 1
        file = SimpleNamespace(file_id=f"private-file-{self.next_id}", file_unique_id=f"unique-{self.next_id}")
        return SimpleNamespace(
            message_id=self.next_id,
            photo=[file] if kind == "photo" else [],
            document=file if kind == "document" else None,
            voice=file if kind == "voice" else None,
        )

    async def send_photo(self, chat_id, photo, **kwargs):
        return await self._send("photo", chat_id, photo, **kwargs)

    async def send_document(self, chat_id, document, **kwargs):
        return await self._send("document", chat_id, document, **kwargs)

    async def send_voice(self, chat_id, voice, **kwargs):
        return await self._send("voice", chat_id, voice, **kwargs)


@pytest.mark.asyncio
async def test_document_upload_reply_and_worker_cleanup(client, settings, session_factory, admin_init_data):
    cid, telegram_id = await _conversation(session_factory)
    async with session_factory() as session:
        reply, _ = await record_incoming_message(
            session, conversation_id=cid, telegram_chat_id=telegram_id,
            telegram_message_id=53, text="Вопрос",
        )
        await session.commit()
        reply_id = reply.id
        payout_count = await session.scalar(select(func.count(Payout.id)))

    url = f"/api/admin/conversations/{cid}/attachments"
    headers = {"X-Telegram-Init-Data": admin_init_data, "Idempotency-Key": "doc-key"}
    data = {"kind": "document", "caption": "Подпись", "reply_to_message_id": str(reply_id)}
    files = {"file": ("report.pdf", PDF, "application/pdf")}
    response = await client.post(url, headers=headers, data=data, files=files)
    assert response.status_code == 202, response.text
    outgoing = response.json()["message"]
    assert outgoing["direction"] == "outgoing"
    assert outgoing["kind"] == "document"
    assert outgoing["caption"] == "Подпись"
    assert outgoing["text"] is None
    assert outgoing["delivery_status"] == "queued"
    assert outgoing["attachment"] == {
        "kind": "document", "filename": "report.pdf", "size": len(PDF),
        "mime_type": "application/pdf", "duration": None,
        "width": None, "height": None, "available": False,
    }
    async with session_factory() as session:
        attachment = await session.get(ChatAttachment, outgoing["id"])
        assert attachment.staging_key is not None
        stage = staging_path(settings.chat_staging_dir, attachment.staging_key)
        assert stage.read_bytes() == PDF
        assert attachment.telegram_file_id is None

    # The filesystem path survives a new worker instance and database session.
    bot = FakeMediaBot()
    await deliver_chat_outbox(bot, session_factory, settings.chat_staging_dir)
    assert bot.calls == [("document", telegram_id, "report.pdf", {
        "caption": "Подпись",
        "reply_parameters": bot.calls[0][3]["reply_parameters"],
        "request_timeout": 300,
    })]
    assert bot.calls[0][3]["reply_parameters"].message_id == 53
    assert not stage.exists()
    async with session_factory() as session:
        attachment = await session.get(ChatAttachment, outgoing["id"])
        message = await session.get(ChatMessage, outgoing["id"])
        row = await session.scalar(select(ChatMessageOutbox))
        assert attachment.available and attachment.telegram_file_id == "private-file-1001"
        assert message.telegram_message_id == row.telegram_message_id == 1001
        assert row.status == "sent" and row.attempts == 1
        assert await session.scalar(select(func.count(Payout.id))) == payout_count
    history = await client.get(f"/api/admin/conversations/{cid}/messages", headers=headers)
    result = history.json()["items"][-1]
    assert result["delivery_status"] == "sent" and result["attachment"]["available"] is True
    assert "private-file-1001" not in history.text

    repeat = await client.post(url, headers=headers, data=data, files=files)
    assert repeat.status_code == 202
    assert repeat.json()["message"]["id"] == outgoing["id"]
    assert (await client.post(url, headers=headers, data=data, files={
        "file": ("report.pdf", PDF + b"modified", "application/pdf"),
    })).status_code == 409
    async with session_factory() as session:
        assert await session.scalar(select(func.count(ChatMessageOutbox.id))) == 1


@pytest.mark.asyncio
async def test_photo_voice_and_upload_retry(client, session_factory, settings, admin_init_data):
    cid, _ = await _conversation(session_factory)
    bot = FakeMediaBot()
    cases = [
        ("photo", "image.jpg", JPEG, "image/jpeg"),
        ("voice", "voice.ogg", VOICE, "audio/ogg"),
        ("photo", "image.png", PNG, "image/png"),
    ]
    for index, (kind, name, data, mime) in enumerate(cases):
        url = f"/api/admin/conversations/{cid}/attachments"
        headers = {"X-Telegram-Init-Data": admin_init_data, "Idempotency-Key": f"media-{index}"}
        response = await client.post(url, headers=headers, data={"kind": kind}, files={"file": (name, data, mime)})
        assert response.status_code == 202, response.text
        assert response.json()["message"]["kind"] == kind
        await deliver_chat_outbox(bot, session_factory, settings.chat_staging_dir)
        assert bot.calls[-1][0] == kind
    assert len(bot.calls) == 3
    async with session_factory() as session:
        assert await session.scalar(select(func.count(ChatMessage.id))) == 3
        assert await session.scalar(select(func.count(ChatMessageOutbox.id))) == 3
        assert await session.scalar(select(func.count(ChatAttachment.chat_message_id))) == 3


@pytest.mark.asyncio
async def test_upload_rejections_authorization_and_revoked_role(
    client, settings, session_factory, admin_init_data, non_admin_init_data, dynamic_admin_init_data
):
    cid, _ = await _conversation(session_factory)
    path = f"/api/admin/conversations/{cid}/attachments"
    headers = {"X-Telegram-Init-Data": admin_init_data, "Idempotency-Key": "file-a"}
    files = {"file": ("report.pdf", PDF, "application/pdf")}
    assert (await client.post(path, data={"kind": "document"}, files=files)).status_code == 401
    assert (await client.post(path, headers={
        "X-Telegram-Init-Data": non_admin_init_data
    }, data={"kind": "document"}, files=files)).status_code == 403
    assert (await client.post("/api/admin/conversations/999999/attachments",
        headers=headers, data={"kind": "document"}, files=files)).status_code == 404
    assert (await client.post(path, headers={
        "X-Telegram-Init-Data": admin_init_data
    }, data={"kind": "document"}, files=files)).status_code == 422
    cases = [
        ("document", "evil.pdf", b"<html>no</html>", "application/pdf", 415),
        ("document", "evil.svg", b"<svg></svg>", "image/svg+xml", 415),
        ("document", "evil.pdf", PDF, "text/html", 415),
        ("document", "fake.pdf", PDF, "application/zip", 415),
        ("voice", "invalid.ogg", b"OggSwithoutopus", "audio/ogg", 415),
        ("photo", "image.jpg", PDF, "image/jpeg", 415),
        ("document", "report.pdf", b"0" * (20 * 1024 * 1024 + 1), "application/pdf", 413),
    ]
    for kind, name, data, mime, status in cases:
        response = await client.post(path, headers=headers, data={"kind": kind}, files={
            "file": (name, data, mime),
        })
        assert response.status_code == status, (name, response.text)
    async with session_factory() as session:
        assert await session.scalar(select(func.count(ChatMessageOutbox.id))) == 0

    async with session_factory() as session:
        session.add(AdminGrant(telegram_id=555, granted_by_telegram_id=123, granted_at=datetime.now(UTC)))
        await session.commit()
    dynamic = {"X-Telegram-Init-Data": dynamic_admin_init_data, "Idempotency-Key": "dynamic"}
    assert (await client.post(path, headers=dynamic, data={"kind": "document"}, files=files)
            ).status_code == 202
    async with session_factory() as session:
        grant = await session.scalar(select(AdminGrant).where(AdminGrant.telegram_id == 555))
        grant.revoked_at = datetime.now(UTC)
        await session.commit()
    assert (await client.post(path, headers=dynamic, data={"kind": "document"}, files=files)
            ).status_code == 403


@pytest.mark.asyncio
async def test_cleanup_protects_queued_and_cleans_expired_orphans(
    client, session_factory, settings, admin_init_data
):
    cid, _ = await _conversation(session_factory)
    res = await client.post(
        f"/api/admin/conversations/{cid}/attachments",
        headers={"X-Telegram-Init-Data": admin_init_data, "Idempotency-Key": "keep"},
        data={"kind": "document"}, files={"file": ("report.pdf", PDF, "application/pdf")},
    )
    assert res.status_code == 202
    async with session_factory() as session:
        att = await session.get(ChatAttachment, res.json()["message"]["id"])
        path = staging_path(settings.chat_staging_dir, att.staging_key)
    old = datetime.now(UTC).timestamp() - ORPHAN_TTL.total_seconds() - 3600
    os.utime(path, (old, old))
    assert await cleanup_staging(settings.chat_staging_dir, session_factory) == 0
    assert path.is_file()

    orphan = staging_path(settings.chat_staging_dir, "a" * 48)
    orphan.write_bytes(b"old orphan")
    os.utime(orphan, (old, old))
    assert await cleanup_staging(settings.chat_staging_dir, session_factory) == 1
    assert not orphan.exists()
    assert path.exists()


@pytest.mark.asyncio
async def test_media_failure_removes_staging_and_keeps_failed_status(
    client, session_factory, settings, admin_init_data
):
    from aiogram.exceptions import TelegramForbiddenError
    from aiogram.methods import SendDocument

    cid, _ = await _conversation(session_factory)
    result = await client.post(
        f"/api/admin/conversations/{cid}/attachments",
        headers={"X-Telegram-Init-Data": admin_init_data, "Idempotency-Key": "blocked-media"},
        data={"kind": "document"},
        files={"file": ("report.pdf", PDF, "application/pdf")},
    )
    assert result.status_code == 202
    message_id = result.json()["message"]["id"]
    async with session_factory() as session:
        att = await session.get(ChatAttachment, message_id)
        path = staging_path(settings.chat_staging_dir, att.staging_key)
        assert path.exists()

    class BlockedBot:
        async def send_document(self, chat_id, document, **kwargs):
            raise TelegramForbiddenError(
                method=SendDocument(chat_id=chat_id, document="ignored"),
                message="Forbidden: bot was blocked by the user",
            )

    await deliver_chat_outbox(BlockedBot(), session_factory, settings.chat_staging_dir)
    assert not path.exists()
    async with session_factory() as session:
        att = await session.get(ChatAttachment, message_id)
        row = await session.scalar(select(ChatMessageOutbox))
        assert not att.available and att.telegram_file_id is None
        assert row.status == "failed" and row.error_code == "blocked"


@pytest.mark.asyncio
async def test_staging_quota_rejects_upload_without_database_or_file_leak(
    client, session_factory, settings, admin_init_data, monkeypatch
):
    import topsbottg.attachment_staging as staging

    cid, _ = await _conversation(session_factory)
    monkeypatch.setattr(staging, "STAGING_QUOTA_BYTES", 1)
    result = await client.post(
        f"/api/admin/conversations/{cid}/attachments",
        headers={"X-Telegram-Init-Data": admin_init_data, "Idempotency-Key": "quota"},
        data={"kind": "document"}, files={"file": ("report.pdf", PDF, "application/pdf")},
    )
    assert result.status_code == 507
    assert result.json()["detail"] == "staging_quota_exceeded"
    async with session_factory() as session:
        assert await session.scalar(select(func.count(ChatMessageOutbox.id))) == 0
        assert await session.scalar(select(func.count(ChatMessage.id))) == 0
    root = Path(settings.chat_staging_dir)
    assert all(len(p.name) != 48 for p in root.iterdir())
