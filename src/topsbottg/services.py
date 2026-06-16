from __future__ import annotations

import csv
import io
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta
from string import Formatter

from sqlalchemy import exists, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from topsbottg.models import (
    AuditLog,
    PaymentProfile,
    PaymentReply,
    Payout,
    PayoutRecipient,
    PayoutStatus,
    RecipientStatus,
    User,
)

DEFAULT_MESSAGE_TEMPLATE = """Всем привет!
Выплачиваем ЗАРПЛАТУ за работу в период с {period_from} по {period_to}.

Для получения выплаты проверьте или заполните платежные данные.

Если перевод на ваши данные:
укажите фамилию и имя, номер телефона для перевода / СБП, банк.

Если перевод на чужие данные:
укажите ваши фамилию и имя, имя владельца, номер телефона владельца, банк.

После сохранения бот должен ответить: «Ваше сообщение сохранено»."""

ACTIVATED_PAYOUT_STATUSES = {
    PayoutStatus.sending.value,
    PayoutStatus.sent.value,
    PayoutStatus.partially_failed.value,
}

PAYMENT_RECEIPT_RECIPIENT_STATUSES = {
    RecipientStatus.sent.value,
    RecipientStatus.payment_required.value,
}


def utcnow() -> datetime:
    return datetime.now(UTC)


def payment_profile_snapshot(profile: PaymentProfile) -> dict[str, str | None]:
    return {
        "raw_payment_details": profile.raw_payment_details,
    }


def render_message_template(payout: Payout) -> str:
    return payout.message_template.format(
        period_from=payout.period_from.isoformat(),
        period_to=payout.period_to.isoformat(),
    )


def _normalize_raw_payment_details(payload: dict | str) -> str:
    if isinstance(payload, str):
        raw_text = payload
    else:
        payload = dict(payload)
        if set(payload) != {"raw_payment_details"}:
            raise ValueError("payment details must contain raw_payment_details only")
        raw_text = payload["raw_payment_details"]
    if not isinstance(raw_text, str):
        raise ValueError("payment details must be a string")
    raw_text = raw_text.strip()
    if not raw_text:
        raise ValueError("payment details must be non-empty")
    return raw_text


def _has_payment_details(profile: PaymentProfile | None) -> bool:
    return bool(
        profile
        and profile.deleted_at is None
        and profile.raw_payment_details
        and profile.raw_payment_details.strip()
    )


def validate_message_template(template: str) -> None:
    formatter = Formatter()
    try:
        parts = list(formatter.parse(template))
    except ValueError as exc:
        raise ValueError("message_template is invalid") from exc
    allowed_fields = {"period_from", "period_to"}
    for _literal_text, field_name, format_spec, conversion in parts:
        if field_name is None:
            continue
        if field_name not in allowed_fields:
            raise ValueError("message_template may use only {period_from} and {period_to}")
        if format_spec or conversion:
            raise ValueError("message_template placeholders must not use formatting options")


def _validate_payout_period(period_from, period_to) -> None:
    if period_from > period_to:
        raise ValueError("period_from must be earlier than or equal to period_to")


async def log_audit(
    session: AsyncSession,
    *,
    actor_telegram_id: int,
    action: str,
    entity_type: str,
    entity_id: str,
    metadata: dict | None = None,
) -> None:
    session.add(
        AuditLog(
            actor_telegram_id=actor_telegram_id,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            metadata=metadata,
        )
    )


async def get_or_create_user(session: AsyncSession, telegram_id: int, full_name: str | None = None) -> User:
    user = await session.scalar(select(User).where(User.telegram_id == telegram_id))
    if user:
        return user
    user = User(telegram_id=telegram_id, full_name=full_name or "Без ФИО")
    session.add(user)
    await session.flush()
    return user


async def start_user(session: AsyncSession, telegram_id: int) -> tuple[User, bool]:
    user = await get_user_by_telegram_id(session, telegram_id)
    if user is not None:
        return user, user.full_name == "Без ФИО"
    user = User(telegram_id=telegram_id, full_name="Без ФИО")
    session.add(user)
    await session.flush()
    return user, True


async def get_payment_profile(session: AsyncSession, user_id: int) -> PaymentProfile | None:
    profile = await session.scalar(select(PaymentProfile).where(PaymentProfile.user_id == user_id))
    if not _has_payment_details(profile):
        return None
    return profile


