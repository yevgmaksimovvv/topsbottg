from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol

from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
    TelegramUnauthorizedError,
)
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ReplyParameters
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.orm import selectinload

from topsbottg.access import get_principal
from topsbottg.chat_outbox import claim_chat_outbox, settle_chat_outbox
from topsbottg.models import (
    AdminInvitation,
    Payout,
    PayoutReadyNotification,
    PayoutRecipient,
    PayoutStatus,
    RecipientStatus,
)
from topsbottg.services import (
    ADMIN_EVENT_PAYOUT_RECIPIENTS_CHANGED,
    claim_pending_recipients,
    claim_ready_notifications,
    finalize_payout_after_worker,
    get_payment_profile,
    payout_send_lock,
    queue_admin_event,
    recover_stale_sending,
    render_message_template,
    settle_ready_notification,
)

MAX_TELEGRAM_MESSAGE_LENGTH = 4096


class MessageSender(Protocol):
    async def send_message(
        self,
        chat_id: int | str,
        text: str,
        reply_markup=None,
    ) -> object: ...


def _recipient_keyboard(recipient_id: int, *, has_profile: bool) -> InlineKeyboardMarkup:
    if has_profile:
        buttons = [
            [
                InlineKeyboardButton(text="Подтвердить данные", callback_data=f"confirm_profile:{recipient_id}"),
                InlineKeyboardButton(text="Изменить данные", callback_data=f"fill_payment:{recipient_id}"),
            ]
        ]
    else:
        buttons = [[InlineKeyboardButton(text="Заполнить данные", callback_data=f"fill_payment:{recipient_id}")]]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def process_recipient(
    bot: MessageSender, session_factory: async_sessionmaker, settings, recipient_id: int
) -> None:
    async with session_factory() as session:
        payout_id = await session.scalar(select(PayoutRecipient.payout_id).where(PayoutRecipient.id == recipient_id))
        if payout_id is None:
            return
        # Держим блокировку в сессии отправки: отмена и удаление в других процессах ждут её освобождения.
        async with payout_send_lock(payout_id, session):
            recipient = await session.scalar(
                select(PayoutRecipient)
                .options(selectinload(PayoutRecipient.user))
                .options(selectinload(PayoutRecipient.payout))
                .where(PayoutRecipient.id == recipient_id)
            )
            if recipient is None or recipient.status != RecipientStatus.sending.value:
                return
            payout = recipient.payout
            if payout.deleted_at is not None or payout.status in {
                PayoutStatus.cancelled.value,
                PayoutStatus.completed.value,
            }:
                recipient.status = RecipientStatus.cancelled.value
                return
            profile = await get_payment_profile(session, recipient.user_id)
            has_profile = profile is not None and profile.deleted_at is None
            keyboard = _recipient_keyboard(recipient.id, has_profile=has_profile)
            chat_id = recipient.user.telegram_id
            try:
                text = render_message_template(payout)
                if has_profile and profile is not None and profile.raw_payment_details is not None:
                    text += "\n\nПлатёжные данные:\n" + profile.raw_payment_details
                if len(text) > MAX_TELEGRAM_MESSAGE_LENGTH:
                    raise ValueError(f"Telegram message exceeds the {MAX_TELEGRAM_MESSAGE_LENGTH}-character limit.")
                error: Exception | None = None
            except Exception as exc:  # noqa: BLE001
                error = exc
            if error is None:
                try:
                    await bot.send_message(chat_id, text, reply_markup=keyboard)
                except Exception as exc:  # noqa: BLE001
                    error = exc
            updated = await session.get(PayoutRecipient, recipient_id)
            if updated is None or updated.status != RecipientStatus.sending.value:
                return
            if error is None:
                updated.status = RecipientStatus.sent.value if has_profile else RecipientStatus.payment_required.value
                updated.sent_at = datetime.now(UTC)
                reason = "worker_sent" if has_profile else "worker_payment_required"
            else:
                updated.status = RecipientStatus.failed.value
                updated.failed_at = datetime.now(UTC)
                updated.failure_reason = str(error)[:250]
                reason = "worker_failed"
            queue_admin_event(
                session, ADMIN_EVENT_PAYOUT_RECIPIENTS_CHANGED, {"payout_id": payout_id, "reason": reason}
            )


async def deliver_ready_notifications(bot: MessageSender, session_factory: async_sessionmaker, settings) -> None:
    async with session_factory() as session:
        rows = await claim_ready_notifications(session)
        notifications = [(row.id, row.telegram_id, row.message) for row in rows]
        await session.commit()
    for message_id, chat_id, message in notifications:
        await deliver_ready_notification(bot, session_factory, message_id, chat_id, message, settings)


