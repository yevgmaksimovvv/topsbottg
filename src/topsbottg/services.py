from __future__ import annotations

import asyncio
import os
import logging
import secrets
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from string import Formatter

from sqlalchemy import event, exists, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session
from sqlalchemy.orm import selectinload

from topsbottg.logging_utils import log_event
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

logger = logging.getLogger(__name__)

ADMIN_EVENTS_TOKEN_TTL_SECONDS = 300
ADMIN_EVENTS_KEEPALIVE_SECONDS = 20
ADMIN_EVENTS_QUEUE_MAXSIZE = 128

ADMIN_EVENT_USERS_CHANGED = "users_changed"
ADMIN_EVENT_PAYOUTS_CHANGED = "payouts_changed"
ADMIN_EVENT_PAYOUT_CHANGED = "payout_changed"
ADMIN_EVENT_PAYOUT_RECIPIENTS_CHANGED = "payout_recipients_changed"
ADMIN_EVENT_PING = "ping"

_ADMIN_EVENT_QUEUE_KEY = "_topsbottg_admin_events"


@dataclass(slots=True)
class AdminEventsToken:
    token: str
    expires_at: datetime


class AdminEventsTokenStore:
    def __init__(self, ttl_seconds: int = ADMIN_EVENTS_TOKEN_TTL_SECONDS) -> None:
        self._ttl_seconds = ttl_seconds
        self._tokens: dict[str, datetime] = {}

    def issue(self) -> AdminEventsToken:
        self._purge_expired()
        token = secrets.token_urlsafe(32)
        expires_at = utcnow() + timedelta(seconds=self._ttl_seconds)
        self._tokens[token] = expires_at
        return AdminEventsToken(token=token, expires_at=expires_at)

    def consume(self, token: str) -> AdminEventsToken | None:
        self._purge_expired()
        expires_at = self._tokens.pop(token, None)
        if expires_at is None:
            return None
        return AdminEventsToken(token=token, expires_at=expires_at)

    def _purge_expired(self) -> None:
        now = utcnow()
        for token, expires_at in list(self._tokens.items()):
            if expires_at <= now:
                self._tokens.pop(token, None)


class AdminEventsBroadcaster:
    def __init__(self, queue_maxsize: int = ADMIN_EVENTS_QUEUE_MAXSIZE) -> None:
        self._queue_maxsize = queue_maxsize
        self._subscribers: set[asyncio.Queue[dict[str, object]]] = set()

    def subscribe(self) -> asyncio.Queue[dict[str, object]]:
        queue: asyncio.Queue[dict[str, object]] = asyncio.Queue(maxsize=self._queue_maxsize)
        self._subscribers.add(queue)
        log_event(
            logger,
            "INFO",
            "admin_events_subscribed",
            "Подписчик admin events добавлен",
            pid=os.getpid(),
            broadcaster_id=id(self),
            queue_id=id(queue),
            subscribers_count_after=len(self._subscribers),
        )
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, object]]) -> None:
        self._subscribers.discard(queue)
        log_event(
            logger,
            "INFO",
            "admin_events_unsubscribed",
            "Подписчик admin events удалён",
            pid=os.getpid(),
            broadcaster_id=id(self),
            queue_id=id(queue),
            subscribers_count_after=len(self._subscribers),
        )

    def publish(self, event_type: str, payload: dict[str, object] | None = None) -> None:
        message = {"event": event_type, "payload": payload or {}}
        queues = list(self._subscribers)
        log_event(
            logger,
            "INFO",
            "admin_event_publish_start",
            "Публикация admin event началась",
            pid=os.getpid(),
            broadcaster_id=id(self),
            event_type=event_type,
            payload=payload or {},
            subscribers_count=len(queues),
            queue_ids=[id(queue) for queue in queues],
            queue_sizes_before=[queue.qsize() for queue in queues],
        )
        queue_sizes_after: list[int] = []
        for queue in queues:
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                log_event(
                    logger,
                    "WARNING",
                    "admin_event_publish_queue_full",
                    "Очередь admin event переполнена",
                    pid=os.getpid(),
                    broadcaster_id=id(self),
                    event_type=event_type,
                    queue_id=id(queue),
                    queue_size=queue.qsize(),
                )
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    queue.put_nowait(message)
                except asyncio.QueueFull:
                    pass
            queue_sizes_after.append(queue.qsize())
        log_event(
            logger,
            "INFO",
            "admin_event_publish_done",
            "Публикация admin event завершена",
            pid=os.getpid(),
            broadcaster_id=id(self),
            event_type=event_type,
            subscribers_count=len(queues),
            queue_sizes_after=queue_sizes_after,
        )


