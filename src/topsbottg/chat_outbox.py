"""Persistent, per-conversation idempotent Telegram message delivery."""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, case, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from topsbottg.conversations import lock_conversation_for_message
from topsbottg.models import ChatMessage, ChatMessageOutbox, Conversation, User
from topsbottg.services import queue_admin_event

MAX_CHAT_ATTEMPTS = 5
CHAT_CLAIM_LEASE = timedelta(minutes=2)
CHAT_RETRY_BASE_SECONDS = 3
CHAT_RETRY_MAX_SECONDS = 300


class IdempotencyConflict(ValueError):
    pass


@dataclass(frozen=True)
class ChatOutboxClaim:
    id: int
    message_id: int
    conversation_id: int
    chat_id: int
    text: str
    attempts: int
    claim_token: str
    reply_to_message_id: int | None
    reply_to_telegram_message_id: int | None
    reply_target_failed: bool


def _fingerprint(text: str, reply_to_message_id: int | None) -> str:
    payload = json.dumps(
        {"text": text, "reply_to_message_id": reply_to_message_id},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


async def create_outgoing_message(
    session: AsyncSession,
    *,
    conversation_id: int,
    admin_telegram_id: int,
    idempotency_key: str,
    text: str,
    reply_to_message_id: int | None = None,
) -> tuple[ChatMessage, bool]:
    """Atomically add a chat message and its delivery job. Commit belongs to the caller."""
    if not conversation_id > 0 or not admin_telegram_id > 0 or not idempotency_key:
        raise ValueError("invalid outgoing message identity")
    if not text.strip() or len(text) > 4096:
        raise ValueError("invalid outgoing text")
    fingerprint = _fingerprint(text, reply_to_message_id)
    # The same transaction lock is taken by incoming and outgoing writers before
    # allocating message IDs, preserving unread and history ordering.
    await lock_conversation_for_message(session, conversation_id=conversation_id)

    existing = await session.scalar(
        select(ChatMessageOutbox).where(
            ChatMessageOutbox.conversation_id == conversation_id,
            ChatMessageOutbox.admin_telegram_id == admin_telegram_id,
            ChatMessageOutbox.idempotency_key == idempotency_key,
        )
    )
    if existing is not None:
        if existing.payload_fingerprint != fingerprint:
            raise IdempotencyConflict("idempotency key belongs to a different payload")
        message = await session.get(ChatMessage, existing.message_id)
        if message is None:
            raise RuntimeError("outbox references missing message")
        return message, False

    if reply_to_message_id is not None:
        target = await session.scalar(
            select(ChatMessage.id).where(
                ChatMessage.id == reply_to_message_id,
                ChatMessage.conversation_id == conversation_id,
            )
        )
        if target is None:
            raise ValueError("reply target is not in this conversation")

    message = ChatMessage(
        conversation_id=conversation_id,
        direction="outgoing",
        kind="text",
        text=text,
        author_admin_telegram_id=admin_telegram_id,
        source="admin",
    )
    outbox = ChatMessageOutbox(
        message=message,
        conversation_id=conversation_id,
        admin_telegram_id=admin_telegram_id,
        idempotency_key=idempotency_key,
        payload_fingerprint=fingerprint,
        reply_to_message_id=reply_to_message_id,
        status="queued",
    )
    session.add(outbox)
    await session.flush()

    now = func.now()
    await session.execute(
        update(Conversation).where(Conversation.id == conversation_id).values(
            last_message_at=case(
                (Conversation.last_message_at.is_(None), now),
                (Conversation.last_message_at < now, now),
                else_=Conversation.last_message_at,
            ),
            updated_at=now,
        )
    )
    queue_admin_event(session, "chat.changed", {"conversation_id": conversation_id})
    return message, True


async def claim_chat_outbox(
    session: AsyncSession, *, limit: int = 1, now: datetime | None = None
) -> list[ChatOutboxClaim]:
    """Claim rows with SKIP LOCKED, then commit before any Telegram network request."""
    if not 1 <= limit <= 50:
        raise ValueError("outbox claim limit must be between 1 and 50")
    now = now or datetime.now(UTC)
    rows = list((await session.scalars(
        select(ChatMessageOutbox)
        .where(or_(
            and_(
                ChatMessageOutbox.status == "queued",
                or_(ChatMessageOutbox.retry_at.is_(None), ChatMessageOutbox.retry_at <= now),
            ),
            and_(
                ChatMessageOutbox.status == "sending",
                ChatMessageOutbox.claimed_until <= now,
            ),
        ))
        .order_by(ChatMessageOutbox.id)
        .limit(limit)
        .with_for_update(of=ChatMessageOutbox, skip_locked=True)
    )).all())

    claims: list[ChatOutboxClaim] = []
    for row in rows:
        if row.attempts >= MAX_CHAT_ATTEMPTS:
            row.status = "failed"
            row.error_code = "attempts_exhausted"
            row.claimed_until = None
            row.claim_token = None
            row.retry_at = None
            queue_admin_event(session, "chat.changed", {"conversation_id": row.conversation_id})
            continue
        message = await session.get(ChatMessage, row.message_id)
        if message is None or message.text is None:
            raise RuntimeError("outbox references missing outgoing message")
        destination = await session.scalar(
            select(User.telegram_id)
            .join(Conversation, Conversation.user_id == User.id)
            .where(Conversation.id == row.conversation_id)
        )
        if destination is None:
            raise RuntimeError("outbox conversation has no recipient")

        target_telegram_id = None
        target_failed = False
        if row.reply_to_message_id is not None:
            target_telegram_id = await session.scalar(
                select(ChatMessage.telegram_message_id).where(
                    ChatMessage.id == row.reply_to_message_id,
                    ChatMessage.conversation_id == row.conversation_id,
                )
            )
            if target_telegram_id is None:
                target_status = await session.scalar(
                    select(ChatMessageOutbox.status).where(
                        ChatMessageOutbox.message_id == row.reply_to_message_id
                    )
                )
                target_failed = target_status == "failed"

        token = secrets.token_hex(16)
        row.status = "sending"
        row.attempts += 1
        row.claim_token = token
        row.claimed_until = now + CHAT_CLAIM_LEASE
        row.retry_at = None
        queue_admin_event(session, "chat.changed", {"conversation_id": row.conversation_id})
        claims.append(ChatOutboxClaim(
            id=row.id,
            message_id=message.id,
            conversation_id=row.conversation_id,
            chat_id=destination,
            text=message.text,
            attempts=row.attempts,
            claim_token=token,
            reply_to_message_id=row.reply_to_message_id,
            reply_to_telegram_message_id=target_telegram_id,
            reply_target_failed=target_failed,
        ))
    return claims


async def settle_chat_outbox(
    session: AsyncSession,
    claim: ChatOutboxClaim,
    *,
    telegram_message_id: int | None = None,
    error_code: str | None = None,
    retry_after: float | None = None,
    permanent: bool = False,
    now: datetime | None = None,
) -> bool:
    """Compare claim tokens so a late response cannot overwrite a newer delivery attempt."""
    now = now or datetime.now(UTC)
    row = await session.scalar(
        select(ChatMessageOutbox)
        .where(ChatMessageOutbox.id == claim.id)
        .with_for_update()
    )
    if row is None or row.status != "sending" or row.claim_token != claim.claim_token:
        return False

    if telegram_message_id is not None:
        if telegram_message_id <= 0:
            raise ValueError("Telegram message ID must be positive")
        message = await session.get(ChatMessage, row.message_id)
        if message is None:
            raise RuntimeError("outbox references missing message")
        row.telegram_message_id = telegram_message_id
        row.status = "sent"
        row.error_code = None
        row.retry_at = None
        message.telegram_message_id = telegram_message_id
        message.telegram_chat_id = claim.chat_id
        message.reply_to_telegram_message_id = claim.reply_to_telegram_message_id
    else:
        row.error_code = (error_code or "delivery_unknown")[:64]
        if permanent or row.attempts >= MAX_CHAT_ATTEMPTS:
            row.status = "failed"
            row.retry_at = None
        else:
            row.status = "queued"
            delay = min(CHAT_RETRY_BASE_SECONDS * 2 ** (row.attempts - 1), CHAT_RETRY_MAX_SECONDS)
            row.retry_at = now + timedelta(seconds=max(float(delay), retry_after or 0))
    row.claimed_until = None
    row.claim_token = None
    queue_admin_event(session, "chat.changed", {"conversation_id": row.conversation_id})
    return True
