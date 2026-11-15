"""Authenticated, read-only conversation API and per-admin read markers."""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Response
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from topsbottg.config import Settings
from topsbottg.conversations import (
    advance_read_marker,
    get_or_create_conversation_with_status,
    get_unread_count,
    list_conversation_messages,
)
from topsbottg.db import get_session
from topsbottg.models import ChatMessage, Conversation, ConversationRead, StudyGroup, User
from topsbottg.schemas import (
    ChatMessageOut,
    ChatMessagePageOut,
    ConversationCreateIn,
    ConversationItemOut,
    ConversationOpenOut,
    ConversationPageOut,
    ConversationReadIn,
    ConversationReadOut,
    ConversationSummaryOut,
    ConversationUserOut,
)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _message_out(message: ChatMessage) -> ChatMessageOut:
    return ChatMessageOut(
        id=message.id,
        conversation_id=message.conversation_id,
        direction=message.direction,
        kind=message.kind,
        text=message.text,
        caption=getattr(message, "caption", None),
        created_at=_utc(message.created_at),
        author_admin_telegram_id=message.author_admin_telegram_id,
        telegram_message_id=message.telegram_message_id,
        reply_to_telegram_message_id=message.reply_to_telegram_message_id,
        delivery_status=getattr(message, "delivery_status", None),
        delivery_error_code=getattr(message, "delivery_error_code", None),
        attachment=getattr(message, "attachment", None),
    )


def _encode_cursor(payload: dict[str, object]) -> str:
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(value: str) -> dict[str, object]:
    if not value or len(value) > 2048:
        raise HTTPException(status_code=422, detail="invalid cursor")
    try:
        raw = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError, binascii.Error) as exc:
        raise HTTPException(status_code=422, detail="invalid cursor") from exc
    if not isinstance(payload, dict) or payload.get("v") != 1:
        raise HTTPException(status_code=422, detail="invalid cursor")
    return payload


def _positive_int(value: object, *, allow_zero: bool = False) -> bool:
    return type(value) is int and (value >= 0 if allow_zero else value > 0)


def _conversation_cursor(
    value: str | None, *, query: str, study_group_id: int | None,
) -> tuple[int, int, int | None, datetime | None] | None:
    if value is None:
        return None
    payload = _decode_cursor(value)
    ceiling = payload.get("c")
    message_ceiling = payload.get("m")
    last_id = payload.get("id")
    last_time = payload.get("at")
    if (
        not _positive_int(ceiling)
        or not _positive_int(message_ceiling, allow_zero=True)
        or not _positive_int(last_id)
        or last_id > ceiling
        or payload.get("q") != query.casefold()
        or payload.get("g") != study_group_id
        or (last_time is not None and not isinstance(last_time, str))
    ):
        raise HTTPException(status_code=422, detail="invalid cursor")
    if last_time is None:
        stamp = None
    else:
        try:
            stamp = _utc(datetime.fromisoformat(last_time))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="invalid cursor") from exc
    return ceiling, message_ceiling, last_id, stamp


def _history_cursor(value: str | None, conversation_id: int) -> int | None:
    if value is None:
        return None
    payload = _decode_cursor(value)
    if (
        payload.get("conversation_id") != conversation_id
        or not _positive_int(payload.get("before_id"))
        or len(payload) != 3
    ):
        raise HTTPException(status_code=422, detail="invalid cursor")
    return payload["before_id"]


