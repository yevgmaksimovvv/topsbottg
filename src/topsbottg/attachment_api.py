"""Authorized Telegram file proxy with bounded downloads and no URL disclosure."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from tempfile import SpooledTemporaryFile
from typing import BinaryIO
from urllib.parse import quote

from aiogram.exceptions import TelegramBadRequest
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from topsbottg.attachments import MAX_ATTACHMENT_BYTES, allowed_type
from topsbottg.config import Settings
from topsbottg.db import get_session
from topsbottg.models import ChatAttachment, ChatMessage

CHUNK_SIZE = 65536


def _filename(value: str | None, kind: str) -> str:
    fallback = {"photo": "photo.jpg", "voice": "voice.ogg", "document": "document"}[kind]
    clean = (value or "").replace("\\", "/").split("/")[-1]
    clean = "".join(ch for ch in clean if ord(ch) >= 32 and ord(ch) != 127).strip(" .")
    return clean[:180] or fallback


async def _download(bot, attachment: ChatAttachment) -> tuple[BinaryIO, int]:
    try:
        info = await bot.get_file(attachment.telegram_file_id)
    except TelegramBadRequest as exc:
        raise HTTPException(status_code=404, detail="attachment unavailable") from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail="attachment unavailable") from exc

    path = info.file_path
    if not isinstance(path, str) or not path or path.startswith("/") or "://" in path or ".." in path.split("/"):
        raise HTTPException(status_code=502, detail="attachment unavailable")
    if info.file_size is not None and info.file_size > MAX_ATTACHMENT_BYTES:
        raise HTTPException(status_code=413, detail="attachment too large")

    # Download into a bounded spool before sending headers, so truncated files and
    # oversized streams return an error rather than an apparently successful 200.
    buffer = SpooledTemporaryFile(max_size=1024 * 1024, mode="w+b")  # noqa: SIM115
    total = 0
    try:
        url = bot.session.api.file_url(bot.token, path)
        async for chunk in bot.session.stream_content(url, timeout=30, chunk_size=CHUNK_SIZE):
            total += len(chunk)
            if total > MAX_ATTACHMENT_BYTES:
                raise HTTPException(status_code=413, detail="attachment too large")
            buffer.write(chunk)
        buffer.seek(0)
        return buffer, total
    except HTTPException:
        buffer.close()
        raise
    except Exception as exc:
        buffer.close()
        raise HTTPException(status_code=502, detail="attachment unavailable") from exc


def register_attachment_routes(
    app: FastAPI,
    settings: Settings,
    require_admin: Callable[[str | None, Settings, AsyncSession], Awaitable[int]],
) -> None:
    @app.get("/api/admin/chat-messages/{message_id}/attachment")
    async def download_chat_attachment(
        message_id: int,
        request: Request,
        session: AsyncSession = Depends(get_session),
        x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
    ) -> StreamingResponse:
        await require_admin(x_telegram_init_data, settings, session)
        if message_id <= 0:
            raise HTTPException(status_code=404, detail="attachment not found")
        found = await session.execute(
            select(ChatAttachment)
            .join(ChatMessage, ChatMessage.id == ChatAttachment.chat_message_id)
            .where(ChatMessage.id == message_id)
        )
        attachment = found.scalar_one_or_none()
        if attachment is None:
            raise HTTPException(status_code=404, detail="attachment not found")
        if not allowed_type(attachment.kind, attachment.mime_type):
            raise HTTPException(status_code=415, detail="unsupported attachment type")
        if attachment.size is not None and attachment.size > MAX_ATTACHMENT_BYTES:
            raise HTTPException(status_code=413, detail="attachment too large")
        if not attachment.available:
            raise HTTPException(status_code=404, detail="attachment unavailable")

        bot = getattr(request.app.state, "bot", None)
        if bot is None:
            raise HTTPException(status_code=503, detail="attachment service unavailable")
        try:
            buffer, length = await _download(bot, attachment)
        except HTTPException as exc:
            if exc.status_code == 404:
                attachment.available = False
                await session.commit()
            raise

        async def chunks():
            try:
                while block := buffer.read(CHUNK_SIZE):
                    yield block
            finally:
                buffer.close()

        content_type = (
            "image/jpeg" if attachment.kind == "photo"
            else "audio/ogg" if attachment.kind == "voice"
            else "application/octet-stream"
        )
        filename = quote(_filename(attachment.filename, attachment.kind), safe="")
        return StreamingResponse(
            chunks(),
            media_type=content_type,
            headers={
                "Content-Disposition": f"attachment; filename*=UTF-8''{filename}",
                "Cache-Control": "private, no-store",
                "Pragma": "no-cache",
                "Content-Security-Policy": "sandbox",
                "X-Content-Type-Options": "nosniff",
                "Content-Length": str(length),
            },
        )
