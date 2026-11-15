"""Incoming Telegram media metadata and bounded attachment policies."""

from __future__ import annotations

from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from topsbottg.conversations import record_incoming_message
from topsbottg.models import ChatAttachment, ChatMessage

# Telegram's standard getFile download limit is 20 MB.
MAX_ATTACHMENT_BYTES = 20_000_000

DOCUMENT_MIME_TYPES = frozenset({
    "application/pdf",
    "application/zip",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "text/plain",
    "image/jpeg",
    "image/png",
})


def allowed_type(kind: str, mime_type: str | None) -> bool:
    mime = (mime_type or "").split(";", 1)[0].strip().lower()
    if kind == "photo":
        return mime == "image/jpeg"
    if kind == "voice":
        return mime == "audio/ogg"
    return kind == "document" and mime in DOCUMENT_MIME_TYPES


def attachment_public_data(attachment: ChatAttachment | None) -> dict | None:
    if attachment is None:
        return None
    return {
        "kind": attachment.kind,
        "filename": attachment.filename,
        "size": attachment.size,
        "mime_type": attachment.mime_type,
        "duration": attachment.duration,
        "width": attachment.width,
        "height": attachment.height,
        "available": attachment.available,
    }


def attachment_fields(message: Message) -> dict | None:
    if message.photo:
        # Telegram supplies multiple renditions of one photo; store only one.
        photo = max(message.photo, key=lambda x: (x.file_size or 0, x.width * x.height))
        return {
            "kind": "photo",
            "telegram_file_id": photo.file_id,
            "telegram_file_unique_id": photo.file_unique_id,
            "filename": None,
            "size": photo.file_size,
            "mime_type": "image/jpeg",
            "width": photo.width,
            "height": photo.height,
            "duration": None,
        }
    if message.document:
        document = message.document
        return {
            "kind": "document",
            "telegram_file_id": document.file_id,
            "telegram_file_unique_id": document.file_unique_id,
            "filename": document.file_name,
            "size": document.file_size,
            "mime_type": document.mime_type,
            "width": None,
            "height": None,
            "duration": None,
        }
    if message.voice:
        voice = message.voice
        return {
            "kind": "voice",
            "telegram_file_id": voice.file_id,
            "telegram_file_unique_id": voice.file_unique_id,
            "filename": None,
            "size": voice.file_size,
            "mime_type": voice.mime_type or "audio/ogg",
            "width": None,
            "height": None,
            "duration": voice.duration,
        }
    return None


async def record_incoming_attachment(
    session: AsyncSession, *, conversation_id: int, message: Message,
) -> tuple[ChatMessage, bool]:
    fields = attachment_fields(message)
    if fields is None:
        raise ValueError("No supported Telegram attachment in message")
    stored, created = await record_incoming_message(
        session,
        conversation_id=conversation_id,
        telegram_chat_id=message.chat.id,
        telegram_message_id=message.message_id,
        text=None,
        kind=fields["kind"],
        reply_to_telegram_message_id=(
            message.reply_to_message.message_id if message.reply_to_message is not None else None
        ),
        source="telegram_reply" if message.reply_to_message is not None else "telegram",
    )
    if created:
        stored.caption = message.caption
        session.add(ChatAttachment(
            chat_message_id=stored.id,
            **fields,
            available=(
                allowed_type(fields["kind"], fields["mime_type"])
                and (fields["size"] is None or fields["size"] <= MAX_ATTACHMENT_BYTES)
            ),
        ))
        await session.flush()
    return stored, created