async def deliver_ready_notification(
    bot: MessageSender, session_factory: async_sessionmaker, message_id: int, chat_id: int, message: str, settings
) -> None:
    async with session_factory() as session:
        notification = await session.get(PayoutReadyNotification, message_id)
        if notification is None:
            return
        async with payout_send_lock(notification.payout_id, session):
            notification = await session.scalar(
                select(PayoutReadyNotification)
                .where(PayoutReadyNotification.id == message_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            payout = (
                await session.scalar(
                    select(Payout)
                    .where(Payout.id == notification.payout_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
                if notification is not None
                else None
            )
            if (
                notification is None
                or notification.delivered_at is not None
                or notification.discarded_at is not None
            ):
                return
            statuses = list(
                await session.scalars(
                    select(PayoutRecipient.status).where(PayoutRecipient.payout_id == notification.payout_id)
                )
            )
            active = [status for status in statuses if status != RecipientStatus.cancelled.value]
            ready = (
                payout is not None
                and payout.deleted_at is None
                and payout.status in {PayoutStatus.sent.value, PayoutStatus.partially_failed.value}
                and active
                and RecipientStatus.payment_received.value in active
                and all(
                    status in {RecipientStatus.payment_received.value, RecipientStatus.paid.value} for status in active
                )
            )
            if not ready:
                await settle_ready_notification(session, message_id, discard=True)
                return
            # Перед отправкой проверяем, что адресату не отозвали доступ.
            if not await get_principal(session, notification.telegram_id, settings.admin_ids_set):
                await settle_ready_notification(session, message_id, discard=True)
                return
            error: Exception | None = None
            try:
                await bot.send_message(chat_id, message)
            except Exception as exc:  # noqa: BLE001
                error = exc
            await settle_ready_notification(session, message_id, error)


def _admin_invitation_keyboard(invitation_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text="Принять", callback_data=f"admin_invite:accept:{invitation_id}"
            ),
            InlineKeyboardButton(
                text="Отклонить", callback_data=f"admin_invite:decline:{invitation_id}"
            ),
        ]]
    )


async def deliver_admin_invitations(bot: MessageSender, session_factory: async_sessionmaker) -> None:
    """Резервируем отправку до Telegram API. Неопределённые результаты не повторяем."""
    now = datetime.now(UTC)
    async with session_factory() as session:
        await session.execute(
            update(AdminInvitation)
            .where(AdminInvitation.status == "pending", AdminInvitation.expires_at <= now)
            .values(status="expired", decided_at=now)
        )
        # Незавершённый вызов Telegram мог доставить сообщение. Повтор запрещён.
        await session.execute(
            update(AdminInvitation)
            .where(
                AdminInvitation.status == "pending",
                AdminInvitation.delivery_state == "sending",
                AdminInvitation.updated_at < now - timedelta(minutes=2),
            )
            .values(delivery_state="unknown", last_delivery_error="delivery_outcome_unknown")
        )
        await session.commit()

    for _ in range(10):
        async with session_factory() as session:
            now = datetime.now(UTC)
            invitation = await session.scalar(
                select(AdminInvitation)
                .where(
                    AdminInvitation.status == "pending",
                    AdminInvitation.delivery_state == "queued",
                    AdminInvitation.expires_at > now,
                )
                .order_by(AdminInvitation.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if invitation is None:
                break
            invitation.delivery_state = "sending"
            invitation_id = invitation.id
            target_id = invitation.target_telegram_id
            await session.commit()

        error: Exception | None = None
        try:
            await bot.send_message(
                target_id,
                "Вас приглашают стать администратором TOPSBOT. Подтвердите или отклоните приглашение.",
                reply_markup=_admin_invitation_keyboard(invitation_id),
            )
        except Exception as exc:  # noqa: BLE001
            error = exc

        async with session_factory() as session:
            invitation = await session.scalar(
                select(AdminInvitation)
                .where(AdminInvitation.id == invitation_id)
                .with_for_update()
            )
            if invitation is None or invitation.delivery_state != "sending":
                continue
            if error is None:
                invitation.delivery_state = "delivered"
                invitation.delivered_at = datetime.now(UTC)
                invitation.last_delivery_error = None
            else:
                # Ошибка сети не доказывает, что Telegram не доставил сообщение.
                invitation.delivery_state = "unknown"
                invitation.last_delivery_error = type(error).__name__[:80]
            await session.commit()


CHAT_SEND_TIMEOUT_SECONDS = 20


def _chat_delivery_error(exc: Exception) -> tuple[str, bool, float | None]:
    if isinstance(exc, TelegramRetryAfter):
        return "rate_limited", False, float(exc.retry_after)
    if isinstance(exc, TelegramForbiddenError):
        return "blocked", True, None
    if isinstance(exc, TelegramBadRequest):
        if "chat not found" in exc.message.lower():
            return "chat_not_found", True, None
        return "bad_request", True, None
    if isinstance(exc, TelegramUnauthorizedError):
        return "telegram_unauthorized", True, None
    if isinstance(exc, TelegramServerError):
        return "telegram_server_error", False, None
    if isinstance(exc, TelegramNetworkError):
        return "network_error", False, None
    if isinstance(exc, TimeoutError):
        return "timeout", False, None
    return "send_error", False, None


async def deliver_chat_outbox(bot: MessageSender, session_factory: async_sessionmaker) -> None:
    async with session_factory() as session:
        claims = await claim_chat_outbox(session, limit=1)
        await session.commit()

    for claim in claims:
        if claim.reply_to_message_id is not None and claim.reply_to_telegram_message_id is None:
            async with session_factory() as session:
                await settle_chat_outbox(
                    session, claim,
                    error_code="reply_unavailable" if claim.reply_target_failed else "reply_pending",
                    permanent=claim.reply_target_failed,
                    retry_after=10 if not claim.reply_target_failed else None,
                )
                await session.commit()
            continue

        error_code: str | None = None
        retry_after: float | None = None
        permanent = False
        telegram_message_id: int | None = None
        try:
            kwargs = (
                {"reply_parameters": ReplyParameters(message_id=claim.reply_to_telegram_message_id)}
                if claim.reply_to_telegram_message_id is not None else {}
            )
            # Telegram has no client-supplied idempotency token. A crash after
            # Telegram accepts the message but before "sent" commits can resend it.
            response = await asyncio.wait_for(
                bot.send_message(claim.chat_id, claim.text, **kwargs),
                timeout=CHAT_SEND_TIMEOUT_SECONDS,
            )
            telegram_message_id = getattr(response, "message_id", None)
            if type(telegram_message_id) is not int or telegram_message_id <= 0:
                telegram_message_id = None
                error_code = "missing_telegram_message_id"
        except Exception as exc:  # noqa: BLE001
            error_code, permanent, retry_after = _chat_delivery_error(exc)

        async with session_factory() as session:
            await settle_chat_outbox(
                session, claim,
                telegram_message_id=telegram_message_id,
                error_code=error_code,
                retry_after=retry_after,
                permanent=permanent,
            )
            await session.commit()


async def run_worker(
    bot: Bot, session_factory: async_sessionmaker, settings, stop_event: asyncio.Event,
    heartbeat: Callable[[float], None] | None = None,
) -> None:
    loop = asyncio.get_running_loop()
    next_recovery = 0.0
    next_notification = 0.0
    next_invitations = 0.0
    next_chat = 0.0
    while not stop_event.is_set():
        now = loop.time()
        if now >= next_recovery:
            async with session_factory() as session:
                await recover_stale_sending(session)
                await session.commit()
            next_recovery = loop.time() + 60
        if now >= next_notification:
            await deliver_ready_notifications(bot, session_factory, settings)
            next_notification = loop.time() + 5
        if now >= next_invitations:
            await deliver_admin_invitations(bot, session_factory)
            next_invitations = loop.time() + 5
        if now >= next_chat:
            await deliver_chat_outbox(bot, session_factory)
            next_chat = loop.time() + 1
        async with session_factory() as session:
            rows = await claim_pending_recipients(session, limit=max(1, int(settings.broadcast_rate_per_second)))
            await session.commit()
        if not rows:
            if heartbeat is not None:
                heartbeat(loop.time())
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop_event.wait(), timeout=1)
            continue
        payout_ids = {row.payout_id for row in rows}
        for row in rows:
            if stop_event.is_set():
                break
            await process_recipient(bot, session_factory, settings, row.id)
            await asyncio.sleep(1 / max(1, int(settings.broadcast_rate_per_second)))
        async with session_factory() as session:
            payouts = await session.scalars(
                select(Payout).where(
                    Payout.id.in_(payout_ids),
                    Payout.deleted_at.is_(None),
                    Payout.status.in_(
                        [PayoutStatus.sending.value, PayoutStatus.sent.value, PayoutStatus.partially_failed.value]
                    ),
                )
            )
            for payout in payouts:
                await finalize_payout_after_worker(session, payout)
            await session.commit()
        if heartbeat is not None:
            heartbeat(loop.time())
