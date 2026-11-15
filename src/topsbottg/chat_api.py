"""Authenticated, read-only conversation API and per-admin read markers."""

from __future__ import annotations

import base64
import binascii
import json
import secrets
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from sqlalchemy import and_, delete, func, insert, literal, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
from starlette.datastructures import UploadFile

from topsbottg.attachment_staging import UploadRejected, remove_staged_file, stage_upload
from topsbottg.attachments import attachment_public_data
from topsbottg.chat_outbox import (
    IdempotencyConflict,
    create_outgoing_attachment,
    create_outgoing_message,
)
from topsbottg.config import Settings
from topsbottg.conversations import (
    advance_read_marker,
    get_or_create_conversation_with_status,
    get_unread_count,
    list_conversation_messages,
)
from topsbottg.db import get_session
from topsbottg.models import (
    ChatMessage,
    Conversation,
    ConversationListSnapshot,
    ConversationListSnapshotItem,
    ConversationRead,
    StudyGroup,
    User,
)
from topsbottg.schemas import (
    ChatMessageAcceptedOut,
    ChatMessageCreateIn,
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
    outbox = message.outbox
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
        delivery_status=outbox.status if outbox else None,
        delivery_error_code=outbox.error_code if outbox else None,
        attachment=attachment_public_data(message.attachment) if message.kind != "text" else None,
    )


def _encode_cursor(payload: dict[str, object]) -> str:
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(value: str, *, version: int = 1) -> dict[str, object]:
    if not value or len(value) > 2048:
        raise HTTPException(status_code=422, detail="invalid cursor")
    try:
        raw = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError, binascii.Error) as exc:
        raise HTTPException(status_code=422, detail="invalid cursor") from exc
    if not isinstance(payload, dict) or payload.get("v") != version:
        raise HTTPException(status_code=422, detail="invalid cursor")
    return payload


def _positive_int(value: object, *, allow_zero: bool = False) -> bool:
    return type(value) is int and (value >= 0 if allow_zero else value > 0)


def _conversation_cursor(value: str | None) -> tuple[str, int] | None:
    if value is None:
        return None
    payload = _decode_cursor(value, version=2)
    snapshot_id = payload.get("sid")
    position = payload.get("position")
    if (
        set(payload) != {"v", "sid", "position"}
        or not isinstance(snapshot_id, str)
        or not 20 <= len(snapshot_id) <= 48
        or not all(ch.isascii() and (ch.isalnum() or ch in "-_") for ch in snapshot_id)
        or not _positive_int(position)
    ):
        raise HTTPException(status_code=422, detail="invalid cursor")
    return snapshot_id, position


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


SNAPSHOT_TTL = timedelta(minutes=15)


async def _create_list_snapshot(
    session: AsyncSession, *, actor_id: int, query: str, study_group_id: int | None,
) -> str:
    """Freeze the committed ordering in one SQL statement, including in-flight IDs only after commit."""
    now = datetime.now(UTC)
    expired_ids = select(ConversationListSnapshot.id).where(ConversationListSnapshot.expires_at <= now)
    await session.execute(
        delete(ConversationListSnapshotItem).where(
            ConversationListSnapshotItem.snapshot_id.in_(expired_ids)
        )
    )
    await session.execute(
        delete(ConversationListSnapshot).where(ConversationListSnapshot.expires_at <= now)
    )

    snapshot_id = secrets.token_urlsafe(24)
    session.add(ConversationListSnapshot(
        id=snapshot_id,
        admin_telegram_id=actor_id,
        search_query=query.casefold(),
        study_group_id=study_group_id,
        expires_at=now + SNAPSHOT_TTL,
    ))
    await session.flush()

    # The latest message ID and the maximum created_at are not necessarily the
    # same row: PostgreSQL transaction timestamps can precede message ID order.
    latest_id = (
        select(func.max(ChatMessage.id))
        .where(ChatMessage.conversation_id == Conversation.id)
        .correlate(Conversation)
        .scalar_subquery()
    )
    activity = (
        select(func.max(ChatMessage.created_at))
        .where(ChatMessage.conversation_id == Conversation.id)
        .correlate(Conversation)
        .scalar_subquery()
    )
    snapshot_rows = (
        select(
            literal(snapshot_id),
            func.row_number().over(order_by=(activity.desc().nulls_last(), Conversation.id.desc())),
            Conversation.id,
            latest_id,
            activity,
        )
        .select_from(Conversation)
        .join(User, User.id == Conversation.user_id)
    )
    if query:
        if session.get_bind().dialect.name == "sqlite":
            # SQLite's lower() cannot case-fold Cyrillic.
            names = await session.execute(
                select(User.id, User.full_name).join(Conversation, Conversation.user_id == User.id)
            )
            matching_ids = [user_id for user_id, name in names if query.casefold() in name.casefold()]
            snapshot_rows = snapshot_rows.where(User.id.in_(matching_ids))
        else:
            escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            snapshot_rows = snapshot_rows.where(User.full_name.ilike(f"%{escaped}%", escape="\\"))
    if study_group_id is not None:
        snapshot_rows = snapshot_rows.where(User.study_group_id == study_group_id)

    await session.execute(insert(ConversationListSnapshotItem).from_select(
        ["snapshot_id", "position", "conversation_id", "last_message_id", "last_message_at"],
        snapshot_rows,
    ))
    await session.commit()
    return snapshot_id


