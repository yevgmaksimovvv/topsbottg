from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Protocol

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.orm import selectinload

from topsbottg.models import Payout, PayoutRecipient, PayoutStatus, RecipientStatus
from topsbottg.services import (
    claim_pending_recipients,
    finalize_payout_after_worker,
    get_payment_profile,
    recover_stale_sending,
    render_message_template,
)


class MessageSender(Protocol):
    async def send_message(
        self,
        chat_id: int | str,
        text: str,
        reply_markup=None,
    ) -> object: ...


def _recipient_keyboard(recipient_id: int, *, has_profile: bool) -> InlineKeyboardMarkup:
    buttons = [[InlineKeyboardButton(text="Заполнить / изменить данные", callback_data=f"fill_payment:{recipient_id}")]]
    if has_profile:
        buttons.append(
            [
                InlineKeyboardButton(
                    text="Подтвердить сохраненные данные", callback_data=f"confirm_profile:{recipient_id}"
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def process_recipient(
    bot: MessageSender,
    session_factory: async_sessionmaker,
    settings,
    recipient_id: int,
) -> None:
    async with session_factory() as session:
        recipient = await session.scalar(
            select(PayoutRecipient)
            .options(selectinload(PayoutRecipient.user))
            .options(selectinload(PayoutRecipient.payout))
            .where(PayoutRecipient.id == recipient_id)
        )
        if recipient is None or recipient.status != RecipientStatus.sending.value:
            return
        payout = recipient.payout
        user = recipient.user
        profile = await get_payment_profile(session, recipient.user_id)
        has_profile = profile is not None and profile.deleted_at is None
        text = render_message_template(payout)
        keyboard = _recipient_keyboard(recipient.id, has_profile=has_profile)
        try:
            await bot.send_message(user.telegram_id, text, reply_markup=keyboard)
        except Exception as exc:  # noqa: BLE001
            async with session_factory() as fail_session:
                fail_recipient = await fail_session.get(PayoutRecipient, recipient.id)
                if fail_recipient is not None and fail_recipient.status == RecipientStatus.sending.value:
                    fail_recipient.status = RecipientStatus.failed.value
                    fail_recipient.failed_at = datetime.now(UTC)
                    fail_recipient.failure_reason = str(exc)[:250]
                await fail_session.commit()
            return
        next_status = RecipientStatus.sent.value if has_profile else RecipientStatus.payment_required.value
        async with session_factory() as ok_session:
            ok_recipient = await ok_session.get(PayoutRecipient, recipient.id)
            if ok_recipient is not None and ok_recipient.status == RecipientStatus.sending.value:
                ok_recipient.status = next_status
                ok_recipient.sent_at = datetime.now(UTC)
                await ok_session.commit()


async def run_worker(bot: Bot, session_factory: async_sessionmaker, settings, stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        async with session_factory() as session:
            await recover_stale_sending(session)
            await session.commit()

        async with session_factory() as session:
            recipients = await claim_pending_recipients(
                session,
                limit=max(1, int(settings.broadcast_rate_per_second)),
            )
            await session.commit()

        if not recipients:
            await asyncio.sleep(1)
            continue

        impacted_payout_ids = {recipient.payout_id for recipient in recipients}
        for recipient in recipients:
            if stop_event.is_set():
                break
            await process_recipient(bot, session_factory, settings, recipient.id)
            await asyncio.sleep(1 / settings.broadcast_rate_per_second)

        async with session_factory() as session:
            payouts = await session.scalars(
                select(Payout).where(
                    Payout.id.in_(impacted_payout_ids),
                    Payout.status.in_(
                        [
                            PayoutStatus.sending.value,
                            PayoutStatus.sent.value,
                            PayoutStatus.partially_failed.value,
                        ]
                    ),
                )
            )
            for payout in payouts:
                await finalize_payout_after_worker(session, payout)
            await session.commit()
