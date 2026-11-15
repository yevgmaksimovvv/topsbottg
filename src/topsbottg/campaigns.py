"""Text campaigns backed by the existing personal-conversation outbox."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from datetime import UTC, datetime

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from topsbottg.chat_outbox import IdempotencyConflict, create_outgoing_message
from topsbottg.conversations import get_or_create_conversation
from topsbottg.models import (
    CampaignRecipient,
    ChatMessageOutbox,
    MessageCampaign,
    StudyGroup,
    User,
)
from topsbottg.schemas import (
    CampaignCreateIn,
    CampaignDetailOut,
    CampaignOut,
    CampaignPreviewOut,
    CampaignPreviewRecipientOut,
    CampaignRecipientOut,
    CampaignRecipientsIn,
)
from topsbottg.services import queue_admin_event


def _dialect_insert(session: AsyncSession):
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        return pg_insert(MessageCampaign)
    if dialect == "sqlite":
        return sqlite_insert(MessageCampaign)
    raise NotImplementedError(f"Campaigns require PostgreSQL or SQLite, not {dialect}")


def _fingerprint(payload: CampaignCreateIn) -> str:
    canonical = {
        "user_ids": sorted(set(payload.user_ids)),
        "study_group_ids": sorted(set(payload.study_group_ids)),
        "text": payload.text,
    }
    encoded = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


async def resolve_campaign_recipients(
    session: AsyncSession, payload: CampaignRecipientsIn,
) -> list[CampaignPreviewRecipientOut]:
    """Resolve the current union; explicit users need not belong to a group."""
    user_ids = sorted(set(payload.user_ids))
    group_ids = sorted(set(payload.study_group_ids))
    if not user_ids and not group_ids:
        return []
    rows = (await session.execute(
        select(User.id, User.full_name, StudyGroup.display_code)
        .outerjoin(StudyGroup, StudyGroup.id == User.study_group_id)
        .where(
            or_(User.id.in_(user_ids), User.study_group_id.in_(group_ids)),
            User.registration_completed_at.is_not(None),
            func.trim(User.full_name) != "",
            func.trim(User.full_name) != "Без ФИО",
        )
        .order_by(User.id)
    )).all()
    return [
        CampaignPreviewRecipientOut(
            user_id=user_id, full_name=name, study_group_name=group_name,
        )
        for user_id, name, group_name in rows
    ]


async def preview_campaign(
    session: AsyncSession, payload: CampaignRecipientsIn,
) -> CampaignPreviewOut:
    recipients = await resolve_campaign_recipients(session, payload)
    return CampaignPreviewOut(recipient_count=len(recipients), recipients=recipients)


async def create_campaign(
    session: AsyncSession, *, actor_id: int,
    key: str, payload: CampaignCreateIn,
) -> tuple[MessageCampaign, bool]:
    """Idempotency and the entire recipient snapshot are committed together by the API."""
    digest = _fingerprint(payload)
    user_ids = sorted(set(payload.user_ids))
    group_ids = sorted(set(payload.study_group_ids))
    statement = (
        _dialect_insert(session)
        .values(
            admin_telegram_id=actor_id,
            idempotency_key=key,
            payload_fingerprint=digest,
            text=payload.text,
            user_ids=user_ids,
            study_group_ids=group_ids,
            recipient_count=0,
        )
        .on_conflict_do_nothing(
            index_elements=[MessageCampaign.admin_telegram_id, MessageCampaign.idempotency_key]
        )
        .returning(MessageCampaign.id)
    )
    campaign_id = (await session.execute(statement)).scalar_one_or_none()
    if campaign_id is None:
        existing = await session.scalar(
            select(MessageCampaign).where(
                MessageCampaign.admin_telegram_id == actor_id,
                MessageCampaign.idempotency_key == key,
            )
        )
        if existing is None:
            raise RuntimeError("campaign conflict did not yield an existing row")
        if existing.payload_fingerprint != digest:
            raise IdempotencyConflict("idempotency key belongs to another campaign")
        return existing, False

    campaign = await session.get(MessageCampaign, campaign_id)
    if campaign is None:
        raise RuntimeError("campaign insert did not return a row")

    recipients = await resolve_campaign_recipients(session, payload)
    if not recipients:
        raise ValueError("campaign has no registered recipients")
    campaign.recipient_count = len(recipients)
    for recipient in recipients:
        conversation = await get_or_create_conversation(
            session, user_id=recipient.user_id,
        )
        message, created = await create_outgoing_message(
            session,
            conversation_id=conversation.id,
            admin_telegram_id=actor_id,
            idempotency_key=f"campaign:{campaign.id}:{recipient.user_id}",
            text=payload.text,
        )
        if not created or message.outbox is None:
            raise RuntimeError("campaign outbox key collision")
        message.source = "campaign"
        session.add(CampaignRecipient(
            campaign_id=campaign.id,
            user_id=recipient.user_id,
            full_name=recipient.full_name,
            study_group_name=recipient.study_group_name,
            conversation_id=conversation.id,
            message_id=message.id,
            outbox_id=message.outbox.id,
            status="queued",
        ))
    await session.flush()
    return campaign, True


def _campaign_status(counts: dict[str, int], total: int) -> str:
    if total == 0 or counts["queued"] == total:
        return "queued"
    if counts["sent"] == total:
        return "sent"
    if counts["failed"] == total:
        return "failed"
    if counts["queued"] or counts["sending"]:
        return "sending"
    return "partially_failed"


async def campaign_summary(session: AsyncSession, campaign: MessageCampaign) -> CampaignOut:
    # Delivery statuses are read from the actual outbox, never from a stale cached total.
    counts = {"queued": 0, "sending": 0, "sent": 0, "failed": 0}
    result = await session.execute(
        select(ChatMessageOutbox.status, func.count(ChatMessageOutbox.id))
        .join(CampaignRecipient, CampaignRecipient.outbox_id == ChatMessageOutbox.id)
        .where(CampaignRecipient.campaign_id == campaign.id)
        .group_by(ChatMessageOutbox.status)
    )
    for status, count in result.all():
        counts[status] = int(count)
    created_at = campaign.created_at
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    return CampaignOut(
        id=campaign.id, recipient_count=campaign.recipient_count,
        status=_campaign_status(counts, campaign.recipient_count),
        created_at=created_at,
        admin_telegram_id=campaign.admin_telegram_id,
        queued_count=counts["queued"], sending_count=counts["sending"],
        sent_count=counts["sent"], failed_count=counts["failed"],
    )


async def campaign_detail(session: AsyncSession, campaign: MessageCampaign) -> CampaignDetailOut:
    summary = await campaign_summary(session, campaign)
    rows = (await session.execute(
        select(CampaignRecipient, ChatMessageOutbox.status, ChatMessageOutbox.error_code)
        .join(ChatMessageOutbox, ChatMessageOutbox.id == CampaignRecipient.outbox_id)
        .where(CampaignRecipient.campaign_id == campaign.id)
        .order_by(CampaignRecipient.user_id)
    )).all()
    recipients = [
        CampaignRecipientOut(
            user_id=row.user_id, full_name=row.full_name,
            study_group_name=row.study_group_name,
            conversation_id=row.conversation_id, message_id=row.message_id,
            status=status, error_code=error, retry_count=row.retry_count,
        )
        for row, status, error in rows
    ]
    return CampaignDetailOut(**summary.model_dump(), recipients=recipients)


async def retry_failed_campaign(
    session: AsyncSession, campaign: MessageCampaign,
) -> int:
    """Requeue failed delivery jobs in place; never create another chat message."""
    rows = (await session.execute(
        select(CampaignRecipient, ChatMessageOutbox)
        .join(ChatMessageOutbox, ChatMessageOutbox.id == CampaignRecipient.outbox_id)
        .where(
            CampaignRecipient.campaign_id == campaign.id,
            ChatMessageOutbox.status == "failed",
        )
        .order_by(CampaignRecipient.user_id)
        .with_for_update(of=ChatMessageOutbox)
    )).all()
    for recipient, outbox in rows:
        outbox.status = "queued"
        outbox.attempts = 0
        outbox.error_code = None
        outbox.retry_at = None
        outbox.claim_token = None
        outbox.claimed_until = None
        recipient.status = "queued"
        recipient.error_code = None
        recipient.retry_count += 1
        queue_admin_event(session, "chat.changed", {"conversation_id": recipient.conversation_id})
    await session.flush()
    return len(rows)


def encode_campaign_cursor(campaign_id: int) -> str:
    payload = json.dumps({"v": 1, "before": campaign_id}, separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")


def decode_campaign_cursor(value: str | None) -> int | None:
    if value is None:
        return None
    if not value or len(value) > 256:
        raise ValueError("invalid cursor")
    try:
        raw = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError, binascii.Error) as exc:
        raise ValueError("invalid cursor") from exc
    if (
        not isinstance(payload, dict)
        or set(payload) != {"v", "before"}
        or payload["v"] != 1
        or type(payload["before"]) is not int
        or payload["before"] <= 0
    ):
        raise ValueError("invalid cursor")
    return payload["before"]