async def _list_conversations(
    session: AsyncSession, *, actor_id: int, query: str,
    study_group_id: int | None, cursor: str | None, limit: int,
) -> ConversationPageOut:
    query = query.strip()
    decoded = _conversation_cursor(cursor)
    if decoded is None:
        snapshot_id = await _create_list_snapshot(
            session, actor_id=actor_id, query=query, study_group_id=study_group_id,
        )
        position = 0
    else:
        snapshot_id, position = decoded
        snapshot = await session.get(ConversationListSnapshot, snapshot_id)
        if (
            snapshot is None
            or snapshot.admin_telegram_id != actor_id
            or snapshot.search_query != query.casefold()
            or snapshot.study_group_id != study_group_id
            or _utc(snapshot.expires_at) <= datetime.now(UTC)
        ):
            raise HTTPException(status_code=422, detail="invalid cursor")

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
            incoming.id > func.coalesce(read_marker, 0),
        )
        .correlate(Conversation)
        .scalar_subquery()
    )
    rows = (await session.execute(
        select(
            ConversationListSnapshotItem, User, StudyGroup, ChatMessage, unread_count,
        )
        .join(Conversation, Conversation.id == ConversationListSnapshotItem.conversation_id)
        .join(User, User.id == Conversation.user_id)
        .outerjoin(StudyGroup, StudyGroup.id == User.study_group_id)
        .outerjoin(ChatMessage, ChatMessage.id == ConversationListSnapshotItem.last_message_id)
        .where(
            ConversationListSnapshotItem.snapshot_id == snapshot_id,
            ConversationListSnapshotItem.position > position,
        )
        .order_by(ConversationListSnapshotItem.position)
        .limit(limit + 1)
    )).all()

    page = rows[:limit]
    items = [
        ConversationItemOut(
            id=entry.conversation_id,
            user=ConversationUserOut(
                id=user.id,
                telegram_id=user.telegram_id,
                full_name=user.full_name,
                study_group_id=user.study_group_id,
                study_group_name=group.display_code if group else None,
            ),
            last_message=_message_out(message) if message else None,
            unread_count=int(unread or 0),
            last_message_at=_utc(entry.last_message_at) if entry.last_message_at else None,
        )
        for entry, user, group, message, unread in page
    ]
    next_cursor = _encode_cursor({
        "v": 2, "sid": snapshot_id, "position": page[-1][0].position,
    }) if len(rows) > limit else None
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

    @app.post(
        "/api/admin/conversations/{conversation_id}/messages",
        response_model=ChatMessageAcceptedOut,
        status_code=202,
    )
    async def admin_send_conversation_message(
        conversation_id: int,
        payload: ChatMessageCreateIn,
        idempotency_key: str | None = Header(
            default=None, alias="Idempotency-Key",
        ),
        session: AsyncSession = Depends(get_session),
        x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
    ) -> ChatMessageAcceptedOut:
        actor_id = await require_admin(x_telegram_init_data, settings, session)
        await _conversation_or_404(session, conversation_id)
        if (
            idempotency_key is None
            or not 1 <= len(idempotency_key) <= 128
            or not idempotency_key.isascii()
            or any(not 33 <= ord(ch) <= 126 for ch in idempotency_key)
        ):
            raise HTTPException(status_code=422, detail="invalid idempotency key")
        try:
            message, _ = await create_outgoing_message(
                session,
                conversation_id=conversation_id,
                admin_telegram_id=actor_id,
                idempotency_key=idempotency_key,
                text=payload.text,
                reply_to_message_id=payload.reply_to_message_id,
            )
        except IdempotencyConflict as exc:
            raise HTTPException(status_code=409, detail="idempotency key reused with different payload") from exc
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="reply message not found in conversation") from exc
        await session.commit()
        return ChatMessageAcceptedOut(message=_message_out(message))

    @app.post(
        "/api/admin/conversations/{conversation_id}/attachments",
        response_model=ChatMessageAcceptedOut,
        status_code=202,
    )
    async def admin_send_conversation_attachment(
        conversation_id: int,
        request: Request,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        session: AsyncSession = Depends(get_session),
        x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
    ) -> ChatMessageAcceptedOut:
        actor_id = await require_admin(x_telegram_init_data, settings, session)
        await _conversation_or_404(session, conversation_id)
        if (
            idempotency_key is None
            or not 1 <= len(idempotency_key) <= 128
            or not idempotency_key.isascii()
            or any(not 33 <= ord(ch) <= 126 for ch in idempotency_key)
        ):
            raise HTTPException(status_code=422, detail="invalid idempotency key")
        if request.headers.get("content-type", "").split(";", 1)[0].lower() != "multipart/form-data":
            raise HTTPException(status_code=415, detail="multipart/form-data required")

        # Parse multipart only after re-checking administrator permissions.
        form = await request.form(max_files=1, max_fields=4)
        staged = None
        keep_file = False
        try:
            allowed_fields = {"file", "kind", "caption", "reply_to_message_id"}
            if (
                set(form) - allowed_fields
                or any(len(form.getlist(key)) != 1 for key in form)
            ):
                raise HTTPException(status_code=422, detail="invalid form fields")
            upload = form.get("file")
            kind = form.get("kind")
            caption = form.get("caption")
            reply_field = form.get("reply_to_message_id")
            if not isinstance(upload, UploadFile) or not isinstance(kind, str):
                raise HTTPException(status_code=422, detail="file and kind required")
            if caption is not None and (not isinstance(caption, str) or len(caption) > 1024):
                raise HTTPException(status_code=422, detail="invalid caption")
            if reply_field is None or reply_field == "":
                reply_id = None
            elif (
                isinstance(reply_field, str)
                and reply_field.isascii() and reply_field.isdecimal()
                and int(reply_field) > 0
            ):
                reply_id = int(reply_field)
            else:
                raise HTTPException(status_code=422, detail="invalid reply_to_message_id")
            try:
                staged = await stage_upload(upload, kind=kind, root=settings.chat_staging_dir)
                message, created = await create_outgoing_attachment(
                    session,
                    conversation_id=conversation_id,
                    admin_telegram_id=actor_id,
                    idempotency_key=idempotency_key,
                    staged=staged,
                    caption=caption,
                    reply_to_message_id=reply_id,
                )
            except UploadRejected as exc:
                raise HTTPException(status_code=exc.status_code, detail=exc.code) from exc
            except IdempotencyConflict as exc:
                raise HTTPException(status_code=409, detail="idempotency key reused with different payload") from exc
            except ValueError as exc:
                raise HTTPException(status_code=404, detail="reply message not found in conversation") from exc
            # COMMIT can succeed before its await is cancelled or raises. Once a
            # new outbox row exists, deletion is unsafe; orphan cleanup owns the file.
            keep_file = created
            await session.commit()
            return ChatMessageAcceptedOut(message=_message_out(message))
        finally:
            await form.close()
            if staged is not None and not keep_file:
                remove_staged_file(settings.chat_staging_dir, staged.key)

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