_admin_events_broadcaster = AdminEventsBroadcaster()
_admin_events_token_store = AdminEventsTokenStore()


def configure_admin_events(
    broadcaster: AdminEventsBroadcaster | None = None,
    token_store: AdminEventsTokenStore | None = None,
) -> tuple[AdminEventsBroadcaster, AdminEventsTokenStore]:
    global _admin_events_broadcaster, _admin_events_token_store
    _admin_events_broadcaster = broadcaster or AdminEventsBroadcaster()
    _admin_events_token_store = token_store or AdminEventsTokenStore()
    return _admin_events_broadcaster, _admin_events_token_store


def get_admin_events_broadcaster() -> AdminEventsBroadcaster:
    return _admin_events_broadcaster


def get_admin_events_token_store() -> AdminEventsTokenStore:
    return _admin_events_token_store


def issue_admin_events_token() -> AdminEventsToken:
    return _admin_events_token_store.issue()


def consume_admin_events_token(token: str) -> AdminEventsToken | None:
    return _admin_events_token_store.consume(token)


def publish_admin_event(event_type: str, payload: dict[str, object] | None = None) -> None:
    _admin_events_broadcaster.publish(event_type, payload)


def queue_admin_event(session: AsyncSession, event_type: str, payload: dict[str, object] | None = None) -> None:
    queued = session.info.setdefault(_ADMIN_EVENT_QUEUE_KEY, [])
    queued.append({"event_type": event_type, "payload": payload or {}})
    log_event(
        logger,
        "INFO",
        "admin_event_queued",
        "Admin event поставлен в очередь session.info",
        pid=os.getpid(),
        broadcaster_id=id(get_admin_events_broadcaster()),
        session_id=id(session.sync_session),
        event_type=event_type,
        payload=payload or {},
        queue_len_after=len(queued),
    )


def queue_admin_events(session: AsyncSession, events: Sequence[tuple[str, dict[str, object] | None]]) -> None:
    for event_type, payload in events:
        queue_admin_event(session, event_type, payload)


def _flush_admin_events(sync_session: Session) -> None:
    queued = sync_session.info.pop(_ADMIN_EVENT_QUEUE_KEY, [])
    log_event(
        logger,
        "INFO",
        "admin_event_flushing",
        "Admin events flush started",
        pid=os.getpid(),
        session_id=id(sync_session),
        events_count=len(queued),
    )
    for event in queued:
        log_event(
            logger,
            "INFO",
            "admin_event_flush_item",
            "Admin event извлекается из очереди session.info",
            pid=os.getpid(),
            session_id=id(sync_session),
            event_type=event["event_type"],
            payload=event["payload"],
        )
        publish_admin_event(event["event_type"], event["payload"])


@event.listens_for(Session, "after_commit")
def _after_commit(sync_session: Session) -> None:
    queued = sync_session.info.get(_ADMIN_EVENT_QUEUE_KEY, [])
    log_event(
        logger,
        "INFO",
        "admin_event_after_commit",
        "after_commit для admin events",
        pid=os.getpid(),
        session_id=id(sync_session),
        events_count=len(queued),
    )
    _flush_admin_events(sync_session)


@event.listens_for(Session, "after_rollback")
def _after_rollback(sync_session: Session) -> None:
    sync_session.info.pop(_ADMIN_EVENT_QUEUE_KEY, None)

DEFAULT_MESSAGE_TEMPLATE = """Всем привет!
Выплачиваем ЗАРПЛАТУ за работу в период {period_label}.

Для получения выплаты проверьте или заполните платежные данные.

Если перевод на ваши данные:
укажите фамилию и имя, номер телефона для перевода / СБП, банк.

Если перевод на чужие данные:
укажите ваши фамилию и имя, имя владельца, номер телефона владельца, банк."""