async def get_payment_profile_for_admin_reveal(session: AsyncSession, user_id: int) -> PaymentProfile | None:
    return await get_payment_profile(session, user_id)


async def get_user_by_id(session: AsyncSession, user_id: int) -> User | None:
    return await session.get(User, user_id)


async def get_user_by_telegram_id(session: AsyncSession, telegram_id: int) -> User | None:
    return await session.scalar(select(User).where(User.telegram_id == telegram_id))


async def upsert_full_name(session: AsyncSession, telegram_id: int, full_name: str) -> User:
    user = await get_or_create_user(session, telegram_id, full_name=full_name)
    user.full_name = full_name
    await session.flush()
    return user


async def upsert_payment_profile(session: AsyncSession, user: User, payload: dict | str) -> PaymentProfile:
    raw_payment_details = _normalize_raw_payment_details(payload)
    profile = await session.scalar(select(PaymentProfile).where(PaymentProfile.user_id == user.id))
    if profile is None:
        profile = PaymentProfile(user_id=user.id, raw_payment_details=raw_payment_details)
        session.add(profile)
    else:
        profile.raw_payment_details = raw_payment_details
        profile.deleted_at = None
    await session.flush()
    return profile


async def soft_delete_payment_profile(session: AsyncSession, user: User) -> None:
    profile = await session.scalar(select(PaymentProfile).where(PaymentProfile.user_id == user.id))
    if profile:
        profile.deleted_at = utcnow()
        await session.flush()


