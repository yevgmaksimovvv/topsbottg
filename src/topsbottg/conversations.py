"""Persistent storage operations for individual administrator conversations."""

from __future__ import annotations

from sqlalchemy import case, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from topsbottg.models import ChatMessage, Conversation, ConversationRead


def _insert(session: AsyncSession, table):
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        return pg_insert(table)
    if dialect == "sqlite":
        return sqlite_insert(table)
    raise NotImplementedError(f"Conversation storage requires PostgreSQL or SQLite, got {dialect}")


async def get_or_create_conversation(session: AsyncSession, *, user_id: int) -> Conversation:
    """Return the user's sole conversation without committing the caller's transaction."""
    if user_id <= 0:
        raise ValueError("user_id must be positive")

    statement = _insert(session, Conversation).values(user_id=user_id)
    await session.execute(statement.on_conflict_do_nothing(index_elements=[Conversation.user_id]))
    conversation = await session.scalar(select(Conversation).where(Conversation.user_id == user_id))
    if conversation is None:
        raise RuntimeError("Conversation insert did not produce a row")
    return conversation


async def record_incoming_message(
    session: AsyncSession,
    *,
    conversation_id: int,
    telegram_chat_id: int,
    telegram_message_id: int,
    text: str | None,
    kind: str = "text",
    reply_to_telegram_message_id: int | None = None,
    source: str = "telegram",
) -> tuple[ChatMessage, bool]:
    """Return (message, created); retries never change an existing message."""
    if conversation_id <= 0 or telegram_chat_id == 0 or telegram_message_id <= 0:
        raise ValueError("Invalid conversation or Telegram message identity")

    statement = _insert(session, ChatMessage).values(
        conversation_id=conversation_id,
        direction="incoming",
        kind=kind,
        text=text,
        telegram_chat_id=telegram_chat_id,
        telegram_message_id=telegram_message_id,
        reply_to_telegram_message_id=reply_to_telegram_message_id,
        source=source,
    )
    statement = statement.on_conflict_do_nothing(
        index_elements=[
            ChatMessage.direction,
            ChatMessage.telegram_chat_id,
            ChatMessage.telegram_message_id,
        ],
        index_where=(
            ChatMessage.telegram_chat_id.is_not(None) & ChatMessage.telegram_message_id.is_not(None)
        ),
    ).returning(ChatMessage.id)
    inserted_id = (await session.execute(statement)).scalar_one_or_none()
    created = inserted_id is not None

    if created:
        now = func.now()
        await session.execute(
            update(Conversation)
            .where(Conversation.id == conversation_id)
            .values(
                last_message_at=case(
                    (Conversation.last_message_at.is_(None), now),
                    (Conversation.last_message_at < now, now),
                    else_=Conversation.last_message_at,
                ),
                updated_at=now,
            )
        )
        message = await session.get(ChatMessage, inserted_id)
    else:
        message = await session.scalar(
            select(ChatMessage).where(
                ChatMessage.direction == "incoming",
                ChatMessage.telegram_chat_id == telegram_chat_id,
                ChatMessage.telegram_message_id == telegram_message_id,
            )
        )

    if message is None:
        raise RuntimeError("Telegram message could not be retrieved")
    if message.conversation_id != conversation_id:
        raise ValueError("Telegram message belongs to a different conversation")
    return message, created


async def list_conversation_messages(
    session: AsyncSession, *, conversation_id: int, before_id: int | None = None, limit: int = 50
) -> tuple[list[ChatMessage], int | None]:
    """Page backward by internal ID; return each page in ascending ID order."""
    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    if before_id is not None and before_id <= 0:
        raise ValueError("before_id must be positive")

    query = select(ChatMessage).where(ChatMessage.conversation_id == conversation_id)
    if before_id is not None:
        query = query.where(ChatMessage.id < before_id)
    rows = list((await session.scalars(query.order_by(ChatMessage.id.desc()).limit(limit + 1))).all())
    page = list(reversed(rows[:limit]))
    return page, page[0].id if len(rows) > limit else None


async def get_unread_count(session: AsyncSession, *, conversation_id: int, admin_telegram_id: int) -> int:
    """Count only incoming messages newer than this administrator's read marker."""
    marker = (
        select(ConversationRead.last_read_message_id)
        .where(
            ConversationRead.conversation_id == conversation_id,
            ConversationRead.admin_telegram_id == admin_telegram_id,
        )
        .scalar_subquery()
    )
    count = await session.scalar(
        select(func.count(ChatMessage.id)).where(
            ChatMessage.conversation_id == conversation_id,
            ChatMessage.direction == "incoming",
            ChatMessage.id > func.coalesce(marker, 0),
        )
    )
    return int(count or 0)


async def advance_read_marker(
    session: AsyncSession, *, conversation_id: int, admin_telegram_id: int, message_id: int
) -> int:
    """Atomically advance one admin's marker to an existing message in this conversation."""
    if conversation_id <= 0 or admin_telegram_id <= 0 or message_id <= 0:
        raise ValueError("Read marker identifiers must be positive")
    existing = await session.scalar(
        select(ChatMessage.id).where(
            ChatMessage.id == message_id, ChatMessage.conversation_id == conversation_id
        )
    )
    if existing is None:
        raise ValueError("Read marker must point to an existing message in this conversation")

    statement = _insert(session, ConversationRead).values(
        conversation_id=conversation_id,
        admin_telegram_id=admin_telegram_id,
        last_read_message_id=message_id,
    )
    next_id = statement.excluded.last_read_message_id
    statement = statement.on_conflict_do_update(
        index_elements=[ConversationRead.conversation_id, ConversationRead.admin_telegram_id],
        set_={
            "last_read_message_id": case(
                (ConversationRead.last_read_message_id.is_(None), next_id),
                (ConversationRead.last_read_message_id < next_id, next_id),
                else_=ConversationRead.last_read_message_id,
            )
        },
    )
    await session.execute(statement)
    stored = await session.scalar(
        select(ConversationRead.last_read_message_id).where(
            ConversationRead.conversation_id == conversation_id,
            ConversationRead.admin_telegram_id == admin_telegram_id,
        )
    )
    if stored is None:
        raise RuntimeError("Read marker was not saved")
    return stored
