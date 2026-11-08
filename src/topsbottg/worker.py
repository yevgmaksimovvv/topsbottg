from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
from typing import Protocol

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.orm import selectinload

from topsbottg.models import Payout, PayoutRecipient, PayoutStatus, RecipientStatus
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
        # Keep the transaction lock on this session so cancellation and deletion
        # in other processes serialize without consuming another pool connection.
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


async def deliver_ready_notifications(bot: MessageSender, session_factory: async_sessionmaker) -> None:
    async with session_factory() as session:
        rows = await claim_ready_notifications(session)
        notifications = [(row.id, row.telegram_id, row.message) for row in rows]
        await session.commit()
    for message_id, chat_id, message in notifications:
        error: Exception | None = None
        try:
            await bot.send_message(chat_id, message)
        except Exception as exc:  # noqa: BLE001
            error = exc
        async with session_factory() as session:
            await settle_ready_notification(session, message_id, error)
            await session.commit()


async def run_worker(bot: Bot, session_factory: async_sessionmaker, settings, stop_event: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    next_recovery = 0.0
    next_notification = 0.0
    while not stop_event.is_set():
        now = loop.time()
        if now >= next_recovery:
            async with session_factory() as session:
                await recover_stale_sending(session)
                await session.commit()
            next_recovery = loop.time() + 60
        if now >= next_notification:
            await deliver_ready_notifications(bot, session_factory)
            next_notification = loop.time() + 5
        async with session_factory() as session:
            rows = await claim_pending_recipients(session, limit=max(1, int(settings.broadcast_rate_per_second)))
            await session.commit()
        if not rows:
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