async def list_users(
    session: AsyncSession,
    *,
    search: str | None = None,
    has_payment_profile: bool | None = None,
    is_active: bool | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[tuple[User, PaymentProfile | None]]:
    limit = max(1, min(limit, 100))
    offset = max(0, offset)
    dialect_name = _session_dialect_name(session)
    profile_exists = exists(
        select(1).where(
            PaymentProfile.user_id == User.id,
            PaymentProfile.deleted_at.is_(None),
            func.length(func.trim(PaymentProfile.raw_payment_details)) > 0,
        )
    )
    stmt = select(User.id).order_by(User.id.desc())
    if search:
        needle = search.strip()
        if dialect_name == "sqlite":
            stmt = stmt.where(User.full_name.like(f"%{needle}%"))
        else:
            stmt = stmt.where(func.lower(User.full_name).like(f"%{needle.lower()}%"))
    if is_active is not None:
        stmt = stmt.where(User.is_active == is_active)
    if has_payment_profile is True:
        stmt = stmt.where(profile_exists)
    elif has_payment_profile is False:
        stmt = stmt.where(~profile_exists)
    stmt = stmt.offset(offset).limit(limit + 1)
    user_ids = list((await session.scalars(stmt)).all())
    if not user_ids:
        return []
    selected_ids = user_ids[:limit]
    rows = await session.scalars(
        select(User).options(selectinload(User.payment_profile)).where(User.id.in_(selected_ids))
    )
    users_by_id = {user.id: user for user in rows}
    result: list[tuple[User, PaymentProfile | None]] = []
    for user_id in selected_ids:
        user = users_by_id[user_id]
        profile = user.payment_profile
        if not _has_payment_details(profile):
            profile = None
        result.append((user, profile))
    return result


async def create_payout(session: AsyncSession, *, actor_telegram_id: int, payload: dict) -> Payout:
    message_template = payload.get("message_template") or DEFAULT_MESSAGE_TEMPLATE
    validate_message_template(message_template)
    _validate_payout_period(payload["period_from"], payload["period_to"])
    payout = Payout(
        title=payload["title"],
        period_from=payload["period_from"],
        period_to=payload["period_to"],
        message_template=message_template,
        status=PayoutStatus.draft.value,
        created_by_telegram_id=actor_telegram_id,
    )
    session.add(payout)
    await session.flush()
    await log_audit(
        session,
        actor_telegram_id=actor_telegram_id,
        action="create_payout",
        entity_type="payout",
        entity_id=str(payout.id),
    )
    return payout


async def update_payout(session: AsyncSession, payout: Payout, *, actor_telegram_id: int, payload: dict) -> Payout:
    next_title = payout.title if payload.get("title") is None else payload["title"]
    next_period_from = payout.period_from if payload.get("period_from") is None else payload["period_from"]
    next_period_to = payout.period_to if payload.get("period_to") is None else payload["period_to"]
    payload_message_template = payload.get("message_template")
    next_message_template = payout.message_template if payload_message_template is None else payload_message_template
    if next_message_template is not None:
        validate_message_template(next_message_template)
    _validate_payout_period(next_period_from, next_period_to)
    payout.title = next_title
    payout.period_from = next_period_from
    payout.period_to = next_period_to
    if next_message_template is not None:
        payout.message_template = next_message_template
    await session.flush()
    await log_audit(
        session,
        actor_telegram_id=actor_telegram_id,
        action="update_payout",
        entity_type="payout",
        entity_id=str(payout.id),
        metadata=payload,
    )
    return payout


async def set_payout_sending(session: AsyncSession, payout: Payout, *, actor_telegram_id: int) -> Payout:
    if payout.status != PayoutStatus.draft.value:
        raise ValueError("payout can only be sent from draft")
    payout.status = PayoutStatus.sending.value
    await session.flush()
    await log_audit(
        session,
        actor_telegram_id=actor_telegram_id,
        action="send_payout",
        entity_type="payout",
        entity_id=str(payout.id),
    )
    return payout


async def complete_payout(session: AsyncSession, payout: Payout, *, actor_telegram_id: int) -> Payout:
    if payout.status == PayoutStatus.completed.value:
        return payout
    if payout.status == PayoutStatus.cancelled.value:
        raise ValueError("cancelled payout cannot be completed")
    if payout.status not in {PayoutStatus.sent.value, PayoutStatus.partially_failed.value}:
        raise ValueError("payout can only be completed after worker finished")
    payout.status = PayoutStatus.completed.value
    await session.flush()
    await log_audit(
        session,
        actor_telegram_id=actor_telegram_id,
        action="complete_payout",
        entity_type="payout",
        entity_id=str(payout.id),
    )
    return payout


async def cancel_payout(session: AsyncSession, payout: Payout, *, actor_telegram_id: int) -> Payout:
    if payout.status == PayoutStatus.completed.value:
        raise ValueError("completed payout cannot be cancelled")
    if payout.status == PayoutStatus.cancelled.value:
        return payout
    payout.status = PayoutStatus.cancelled.value
    await session.flush()
    await log_audit(
        session,
        actor_telegram_id=actor_telegram_id,
        action="cancel_payout",
        entity_type="payout",
        entity_id=str(payout.id),
    )
    return payout


async def add_recipients(session: AsyncSession, payout: Payout, user_ids: Sequence[int]) -> list[PayoutRecipient]:
    created: list[PayoutRecipient] = []
    existing = await session.scalars(
        select(PayoutRecipient.user_id).where(
            PayoutRecipient.payout_id == payout.id, PayoutRecipient.user_id.in_(list(user_ids))
        )
    )
    existing_ids = set(existing.all())
    for user_id in user_ids:
        if user_id in existing_ids:
            continue
        recipient = PayoutRecipient(payout_id=payout.id, user_id=user_id, status=RecipientStatus.pending.value)
        session.add(recipient)
        created.append(recipient)
    await session.flush()
    return created


async def cancel_recipient(
    session: AsyncSession, recipient: PayoutRecipient, *, actor_telegram_id: int
) -> PayoutRecipient:
    if recipient.status == RecipientStatus.paid.value:
        raise ValueError("paid recipient cannot be cancelled")
    if recipient.status != RecipientStatus.cancelled.value:
        recipient.status = RecipientStatus.cancelled.value
        await session.flush()
        await log_audit(
            session,
            actor_telegram_id=actor_telegram_id,
            action="cancel_recipient",
            entity_type="payout_recipient",
            entity_id=str(recipient.id),
        )
    return recipient


async def retry_failed_recipient(
    session: AsyncSession, recipient: PayoutRecipient, *, actor_telegram_id: int
) -> PayoutRecipient:
    if recipient.status != RecipientStatus.failed.value:
        raise ValueError("recipient can only be retried from failed")
    recipient.status = RecipientStatus.pending.value
    recipient.failed_at = None
    recipient.failure_reason = None
    await session.flush()
    await log_audit(
        session,
        actor_telegram_id=actor_telegram_id,
        action="retry_failed_recipient",
        entity_type="payout_recipient",
        entity_id=str(recipient.id),
    )
    return recipient


async def mark_paid(
    session: AsyncSession,
    recipient: PayoutRecipient,
    *,
    actor_telegram_id: int,
    paid_at: datetime | None = None,
    paid_note: str | None = None,
) -> PayoutRecipient:
    if recipient.status != RecipientStatus.payment_received.value:
        raise ValueError("recipient can only be marked paid from payment_received")
    recipient.status = RecipientStatus.paid.value
    recipient.paid_at = paid_at or utcnow()
    recipient.paid_by_admin_id = actor_telegram_id
    recipient.paid_note = paid_note
    await session.flush()
    await log_audit(
        session,
        actor_telegram_id=actor_telegram_id,
        action="recipient_marked_paid",
        entity_type="payout_recipient",
        entity_id=str(recipient.id),
    )
    return recipient


async def get_payout(session: AsyncSession, payout_id: int) -> Payout | None:
    stmt = select(Payout).where(Payout.id == payout_id)
    return await session.scalar(stmt)


async def list_payouts(session: AsyncSession) -> list[Payout]:
    rows = await session.scalars(select(Payout).order_by(Payout.id.desc()))
    return list(rows)


async def get_payout_recipients(session: AsyncSession, payout_id: int) -> list[PayoutRecipient]:
    stmt = (
        select(PayoutRecipient)
        .options(selectinload(PayoutRecipient.user))
        .options(selectinload(PayoutRecipient.payment_replies))
        .where(PayoutRecipient.payout_id == payout_id)
        .order_by(PayoutRecipient.id.asc())
    )
    rows = await session.scalars(stmt)
    return list(rows)


async def get_latest_active_recipient(session: AsyncSession, user_id: int) -> PayoutRecipient | None:
    stmt = (
        select(PayoutRecipient)
        .join(Payout)
        .where(
            PayoutRecipient.user_id == user_id,
            PayoutRecipient.status.in_(list(PAYMENT_RECEIPT_RECIPIENT_STATUSES)),
            Payout.status.in_(list(ACTIVATED_PAYOUT_STATUSES)),
        )
        .order_by(PayoutRecipient.id.desc())
    )
    return await session.scalar(stmt)


async def save_payment_reply(
    session: AsyncSession,
    recipient: PayoutRecipient,
    raw_text: str,
    *,
    parsed: dict | None = None,
) -> PaymentReply:
    reply = PaymentReply(payout_recipient_id=recipient.id, raw_text=raw_text, parsed=parsed)
    session.add(reply)
    recipient.replied_at = utcnow()
    await session.flush()
    return reply


async def save_profile_and_snapshot(
    session: AsyncSession,
    *,
    user: User,
    payload: dict | str,
    active_recipient: PayoutRecipient | None,
) -> PaymentProfile:
    profile = await upsert_payment_profile(session, user, payload)
    if active_recipient is not None and active_recipient.status in PAYMENT_RECEIPT_RECIPIENT_STATUSES:
        active_recipient.payment_profile_snapshot = payment_profile_snapshot(profile)
        active_recipient.status = RecipientStatus.payment_received.value
        active_recipient.replied_at = utcnow()
    await session.flush()
    return profile


async def confirm_saved_profile(
    session: AsyncSession,
    *,
    recipient_id: int,
    user_id: int,
) -> PayoutRecipient | None:
    recipient = await session.scalar(
        select(PayoutRecipient)
        .join(User)
        .options(selectinload(PayoutRecipient.user))
        .where(PayoutRecipient.id == recipient_id, PayoutRecipient.user_id == user_id)
    )
    if recipient is None or recipient.status not in PAYMENT_RECEIPT_RECIPIENT_STATUSES:
        return None
    profile = await get_payment_profile(session, user_id)
    if profile is None:
        return None
    recipient.payment_profile_snapshot = payment_profile_snapshot(profile)
    recipient.status = RecipientStatus.payment_received.value
    recipient.replied_at = utcnow()
    await session.flush()
    return recipient


async def recover_stale_sending(session: AsyncSession, *, stale_after_minutes: int = 10) -> int:
    threshold = utcnow() - timedelta(minutes=stale_after_minutes)
    result = await session.execute(
        update(PayoutRecipient)
        .where(
            PayoutRecipient.status == RecipientStatus.sending.value,
            PayoutRecipient.updated_at < threshold,
        )
        .values(status=RecipientStatus.pending.value, failure_reason=None)
    )
    return int(getattr(result, "rowcount", 0) or 0)


def _session_dialect_name(session: AsyncSession) -> str:
    bind = session.get_bind()
    return bind.dialect.name


async def claim_pending_recipients(session: AsyncSession, *, limit: int) -> list[PayoutRecipient]:
    if limit < 1:
        return []
    dialect_name = _session_dialect_name(session)
    stmt = (
        select(PayoutRecipient.id)
        .join(Payout)
        .where(
            PayoutRecipient.status == RecipientStatus.pending.value,
            Payout.status == PayoutStatus.sending.value,
        )
        .order_by(PayoutRecipient.id.asc())
        .limit(limit)
    )
    if dialect_name != "sqlite":
        stmt = stmt.with_for_update(skip_locked=True)
    ids = [row[0] for row in (await session.execute(stmt)).all()]
    if not ids:
        return []
    await session.execute(
        update(PayoutRecipient)
        .where(PayoutRecipient.id.in_(ids), PayoutRecipient.status == RecipientStatus.pending.value)
        .values(status=RecipientStatus.sending.value)
    )
    rows = await session.scalars(
        select(PayoutRecipient)
        .options(selectinload(PayoutRecipient.user))
        .options(selectinload(PayoutRecipient.payout))
        .where(PayoutRecipient.id.in_(ids))
        .order_by(PayoutRecipient.id.asc())
    )
    return list(rows)


async def format_users_csv(rows: Iterable[tuple[User, PaymentProfile | None]]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "user_id",
            "telegram_id",
            "full_name",
            "is_active",
            "has_payment_profile",
            "raw_payment_details",
        ]
    )
    for user, profile in rows:
        writer.writerow(
            [
                user.id,
                user.telegram_id,
                user.full_name,
                user.is_active,
                bool(profile and profile.raw_payment_details and profile.raw_payment_details.strip()),
                getattr(profile, "raw_payment_details", ""),
            ]
        )
    return buffer.getvalue()