ACTIVATED_PAYOUT_STATUSES = {
    PayoutStatus.sending.value,
    PayoutStatus.sent.value,
    PayoutStatus.partially_failed.value,
}

PAYMENT_RECEIPT_RECIPIENT_STATUSES = {
    RecipientStatus.sent.value,
    RecipientStatus.payment_required.value,
}

_PAYOUT_PERIOD_MONTH_MAX_DAYS = {
    1: 31,
    2: 29,
    3: 31,
    4: 30,
    5: 31,
    6: 30,
    7: 31,
    8: 31,
    9: 30,
    10: 31,
    11: 30,
    12: 31,
}


def utcnow() -> datetime:
    return datetime.now(UTC)


def payment_profile_snapshot(profile: PaymentProfile) -> dict[str, str | None]:
    return {
        "raw_payment_details": profile.raw_payment_details,
    }


def format_payout_period_label(
    start_day: int,
    start_month: int,
    end_day: int,
    end_month: int,
) -> str:
    return f"{start_day:02d}.{start_month:02d} — {end_day:02d}.{end_month:02d}"


def validate_payout_period_parts(
    period_start_day: int,
    period_start_month: int,
    period_end_day: int,
    period_end_month: int,
) -> None:
    def _validate_part(day: int, month: int, *, side: str) -> None:
        if not 1 <= month <= 12:
            raise ValueError(f"Месяц {side} периода должен быть от 1 до 12.")
        if not 1 <= day <= 31:
            raise ValueError(f"День {side} периода должен быть от 1 до 31.")
        max_day = _PAYOUT_PERIOD_MONTH_MAX_DAYS[month]
        if day > max_day:
            raise ValueError(f"Для {side} периода указана невозможная дата.")

    _validate_part(period_start_day, period_start_month, side="начала")
    _validate_part(period_end_day, period_end_month, side="окончания")


