from __future__ import annotations

import pytest
from aiogram.types import InlineKeyboardMarkup

from topsbottg.models import PayoutRecipient, RecipientStatus
from topsbottg.services import add_recipients, create_payout, get_or_create_user, upsert_payment_profile
from topsbottg.worker import _recipient_keyboard, process_recipient


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str, object]] = []

    async def send_message(self, chat_id: int, text: str, reply_markup=None):  # noqa: ANN001
        self.sent.append((chat_id, text, reply_markup))


def _payout_payload() -> dict[str, object]:
    return {
        "period_start_day": 1,
        "period_start_month": 2,
        "period_end_day": 28,
        "period_end_month": 2,
        "message_template": None,
    }


@pytest.mark.asyncio
async def test_worker_sends_only_sending_claimed_recipient(session_factory, settings):
    async with session_factory() as session:
        user = await get_or_create_user(session, 111, "Иван Иванов")
        payout = await create_payout(
            session,
            actor_telegram_id=123,
            payload=_payout_payload(),
        )
        [recipient] = await add_recipients(session, payout, [user.id])
        recipient.status = RecipientStatus.sending.value
        await session.commit()
    bot = FakeBot()
    await process_recipient(bot, session_factory, settings, recipient.id)
    assert len(bot.sent) == 1
    assert "01.02 — 28.02" in bot.sent[0][1]
    assert "Платёжные данные:" not in bot.sent[0][1]
    assert isinstance(bot.sent[0][2], InlineKeyboardMarkup)
    assert [[button.text for button in row] for row in bot.sent[0][2].inline_keyboard] == [["Заполнить данные"]]


@pytest.mark.asyncio
async def test_worker_sends_profile_block_and_two_buttons(session_factory, settings):
    async with session_factory() as session:
        user = await get_or_create_user(session, 111, "Иван Иванов")
        await upsert_payment_profile(session, user, "Иван Иванов\n+79990000000\nT-Bank")
        payout = await create_payout(
            session,
            actor_telegram_id=123,
            payload=_payout_payload(),
        )
        [recipient] = await add_recipients(session, payout, [user.id])
        recipient.status = RecipientStatus.sending.value
        await session.commit()
    bot = FakeBot()
    await process_recipient(bot, session_factory, settings, recipient.id)
    assert len(bot.sent) == 1
    text = bot.sent[0][1]
    assert "Платёжные данные:" in text
    assert "Иван Иванов\n+79990000000\nT-Bank" in text
    assert isinstance(bot.sent[0][2], InlineKeyboardMarkup)
    assert [[button.text for button in row] for row in bot.sent[0][2].inline_keyboard] == [
        ["Подтвердить данные", "Изменить данные"]
    ]


@pytest.mark.asyncio
async def test_worker_keyboard_has_no_url(session_factory, settings):
    keyboard = _recipient_keyboard(10, has_profile=True)
    buttons = keyboard.inline_keyboard
    assert all(button.url is None for row in buttons for button in row)
    assert {button.callback_data for row in buttons for button in row} == {
        "confirm_profile:10",
        "fill_payment:10",
    }


@pytest.mark.asyncio
async def test_worker_does_not_resend_non_sending(session_factory, settings):
    async with session_factory() as session:
        user = await get_or_create_user(session, 111, "Иван Иванов")
        payout = await create_payout(
            session,
            actor_telegram_id=123,
            payload=_payout_payload(),
        )
        [recipient] = await add_recipients(session, payout, [user.id])
        recipient.status = RecipientStatus.sent.value
        await session.commit()
    bot = FakeBot()
    await process_recipient(bot, session_factory, settings, recipient.id)
    assert bot.sent == []


@pytest.mark.asyncio
async def test_worker_success_without_profile_sets_payment_required(session_factory, settings):
    async with session_factory() as session:
        user = await get_or_create_user(session, 111, "Иван Иванов")
        payout = await create_payout(
            session,
            actor_telegram_id=123,
            payload=_payout_payload(),
        )
        [recipient] = await add_recipients(session, payout, [user.id])
        recipient.status = RecipientStatus.sending.value
        await session.commit()
    bot = FakeBot()
    await process_recipient(bot, session_factory, settings, recipient.id)
    async with session_factory() as session:
        refreshed = await session.get(PayoutRecipient, recipient.id)
        assert refreshed.status == RecipientStatus.payment_required.value


@pytest.mark.asyncio
async def test_worker_success_with_profile_sets_sent(session_factory, settings):
    async with session_factory() as session:
        user = await get_or_create_user(session, 111, "Иван Иванов")
        await upsert_payment_profile(
            session,
            user,
            "Иван Иванов\n+79990000000\nT-Bank",
        )
        payout = await create_payout(
            session,
            actor_telegram_id=123,
            payload=_payout_payload(),
        )
        [recipient] = await add_recipients(session, payout, [user.id])
        recipient.status = RecipientStatus.sending.value
        await session.commit()
    bot = FakeBot()
    await process_recipient(bot, session_factory, settings, recipient.id)
    async with session_factory() as session:
        refreshed = await session.get(PayoutRecipient, recipient.id)
        assert refreshed.status == RecipientStatus.sent.value


@pytest.mark.asyncio
async def test_worker_send_failure_sets_failed(session_factory, settings):
    class BrokenBot(FakeBot):
        async def send_message(self, chat_id: int, text: str, reply_markup=None):  # noqa: ANN001
            raise RuntimeError("boom")

    async with session_factory() as session:
        user = await get_or_create_user(session, 111, "Иван Иванов")
        payout = await create_payout(
            session,
            actor_telegram_id=123,
            payload=_payout_payload(),
        )
        [recipient] = await add_recipients(session, payout, [user.id])
        recipient.status = RecipientStatus.sending.value
        await session.commit()
    await process_recipient(BrokenBot(), session_factory, settings, recipient.id)
    async with session_factory() as session:
        refreshed = await session.get(PayoutRecipient, recipient.id)
        assert refreshed.status == RecipientStatus.failed.value


@pytest.mark.asyncio
async def test_worker_does_not_overwrite_paid(session_factory, settings):
    async with session_factory() as session:
        user = await get_or_create_user(session, 111, "Иван Иванов")
        payout = await create_payout(
            session,
            actor_telegram_id=123,
            payload=_payout_payload(),
        )
        [recipient] = await add_recipients(session, payout, [user.id])
        recipient.status = RecipientStatus.paid.value
        await session.commit()
    await process_recipient(FakeBot(), session_factory, settings, recipient.id)
    async with session_factory() as session:
        refreshed = await session.get(PayoutRecipient, recipient.id)
        assert refreshed.status == RecipientStatus.paid.value


@pytest.mark.asyncio
async def test_worker_does_not_overwrite_payment_received(session_factory, settings):
    async with session_factory() as session:
        user = await get_or_create_user(session, 111, "Иван Иванов")
        payout = await create_payout(
            session,
            actor_telegram_id=123,
            payload=_payout_payload(),
        )
        [recipient] = await add_recipients(session, payout, [user.id])
        recipient.status = RecipientStatus.payment_received.value
        await session.commit()
    await process_recipient(FakeBot(), session_factory, settings, recipient.id)
    async with session_factory() as session:
        refreshed = await session.get(PayoutRecipient, recipient.id)
        assert refreshed.status == RecipientStatus.payment_received.value