async def format_recipients_csv(session: AsyncSession, payout_id: int) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "recipient_id",
            "payout_id",
            "user_id",
            "telegram_id",
            "full_name",
            "status",
            "sent_at",
            "failed_at",
            "failure_reason",
            "replied_at",
            "paid_at",
            "paid_by_admin_id",
            "raw_reply",
            "raw_payment_details",
        ]
    )
    recipients = await session.scalars(
        select(PayoutRecipient)
        .options(selectinload(PayoutRecipient.user))
        .options(selectinload(PayoutRecipient.payment_replies))
        .where(PayoutRecipient.payout_id == payout_id)
        .order_by(PayoutRecipient.id.asc())
    )
    for recipient in recipients:
        reply = recipient.payment_replies[-1] if recipient.payment_replies else None
        snapshot = recipient.payment_profile_snapshot or {}
        writer.writerow(
            [
                recipient.id,
                recipient.payout_id,
                recipient.user_id,
                recipient.user.telegram_id,
                recipient.user.full_name,
                recipient.status,
                recipient.sent_at or "",
                recipient.failed_at or "",
                recipient.failure_reason or "",
                recipient.replied_at or "",
                recipient.paid_at or "",
                recipient.paid_by_admin_id or "",
                reply.raw_text if reply else "",
                snapshot.get("raw_payment_details", ""),
            ]
        )
    return buffer.getvalue()


async def finalize_payout_after_worker(session: AsyncSession, payout: Payout) -> Payout | None:
    if payout.status in {PayoutStatus.cancelled.value, PayoutStatus.completed.value}:
        return None
    statuses = list(await session.scalars(select(PayoutRecipient.status).where(PayoutRecipient.payout_id == payout.id)))
    if not statuses:
        return None
    if any(status in {RecipientStatus.pending.value, RecipientStatus.sending.value} for status in statuses):
        return None
    next_status = (
        PayoutStatus.partially_failed.value
        if any(status == RecipientStatus.failed.value for status in statuses)
        else PayoutStatus.sent.value
    )
    if payout.status != next_status:
        payout.status = next_status
        await session.flush()
    return payout