def render_message_template(payout: Payout) -> str:
    return payout.message_template.format(
        period_label=format_payout_period_label(
            payout.period_start_day,
            payout.period_start_month,
            payout.period_end_day,
            payout.period_end_month,
        ),
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
        raise ValueError("Шаблон сообщения некорректен.") from exc
    allowed_fields = {"period_start", "period_end", "period_label"}
    for _literal_text, field_name, format_spec, conversion in parts:
        if field_name is None:
            continue
        if field_name not in allowed_fields:
            raise ValueError(
                "В шаблоне можно использовать только {period_start}, {period_end} и {period_label}."
            )
        if format_spec or conversion:
            raise ValueError("Плейсхолдеры шаблона не должны использовать форматирование.")


def _log_payout_action(
    event: str,
    message: str,
    *,
    action: str,
    payout_id: int | None = None,
    actor_telegram_id: int | None = None,
    admin_id: int | None = None,
    recipients_count: int | None = None,
    result_status: str | None = None,
    result_count: int | None = None,
    reason: str | None = None,
    error_type: str | None = None,
) -> None:
    log_event(
        logger,
        "INFO" if event.endswith("_requested") or event.endswith("_completed") else "ERROR",
        event,
        message,
        action=action,
        payout_id=payout_id,
        actor_telegram_id=actor_telegram_id,
        admin_id=admin_id,
        recipients_count=recipients_count,
        result_status=result_status,
        result_count=result_count,
        reason=reason,
        error_type=error_type,
    )


async def log_audit(
    session: AsyncSession,
    *,
    actor_telegram_id: int,
    action: str,
    entity_type: str,
    entity_id: str,
    metadata: dict | None = None,
) -> None:
    try:
        session.add(
            AuditLog(
                actor_telegram_id=actor_telegram_id,
                action=action,
                entity_type=entity_type,
                entity_id=entity_id,
                metadata=metadata,
            )
        )
    except Exception as exc:
        log_event(
            logger,
            "ERROR",
            "audit_log_write_failed",
            "Не удалось записать audit log",
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            error_type=type(exc).__name__,
        )
        raise


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
    log_event(
        logger,
        "INFO",
        "user_profile_upsert_requested",
        "Запрошено обновление профиля пользователя",
        telegram_user_id=telegram_id,
    )
    try:
        user = await get_or_create_user(session, telegram_id, full_name=full_name)
        user.full_name = full_name
        await session.flush()
    except Exception as exc:
        log_event(
            logger,
            "ERROR",
            "user_profile_upsert_failed",
            "Не удалось обновить профиль пользователя",
            telegram_user_id=telegram_id,
            error_type=type(exc).__name__,
        )
        raise
    log_event(
        logger,
        "INFO",
        "user_profile_upsert_completed",
        "Профиль пользователя обновлён",
        telegram_user_id=telegram_id,
        user_id=user.id,
    )
    queue_admin_event(session, ADMIN_EVENT_USERS_CHANGED, {"user_id": user.id, "telegram_user_id": telegram_id})
    return user


async def upsert_payment_profile(session: AsyncSession, user: User, payload: dict | str) -> PaymentProfile:
    log_event(
        logger,
        "INFO",
        "payment_details_upsert_requested",
        "Запрошено обновление платёжных данных",
        telegram_user_id=user.telegram_id,
        user_id=user.id,
    )
    try:
        raw_payment_details = _normalize_raw_payment_details(payload)
        profile = await session.scalar(select(PaymentProfile).where(PaymentProfile.user_id == user.id))
        if profile is None:
            profile = PaymentProfile(user_id=user.id, raw_payment_details=raw_payment_details)
            session.add(profile)
        else:
            profile.raw_payment_details = raw_payment_details
            profile.deleted_at = None
        await session.flush()
    except Exception as exc:
        log_event(
            logger,
            "ERROR",
            "payment_details_upsert_failed",
            "Не удалось обновить платёжные данные",
            telegram_user_id=user.telegram_id,
            user_id=user.id,
            error_type=type(exc).__name__,
        )
        raise
    log_event(
        logger,
        "INFO",
        "payment_details_upsert_completed",
        "Платёжные данные обновлены",
        telegram_user_id=user.telegram_id,
        user_id=user.id,
    )
    queue_admin_event(session, ADMIN_EVENT_USERS_CHANGED, {"user_id": user.id, "telegram_user_id": user.telegram_id})
    return profile


async def soft_delete_payment_profile(session: AsyncSession, user: User) -> None:
    profile = await session.scalar(select(PaymentProfile).where(PaymentProfile.user_id == user.id))
    if profile:
        profile.deleted_at = utcnow()
        await session.flush()
        queue_admin_event(session, ADMIN_EVENT_USERS_CHANGED, {"user_id": user.id, "telegram_user_id": user.telegram_id})


async def list_users(
    session: AsyncSession,
    *,
    search: str | None = None,
    has_payment_profile: bool | None = None,
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
    stmt = select(User.id).order_by(func.lower(User.full_name), User.id)
    if search:
        needle = search.strip()
        if needle:
            if dialect_name == "sqlite":
                stmt = stmt.where(User.full_name.like(f"%{needle}%"))
            else:
                stmt = stmt.where(func.lower(User.full_name).like(f"%{needle.lower()}%"))
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
    _log_payout_action(
        "payout_action_requested",
        "Запрошено создание выплаты",
        action="create_payout",
        actor_telegram_id=actor_telegram_id,
    )
    try:
        message_template = payload.get("message_template") or DEFAULT_MESSAGE_TEMPLATE
        validate_message_template(message_template)
        period_start_day = payload["period_start_day"]
        period_start_month = payload["period_start_month"]
        period_end_day = payload["period_end_day"]
        period_end_month = payload["period_end_month"]
        validate_payout_period_parts(period_start_day, period_start_month, period_end_day, period_end_month)
        payout = Payout(
            period_start_day=period_start_day,
            period_start_month=period_start_month,
            period_end_day=period_end_day,
            period_end_month=period_end_month,
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
    except Exception as exc:
        _log_payout_action(
            "payout_action_failed",
            "Не удалось создать выплату",
            action="create_payout",
            actor_telegram_id=actor_telegram_id,
            reason="validation_or_db_error",
            error_type=type(exc).__name__,
        )
        raise
    _log_payout_action(
        "payout_action_completed",
        "Выплата создана",
        action="create_payout",
        payout_id=payout.id,
        actor_telegram_id=actor_telegram_id,
    )
    queue_admin_event(session, ADMIN_EVENT_PAYOUTS_CHANGED, {"payout_id": payout.id, "reason": "create_payout"})
    return payout


async def update_payout(session: AsyncSession, payout: Payout, *, actor_telegram_id: int, payload: dict) -> Payout:
    _log_payout_action(
        "payout_action_requested",
        "Запрошено обновление выплаты",
        action="update_payout",
        payout_id=payout.id,
        actor_telegram_id=actor_telegram_id,
    )
    try:
        payload_message_template = payload.get("message_template")
        next_message_template = (
            payout.message_template if payload_message_template is None else payload_message_template
        )
        if next_message_template is not None:
            validate_message_template(next_message_template)
        next_period_start_day = payload.get("period_start_day", payout.period_start_day)
        next_period_start_month = payload.get("period_start_month", payout.period_start_month)
        next_period_end_day = payload.get("period_end_day", payout.period_end_day)
        next_period_end_month = payload.get("period_end_month", payout.period_end_month)
        validate_payout_period_parts(
            next_period_start_day,
            next_period_start_month,
            next_period_end_day,
            next_period_end_month,
        )
        payout.period_start_day = next_period_start_day
        payout.period_start_month = next_period_start_month
        payout.period_end_day = next_period_end_day
        payout.period_end_month = next_period_end_month
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
    except Exception as exc:
        _log_payout_action(
            "payout_action_failed",
            "Не удалось обновить выплату",
            action="update_payout",
            payout_id=payout.id,
            actor_telegram_id=actor_telegram_id,
            reason="validation_or_db_error",
            error_type=type(exc).__name__,
        )
        raise
    _log_payout_action(
        "payout_action_completed",
        "Выплата обновлена",
        action="update_payout",
        payout_id=payout.id,
        actor_telegram_id=actor_telegram_id,
    )
    return payout


async def set_payout_sending(session: AsyncSession, payout: Payout, *, actor_telegram_id: int) -> Payout:
    _log_payout_action(
        "payout_action_requested",
        "Запрошен перевод выплаты в sending",
        action="send_payout",
        payout_id=payout.id,
        actor_telegram_id=actor_telegram_id,
    )
    try:
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
    except Exception as exc:
        _log_payout_action(
            "payout_action_failed",
            "Не удалось перевести выплату в sending",
            action="send_payout",
            payout_id=payout.id,
            actor_telegram_id=actor_telegram_id,
            reason="validation_or_db_error",
            error_type=type(exc).__name__,
        )
        raise
    _log_payout_action(
        "payout_action_completed",
        "Выплата переведена в sending",
        action="send_payout",
        payout_id=payout.id,
        actor_telegram_id=actor_telegram_id,
        result_status=payout.status,
    )
    queue_admin_events(
        session,
        [
            (ADMIN_EVENT_PAYOUT_CHANGED, {"payout_id": payout.id, "reason": "set_payout_sending"}),
            (ADMIN_EVENT_PAYOUTS_CHANGED, {"payout_id": payout.id, "reason": "set_payout_sending"}),
        ],
    )
    return payout


async def complete_payout(session: AsyncSession, payout: Payout, *, actor_telegram_id: int) -> Payout:
    _log_payout_action(
        "payout_action_requested",
        "Запрошено закрытие выплаты",
        action="complete_payout",
        payout_id=payout.id,
        actor_telegram_id=actor_telegram_id,
    )
    try:
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
    except Exception as exc:
        _log_payout_action(
            "payout_action_failed",
            "Не удалось закрыть выплату",
            action="complete_payout",
            payout_id=payout.id,
            actor_telegram_id=actor_telegram_id,
            reason="validation_or_db_error",
            error_type=type(exc).__name__,
        )
        raise
    _log_payout_action(
        "payout_action_completed",
        "Выплата закрыта",
        action="complete_payout",
        payout_id=payout.id,
        actor_telegram_id=actor_telegram_id,
        result_status=payout.status,
    )
    queue_admin_events(
        session,
        [
            (ADMIN_EVENT_PAYOUT_CHANGED, {"payout_id": payout.id, "reason": "complete_payout"}),
            (ADMIN_EVENT_PAYOUTS_CHANGED, {"payout_id": payout.id, "reason": "complete_payout"}),
        ],
    )
    return payout


async def cancel_payout(session: AsyncSession, payout: Payout, *, actor_telegram_id: int) -> Payout:
    _log_payout_action(
        "payout_action_requested",
        "Запрошена отмена выплаты",
        action="cancel_payout",
        payout_id=payout.id,
        actor_telegram_id=actor_telegram_id,
    )
    try:
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
    except Exception as exc:
        _log_payout_action(
            "payout_action_failed",
            "Не удалось отменить выплату",
            action="cancel_payout",
            payout_id=payout.id,
            actor_telegram_id=actor_telegram_id,
            reason="validation_or_db_error",
            error_type=type(exc).__name__,
        )
        raise
    _log_payout_action(
        "payout_action_completed",
        "Выплата отменена",
        action="cancel_payout",
        payout_id=payout.id,
        actor_telegram_id=actor_telegram_id,
        result_status=payout.status,
    )
    queue_admin_events(
        session,
        [
            (ADMIN_EVENT_PAYOUT_CHANGED, {"payout_id": payout.id, "reason": "cancel_payout"}),
            (ADMIN_EVENT_PAYOUTS_CHANGED, {"payout_id": payout.id, "reason": "cancel_payout"}),
        ],
    )
    return payout


async def add_recipients(session: AsyncSession, payout: Payout, user_ids: Sequence[int]) -> list[PayoutRecipient]:
    _log_payout_action(
        "payout_action_requested",
        "Запрошено добавление получателей",
        action="add_recipients",
        payout_id=payout.id,
        recipients_count=len(user_ids),
    )
    try:
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
    except Exception as exc:
        _log_payout_action(
            "payout_action_failed",
            "Не удалось добавить получателей",
            action="add_recipients",
            payout_id=payout.id,
            recipients_count=len(user_ids),
            reason="validation_or_db_error",
            error_type=type(exc).__name__,
        )
        raise
    _log_payout_action(
        "payout_action_completed",
        "Получатели добавлены",
        action="add_recipients",
        payout_id=payout.id,
        recipients_count=len(created),
        result_count=len(created),
    )
    if created:
        queue_admin_events(
            session,
            [
                (
                    ADMIN_EVENT_PAYOUT_RECIPIENTS_CHANGED,
                    {"payout_id": payout.id, "reason": "add_recipients"},
                ),
                (ADMIN_EVENT_PAYOUTS_CHANGED, {"payout_id": payout.id, "reason": "add_recipients"}),
            ],
        )
    return created


async def cancel_recipient(
    session: AsyncSession, recipient: PayoutRecipient, *, actor_telegram_id: int
) -> PayoutRecipient:
    _log_payout_action(
        "payout_action_requested",
        "Запрошена отмена получателя",
        action="cancel_recipient",
        payout_id=recipient.payout_id,
        actor_telegram_id=actor_telegram_id,
    )
    try:
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
    except Exception as exc:
        _log_payout_action(
            "payout_action_failed",
            "Не удалось отменить получателя",
            action="cancel_recipient",
            payout_id=recipient.payout_id,
            actor_telegram_id=actor_telegram_id,
            reason="validation_or_db_error",
            error_type=type(exc).__name__,
        )
        raise
    _log_payout_action(
        "payout_action_completed",
        "Получатель отменён",
        action="cancel_recipient",
        payout_id=recipient.payout_id,
        actor_telegram_id=actor_telegram_id,
        result_status=recipient.status,
    )
    queue_admin_event(
        session,
        ADMIN_EVENT_PAYOUT_RECIPIENTS_CHANGED,
        {"payout_id": recipient.payout_id, "reason": "cancel_recipient"},
    )
    return recipient


async def retry_failed_recipient(
    session: AsyncSession, recipient: PayoutRecipient, *, actor_telegram_id: int
) -> PayoutRecipient:
    _log_payout_action(
        "payout_action_requested",
        "Запрошен повторный запуск получателя",
        action="retry_failed_recipient",
        payout_id=recipient.payout_id,
        actor_telegram_id=actor_telegram_id,
    )
    try:
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
    except Exception as exc:
        _log_payout_action(
            "payout_action_failed",
            "Не удалось повторно запустить получателя",
            action="retry_failed_recipient",
            payout_id=recipient.payout_id,
            actor_telegram_id=actor_telegram_id,
            reason="validation_or_db_error",
            error_type=type(exc).__name__,
        )
        raise
    _log_payout_action(
        "payout_action_completed",
        "Получатель повторно запущен",
        action="retry_failed_recipient",
        payout_id=recipient.payout_id,
        actor_telegram_id=actor_telegram_id,
        result_status=recipient.status,
    )
    queue_admin_event(
        session,
        ADMIN_EVENT_PAYOUT_RECIPIENTS_CHANGED,
        {"payout_id": recipient.payout_id, "reason": "retry_failed_recipient"},
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
    _log_payout_action(
        "payout_action_requested",
        "Запрошена отметка выплаты получателю",
        action="recipient_marked_paid",
        payout_id=recipient.payout_id,
        actor_telegram_id=actor_telegram_id,
    )
    try:
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
    except Exception as exc:
        _log_payout_action(
            "payout_action_failed",
            "Не удалось отметить выплату получателю",
            action="recipient_marked_paid",
            payout_id=recipient.payout_id,
            actor_telegram_id=actor_telegram_id,
            reason="validation_or_db_error",
            error_type=type(exc).__name__,
        )
        raise
    _log_payout_action(
        "payout_action_completed",
        "Выплата получателю отмечена",
        action="recipient_marked_paid",
        payout_id=recipient.payout_id,
        actor_telegram_id=actor_telegram_id,
        result_status=recipient.status,
    )
    queue_admin_event(
        session,
        ADMIN_EVENT_PAYOUT_RECIPIENTS_CHANGED,
        {"payout_id": recipient.payout_id, "reason": "mark_paid"},
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
    queue_admin_events(
        session,
        [
            (ADMIN_EVENT_USERS_CHANGED, {"user_id": user.id, "telegram_user_id": user.telegram_id}),
            (
                ADMIN_EVENT_PAYOUT_RECIPIENTS_CHANGED,
                {"payout_id": getattr(active_recipient, "payout_id", None), "reason": "save_profile_and_snapshot"},
            ),
        ],
    )
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
    queue_admin_event(
        session,
        ADMIN_EVENT_PAYOUT_RECIPIENTS_CHANGED,
        {"payout_id": recipient.payout_id, "reason": "confirm_saved_profile"},
    )
    return recipient


async def recover_stale_sending(session: AsyncSession, *, stale_after_minutes: int = 10) -> int:
    threshold = utcnow() - timedelta(minutes=stale_after_minutes)
    impacted_payout_ids = list(
        await session.scalars(
            select(PayoutRecipient.payout_id)
            .where(
                PayoutRecipient.status == RecipientStatus.sending.value,
                PayoutRecipient.updated_at < threshold,
            )
            .distinct()
        )
    )
    result = await session.execute(
        update(PayoutRecipient)
        .where(
            PayoutRecipient.status == RecipientStatus.sending.value,
            PayoutRecipient.updated_at < threshold,
        )
        .values(status=RecipientStatus.pending.value, failure_reason=None)
    )
    for payout_id in impacted_payout_ids:
        queue_admin_event(
            session,
            ADMIN_EVENT_PAYOUT_RECIPIENTS_CHANGED,
            {"payout_id": payout_id, "reason": "recover_stale_sending"},
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
        queue_admin_events(
            session,
            [
                (ADMIN_EVENT_PAYOUT_CHANGED, {"payout_id": payout.id, "reason": "finalize_payout_after_worker"}),
                (ADMIN_EVENT_PAYOUTS_CHANGED, {"payout_id": payout.id, "reason": "finalize_payout_after_worker"}),
            ],
        )
    return payout