async def _conversation_or_404(session: AsyncSession, conversation_id: int) -> Conversation:
    conversation = await session.get(Conversation, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    return conversation


async def _list_conversations(
    session: AsyncSession, *, actor_id: int, query: str,
    study_group_id: int | None, cursor: str | None, limit: int,
) -> ConversationPageOut:
    query = query.strip()
    decoded = _conversation_cursor(cursor, query=query, study_group_id=study_group_id)
    if decoded is None:
        conversation_ceiling = int(await session.scalar(select(func.max(Conversation.id))) or 0)
        message_ceiling = int(await session.scalar(select(func.max(ChatMessage.id))) or 0)
        last_id = None
        last_time = None
    else:
        conversation_ceiling, message_ceiling, last_id, last_time = decoded
    if not conversation_ceiling:
        return ConversationPageOut(items=[], next_cursor=None)

    latest_id = (
        select(func.max(ChatMessage.id))
        .where(ChatMessage.conversation_id == Conversation.id, ChatMessage.id <= message_ceiling)
        .correlate(Conversation)
        .scalar_subquery()
    )
    latest = aliased(ChatMessage)
    read_marker = (
        select(ConversationRead.last_read_message_id)
        .where(
            ConversationRead.conversation_id == Conversation.id,
            ConversationRead.admin_telegram_id == actor_id,
        )
        .correlate(Conversation)
        .scalar_subquery()
    )
    incoming = aliased(ChatMessage)
    unread_count = (
        select(func.count(incoming.id))
        .where(
            incoming.conversation_id == Conversation.id,
            incoming.direction == "incoming",
            incoming.id <= message_ceiling,
            incoming.id > func.coalesce(read_marker, 0),
        )
        .correlate(Conversation)
        .scalar_subquery()
    )

    statement = (
        select(Conversation, User, StudyGroup, latest, unread_count)
        .join(User, User.id == Conversation.user_id)
        .outerjoin(StudyGroup, StudyGroup.id == User.study_group_id)
        .outerjoin(latest, latest.id == latest_id)
        .where(Conversation.id <= conversation_ceiling)
    )
    if query:
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        statement = statement.where(User.full_name.ilike(f"%{escaped}%", escape="\\"))
    if study_group_id is not None:
        statement = statement.where(User.study_group_id == study_group_id)

    if last_id is not None:
        if last_time is None:
            statement = statement.where(latest.id.is_(None), Conversation.id < last_id)
        else:
            statement = statement.where(or_(
                latest.created_at < last_time,
                and_(latest.created_at == last_time, Conversation.id < last_id),
                latest.id.is_(None),
            ))
    statement = statement.order_by(latest.created_at.desc().nulls_last(), Conversation.id.desc()).limit(limit + 1)
    rows = (await session.execute(statement)).all()
    page = rows[:limit]

    items = [
        ConversationItemOut(
            id=conversation.id,
            user=ConversationUserOut(
                id=user.id,
                telegram_id=user.telegram_id,
                full_name=user.full_name,
                study_group_id=user.study_group_id,
                study_group_name=group.display_code if group else None,
            ),
            last_message=_message_out(message) if message else None,
            unread_count=int(unread or 0),
            last_message_at=_utc(message.created_at) if message else None,
        )
        for conversation, user, group, message, unread in page
    ]
    next_cursor = None
    if len(rows) > limit:
        last_conversation, _, _, last_message, _ = page[-1]
        next_cursor = _encode_cursor({
            "v": 1,
            "c": conversation_ceiling,
            "m": message_ceiling,
            "id": last_conversation.id,
            "at": _utc(last_message.created_at).isoformat() if last_message else None,
            "q": query.casefold(),
            "g": study_group_id,
        })
    return ConversationPageOut(items=items, next_cursor=next_cursor)


def register_conversation_routes(
    app: FastAPI,
    settings: Settings,
    require_admin: Callable[[str | None, Settings, AsyncSession], Awaitable[int]],
) -> None:
    @app.post("/api/admin/conversations", response_model=ConversationOpenOut)
    async def admin_create_conversation(
        payload: ConversationCreateIn,
        response: Response,
        session: AsyncSession = Depends(get_session),
        x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
    ) -> ConversationOpenOut:
        await require_admin(x_telegram_init_data, settings, session)
        user = await session.get(User, payload.user_id)
        if (
            user is None
            or user.registration_completed_at is None
            or not user.full_name.strip()
            or user.full_name.strip() == "Без ФИО"
        ):
            raise HTTPException(status_code=404, detail="registered user not found")
        conversation, created = await get_or_create_conversation_with_status(session, user_id=user.id)
        await session.commit()
        response.status_code = 201 if created else 200
        return ConversationOpenOut(conversation_id=conversation.id)

    @app.get("/api/admin/conversations", response_model=ConversationPageOut)
    async def admin_list_conversations(
        query: str = Query(default="", max_length=128),
        study_group_id: int | None = Query(default=None, ge=1),
        cursor: str | None = Query(default=None),
        limit: int = Query(default=50, ge=1, le=100),
        session: AsyncSession = Depends(get_session),
        x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
    ) -> ConversationPageOut:
        actor_id = await require_admin(x_telegram_init_data, settings, session)
        return await _list_conversations(
            session, actor_id=actor_id, query=query, study_group_id=study_group_id,
            cursor=cursor, limit=limit,
        )

    @app.get("/api/admin/conversations/summary", response_model=ConversationSummaryOut)
    async def admin_conversations_summary(
        session: AsyncSession = Depends(get_session),
        x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
    ) -> ConversationSummaryOut:
        actor_id = await require_admin(x_telegram_init_data, settings, session)
        statement = (
            select(func.count(ChatMessage.id))
            .select_from(ChatMessage)
            .join(Conversation, Conversation.id == ChatMessage.conversation_id)
            .outerjoin(ConversationRead, and_(
                ConversationRead.conversation_id == Conversation.id,
                ConversationRead.admin_telegram_id == actor_id,
            ))
            .where(
                ChatMessage.direction == "incoming",
                ChatMessage.id > func.coalesce(ConversationRead.last_read_message_id, 0),
            )
        )
        count = await session.scalar(statement)
        return ConversationSummaryOut(unread_count=int(count or 0))

    @app.get("/api/admin/conversations/{conversation_id}/messages", response_model=ChatMessagePageOut)
    async def admin_conversation_messages(
        conversation_id: int,
        before: str | None = Query(default=None),
        limit: int = Query(default=50, ge=1, le=100),
        session: AsyncSession = Depends(get_session),
        x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
    ) -> ChatMessagePageOut:
        await require_admin(x_telegram_init_data, settings, session)
        await _conversation_or_404(session, conversation_id)
        before_id = _history_cursor(before, conversation_id)
        messages, next_id = await list_conversation_messages(
            session, conversation_id=conversation_id, before_id=before_id, limit=limit,
        )
        next_cursor = _encode_cursor({
            "v": 1, "conversation_id": conversation_id, "before_id": next_id,
        }) if next_id is not None else None
        return ChatMessagePageOut(items=[_message_out(message) for message in messages], next_cursor=next_cursor)

    @app.post("/api/admin/conversations/{conversation_id}/read", response_model=ConversationReadOut)
    async def admin_conversation_read(
        conversation_id: int,
        payload: ConversationReadIn,
        session: AsyncSession = Depends(get_session),
        x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
    ) -> ConversationReadOut:
        actor_id = await require_admin(x_telegram_init_data, settings, session)
        await _conversation_or_404(session, conversation_id)
        try:
            stored_id = await advance_read_marker(
                session, conversation_id=conversation_id,
                admin_telegram_id=actor_id, message_id=payload.last_read_message_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="message not found in conversation") from exc
        await session.commit()
        unread = await get_unread_count(session, conversation_id=conversation_id, admin_telegram_id=actor_id)
        return ConversationReadOut(last_read_message_id=stored_id, unread_count=unread)
