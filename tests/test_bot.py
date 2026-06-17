from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup, Message, ReplyKeyboardMarkup, ReplyKeyboardRemove, Update, WebAppInfo
from sqlalchemy import select

from topsbottg.bot import build_router, main_keyboard
from topsbottg.models import PaymentProfile, PayoutStatus, RecipientStatus, User
from topsbottg.services import (
    add_recipients,
    create_payout,
    get_or_create_user,
    get_payment_profile,
    upsert_payment_profile,
)


class RecordingSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.methods: list = []

    async def make_request(self, bot: Bot, method, timeout=None):  # noqa: ANN001
        self.methods.append(method)
        if method.__class__.__name__ == "AnswerCallbackQuery":
            return True
        return Message.model_validate(
            {
                "message_id": 100 + len(self.methods),
                "date": int(datetime.now(UTC).timestamp()),
                "chat": {"id": int(getattr(method, "chat_id", 0)), "type": "private"},
                "text": method.text,
            },
            context={"bot": bot},
        )

    async def stream_content(self, *args, **kwargs):  # noqa: ANN001
        if False:
            yield b""

    async def close(self) -> None:
        return None


def _make_bot() -> tuple[Bot, RecordingSession]:
    session = RecordingSession()
    bot = Bot("123:abc", session=session)
    return bot, session


def _make_dispatcher(session_factory, settings) -> Dispatcher:
    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher.include_router(build_router(session_factory, settings))
    return dispatcher


def _payout_payload() -> dict[str, object]:
    return {
        "period_start_day": 1,
        "period_start_month": 3,
        "period_end_day": 31,
        "period_end_month": 3,
        "message_template": None,
    }


def _make_update(bot: Bot, user_id: int, text: str, update_id: int) -> Update:
    return Update.model_validate(
        {
            "update_id": update_id,
            "message": {
                "message_id": 10 + update_id,
                "date": int(datetime.now(UTC).timestamp()),
                "chat": {"id": user_id, "type": "private"},
                "from": {"id": user_id, "is_bot": False, "first_name": "Test"},
                "text": text,
            },
        },
        context={"bot": bot},
    )


def _make_callback_update(
    bot: Bot,
    *,
    callback_user_id: int,
    message_from_user_id: int,
    chat_id: int,
    data: str,
    update_id: int,
) -> Update:
    return Update.model_validate(
        {
            "update_id": update_id,
            "callback_query": {
                "id": f"callback-{update_id}",
                "from": {"id": callback_user_id, "is_bot": False, "first_name": "Test"},
                "chat_instance": "test-instance",
                "message": {
                    "message_id": 900 + update_id,
                    "date": int(datetime.now(UTC).timestamp()),
                    "chat": {"id": chat_id, "type": "private"},
                    "from": {"id": message_from_user_id, "is_bot": True, "first_name": "Bot"},
                    "text": "Заполнить данные",
                },
                "data": data,
            },
        },
        context={"bot": bot},
    )


def _last_method(session: RecordingSession):
    assert session.methods, "bot did not send any message"
    return session.methods[-1]


def _last_message_method(session: RecordingSession):
    for method in reversed(session.methods):
        if method.__class__.__name__ == "SendMessage":
            return method
    raise AssertionError("bot did not send a message")


def _keyboard_texts(reply_markup):
    assert isinstance(reply_markup, ReplyKeyboardMarkup)
    return [[button.text for button in row] for row in reply_markup.keyboard]


def _inline_keyboard_texts(reply_markup):
    assert isinstance(reply_markup, InlineKeyboardMarkup)
    return [[button.text for button in row] for row in reply_markup.inline_keyboard]


def _inline_keyboard_button(reply_markup, text: str):
    assert isinstance(reply_markup, InlineKeyboardMarkup)
    for row in reply_markup.inline_keyboard:
        for button in row:
            if button.text == text:
                return button
    raise AssertionError(f"button {text!r} not found")


def _expected_payment_prompt() -> str:
    return (
        "Отправьте платёжные данные одним сообщением в свободной форме.\n\n"
        "Например:\n"
        "СБП +7 999 999-99-99, Т-Банк, Иван Иванович И.\n"
        "или\n"
        "карта 1111 1111 1111 1111, Сбер, Иван Иванович И."
    )


def _events(caplog: pytest.LogCaptureFixture) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    for record in caplog.records:
        try:
            events.append(json.loads(record.getMessage()))
        except json.JSONDecodeError:
            continue
    return events


async def test_main_keyboard_regular_user_has_no_admin_button():
    keyboard = _keyboard_texts(main_keyboard())
    assert keyboard == [
        ["Мои данные", "Изменить ФИО"],
        ["Изменить платёжные данные"],
    ]


async def test_main_keyboard_admin_has_no_admin_button(settings):
    keyboard = main_keyboard(is_admin=True, mini_app_url=settings.mini_app_url)
    assert _keyboard_texts(keyboard) == [
        ["Мои данные", "Изменить ФИО"],
        ["Изменить платёжные данные"],
    ]
    assert all(button.web_app is None for row in keyboard.keyboard for button in row)


async def test_start_for_registered_user_shows_main_keyboard(session_factory, settings):
    async with session_factory() as session:
        await get_or_create_user(session, 111, "Иван Иванов")
        await session.commit()

    bot, recording_session = _make_bot()
    dispatcher = _make_dispatcher(session_factory, settings)

    await dispatcher.feed_update(bot, _make_update(bot, 111, "/start", 1))

    method = _last_message_method(recording_session)
    assert method.text == "Вы уже зарегистрированы. Что хотите изменить?"
    assert _keyboard_texts(method.reply_markup) == [
        ["Мои данные", "Изменить ФИО"],
        ["Изменить платёжные данные"],
    ]


@pytest.mark.asyncio
async def test_start_registered_admin_has_no_admin_button_in_reply_keyboard(session_factory, settings):
    async with session_factory() as session:
        await get_or_create_user(session, 123, "Админ Пользователь")
        await session.commit()

    bot, recording_session = _make_bot()
    dispatcher = _make_dispatcher(session_factory, settings)

    await dispatcher.feed_update(bot, _make_update(bot, 123, "/start", 1))

    method = _last_message_method(recording_session)
    assert method.text == "Вы уже зарегистрированы. Что хотите изменить?"
    assert _keyboard_texts(method.reply_markup) == [
        ["Мои данные", "Изменить ФИО"],
        ["Изменить платёжные данные"],
    ]
    assert all(button.web_app is None for row in method.reply_markup.keyboard for button in row)


@pytest.mark.asyncio
async def test_start_registered_non_admin_has_no_admin_button(session_factory, settings):
    async with session_factory() as session:
        await get_or_create_user(session, 111, "Обычный Пользователь")
        await session.commit()

    bot, recording_session = _make_bot()
    dispatcher = _make_dispatcher(session_factory, settings)

    await dispatcher.feed_update(bot, _make_update(bot, 111, "/start", 1))

    method = _last_method(recording_session)
    assert method.text == "Вы уже зарегистрированы. Что хотите изменить?"
    assert _keyboard_texts(method.reply_markup) == [
        ["Мои данные", "Изменить ФИО"],
        ["Изменить платёжные данные"],
    ]
    assert all(button.web_app is None for row in method.reply_markup.keyboard for button in row)


@pytest.mark.asyncio
async def test_admin_command_shows_inline_web_app_button_for_admin(
    session_factory, settings, caplog: pytest.LogCaptureFixture
):
    caplog.set_level(logging.INFO)
    async with session_factory() as session:
        await get_or_create_user(session, 123, "Админ Пользователь")
        await session.commit()

    bot, recording_session = _make_bot()
    dispatcher = _make_dispatcher(session_factory, settings)

    await dispatcher.feed_update(bot, _make_update(bot, 123, "/admin", 1))

    method = _last_method(recording_session)
    assert method.text == "Откройте админку."
    assert _inline_keyboard_texts(method.reply_markup) == [["Открыть админку"]]
    admin_button = _inline_keyboard_button(method.reply_markup, "Открыть админку")
    assert admin_button.web_app == WebAppInfo(url=settings.mini_app_url)
    events = _events(caplog)
    assert any(event.get("event") == "bot_admin_entrypoint_requested" for event in events)
    assert any(event.get("event") == "bot_admin_entrypoint_sent" for event in events)


@pytest.mark.asyncio
async def test_admin_command_is_not_available_for_non_admin(
    session_factory, settings, caplog: pytest.LogCaptureFixture
):
    caplog.set_level(logging.INFO)
    async with session_factory() as session:
        await get_or_create_user(session, 111, "Обычный Пользователь")
        await session.commit()

    bot, recording_session = _make_bot()
    dispatcher = _make_dispatcher(session_factory, settings)

    await dispatcher.feed_update(bot, _make_update(bot, 111, "/admin", 1))

    method = _last_method(recording_session)
    assert method.text == "Команда доступна только админам."
    assert method.reply_markup is None
    events = _events(caplog)
    assert any(
        event.get("event") == "bot_admin_entrypoint_denied" and event.get("reason") == "non_admin"
        for event in events
    )


@pytest.mark.asyncio
async def test_registration_full_name_asks_payment_decision_only_on_first_registration(session_factory, settings):
    async with session_factory() as session:
        user = await get_or_create_user(session, 111, "Без ФИО")
        payout = await create_payout(
            session,
            actor_telegram_id=123,
            payload=_payout_payload(),
        )
        payout.status = PayoutStatus.sending.value
        payout = await add_recipients(session, payout, [user.id])
        [recipient] = payout
        recipient.status = RecipientStatus.sent.value
        await session.commit()

    bot, recording_session = _make_bot()
    dispatcher = _make_dispatcher(session_factory, settings)

    await dispatcher.feed_update(bot, _make_update(bot, 111, "/start", 1))

    method = _last_method(recording_session)
    assert method.text == "Здравствуйте. Для регистрации укажите ФИО."

    await dispatcher.feed_update(bot, _make_update(bot, 111, "Иван Иванов", 2))

    method = _last_method(recording_session)
    assert method.text == "ФИО сохранено. Хотите добавить платёжные данные сейчас?"
    assert _keyboard_texts(method.reply_markup) == [["Да", "Заполнить позже"]]
    async with session_factory() as session:
        db_user = await get_or_create_user(session, 111)
        assert db_user.full_name == "Иван Иванов"


@pytest.mark.asyncio
async def test_payment_later_returns_main_keyboard(session_factory, settings):
    async with session_factory() as session:
        user = await get_or_create_user(session, 111, "Без ФИО")
        payout = await create_payout(
            session,
            actor_telegram_id=123,
            payload=_payout_payload(),
        )
        payout.status = PayoutStatus.sending.value
        [recipient] = await add_recipients(session, payout, [user.id])
        recipient.status = RecipientStatus.payment_required.value
        await session.commit()

    bot, recording_session = _make_bot()
    dispatcher = _make_dispatcher(session_factory, settings)

    await dispatcher.feed_update(bot, _make_update(bot, 111, "/start", 1))
    await dispatcher.feed_update(bot, _make_update(bot, 111, "Иван Иванов", 2))
    await dispatcher.feed_update(bot, _make_update(bot, 111, "Заполнить позже", 3))

    method = _last_method(recording_session)
    assert method.text == "Готово, вы зарегистрированы. Платёжные данные можно добавить позже через меню."
    assert _keyboard_texts(method.reply_markup) == [
        ["Мои данные", "Изменить ФИО"],
        ["Изменить платёжные данные"],
    ]

    async with session_factory() as session:
        db_user = await get_or_create_user(session, 111)
        assert db_user.full_name == "Иван Иванов"
        profile = await session.scalar(select(PaymentProfile).where(PaymentProfile.user_id == db_user.id))
        assert profile is None
        recipient = await session.get(type(recipient), recipient.id)
        assert recipient.status == RecipientStatus.payment_required.value


@pytest.mark.asyncio
async def test_payment_details_saved_returns_main_keyboard(
    session_factory, settings, caplog: pytest.LogCaptureFixture
):
    caplog.set_level(logging.INFO)
    async with session_factory() as session:
        user = await get_or_create_user(session, 111, "Без ФИО")
        payout = await create_payout(
            session,
            actor_telegram_id=123,
            payload=_payout_payload(),
        )
        payout.status = PayoutStatus.sending.value
        [recipient] = await add_recipients(session, payout, [user.id])
        recipient.status = RecipientStatus.sent.value
        await session.commit()

    bot, recording_session = _make_bot()
    dispatcher = _make_dispatcher(session_factory, settings)

    await dispatcher.feed_update(bot, _make_update(bot, 111, "/start", 1))
    await dispatcher.feed_update(bot, _make_update(bot, 111, "Иван Иванов", 2))
    await dispatcher.feed_update(bot, _make_update(bot, 111, "Да", 3))

    method = _last_message_method(recording_session)
    assert method.text == _expected_payment_prompt()
    assert isinstance(method.reply_markup, ReplyKeyboardRemove)

    await dispatcher.feed_update(
        bot,
        _make_update(bot, 111, "Сбер\n+7 999 123 45 67\nИван", 4),
    )

    method = _last_method(recording_session)
    assert method.text == "Платёжные данные сохранены."
    assert _keyboard_texts(method.reply_markup) == [
        ["Мои данные", "Изменить ФИО"],
        ["Изменить платёжные данные"],
    ]

    async with session_factory() as session:
        db_user = await get_or_create_user(session, 111)
        profile = await session.scalar(select(PaymentProfile).where(PaymentProfile.user_id == db_user.id))
        assert profile is not None
        assert profile.raw_payment_details == "Сбер\n+7 999 123 45 67\nИван"
        recipient = await session.get(type(recipient), recipient.id)
        assert recipient.status == RecipientStatus.payment_received.value
    events = _events(caplog)
    assert any(event.get("event") == "bot_payment_details_update_started" for event in events)
    assert any(event.get("event") == "bot_payment_details_update_completed" for event in events)
    assert "Сбер" not in json.dumps(events, ensure_ascii=False)


@pytest.mark.asyncio
async def test_fill_payment_callback_uses_callback_user_identity(session_factory, settings, caplog):
    caplog.set_level(logging.INFO)
    actor_telegram_id = 1823119058
    bot_message_telegram_id = 8841263494
    async with session_factory() as session:
        user = await get_or_create_user(session, actor_telegram_id, "Без ФИО")
        payout = await create_payout(
            session,
            actor_telegram_id=123,
            payload=_payout_payload(),
        )
        payout.status = PayoutStatus.sending.value
        [recipient] = await add_recipients(session, payout, [user.id])
        recipient.status = RecipientStatus.sent.value
        await session.commit()

    bot, recording_session = _make_bot()
    dispatcher = _make_dispatcher(session_factory, settings)

    await dispatcher.feed_update(
        bot,
        _make_callback_update(
            bot,
            callback_user_id=actor_telegram_id,
            message_from_user_id=bot_message_telegram_id,
            chat_id=actor_telegram_id,
            data=f"fill_payment:{recipient.id}",
            update_id=1,
        ),
    )

    method = recording_session.methods[-1]
    assert method.__class__.__name__ == "AnswerCallbackQuery"
    edit_method = recording_session.methods[-2]
    assert edit_method.__class__.__name__ == "EditMessageText"
    assert edit_method.text == "Введите платёжные данные одним сообщением."
    assert edit_method.reply_markup is None
    assert method.text == "Введите платёжные данные."

    key = StorageKey(bot_id=bot.id, chat_id=actor_telegram_id, user_id=actor_telegram_id)
    assert await dispatcher.storage.get_state(key) == "RegistrationFSM:payment_details"
    assert (await dispatcher.storage.get_data(key)) == {"active_recipient_id": recipient.id}

    async with session_factory() as session:
        db_actor_user = await session.scalar(select(User).where(User.telegram_id == actor_telegram_id))
        db_bot_user = await session.scalar(select(User).where(User.telegram_id == bot_message_telegram_id))
        assert db_actor_user is not None
        assert db_actor_user.id == user.id
        assert db_bot_user is None

    events = _events(caplog)
    started = [event for event in events if event.get("event") == "bot_payment_details_update_started"]
    assert started
    assert started[-1].get("telegram_user_id") == actor_telegram_id
    assert started[-1].get("user_id") == user.id
    assert started[-1].get("active_recipient_id") == recipient.id


@pytest.mark.asyncio
async def test_confirm_profile_callback_updates_message_and_uses_actor_identity(
    session_factory, settings, caplog: pytest.LogCaptureFixture
):
    caplog.set_level(logging.INFO)
    actor_telegram_id = 1823119058
    bot_message_telegram_id = 8841263494
    async with session_factory() as session:
        user = await get_or_create_user(session, actor_telegram_id, "Иван Иванов")
        await upsert_payment_profile(session, user, "Иван Иванов\n+79990000000\nT-Bank")
        payout = await create_payout(
            session,
            actor_telegram_id=123,
            payload=_payout_payload(),
        )
        payout.status = PayoutStatus.sending.value
        [recipient] = await add_recipients(session, payout, [user.id])
        recipient.status = RecipientStatus.sent.value
        await session.commit()

    bot, recording_session = _make_bot()
    dispatcher = _make_dispatcher(session_factory, settings)

    await dispatcher.feed_update(
        bot,
        _make_callback_update(
            bot,
            callback_user_id=actor_telegram_id,
            message_from_user_id=bot_message_telegram_id,
            chat_id=actor_telegram_id,
            data=f"confirm_profile:{recipient.id}",
            update_id=2,
        ),
    )

    method = recording_session.methods[-1]
    assert method.__class__.__name__ == "AnswerCallbackQuery"
    assert method.text == "Данные подтверждены."
    edit_method = recording_session.methods[-2]
    assert edit_method.__class__.__name__ == "EditMessageText"
    assert edit_method.text == "Данные подтверждены."
    assert edit_method.reply_markup is None

    async with session_factory() as session:
        db_actor_user = await session.scalar(select(User).where(User.telegram_id == actor_telegram_id))
        db_bot_user = await session.scalar(select(User).where(User.telegram_id == bot_message_telegram_id))
        refreshed = await session.get(type(recipient), recipient.id)
        assert db_actor_user is not None
        assert db_bot_user is None
        assert refreshed.status == RecipientStatus.payment_received.value
        assert refreshed.payment_profile_snapshot["raw_payment_details"] == "Иван Иванов\n+79990000000\nT-Bank"

    events = _events(caplog)
    assert any(event.get("event") == "bot_profile_confirmed" for event in events)
    assert not any(event.get("event") == "bot_callback_rejected" for event in events)


@pytest.mark.asyncio
async def test_repeat_confirm_profile_returns_already_confirmed(
    session_factory,
    settings,
    caplog: pytest.LogCaptureFixture,
):
    caplog.set_level(logging.INFO)
    actor_telegram_id = 1823119058
    async with session_factory() as session:
        user = await get_or_create_user(session, actor_telegram_id, "Иван Иванов")
        await upsert_payment_profile(session, user, "Иван Иванов\n+79990000000\nT-Bank")
        payout = await create_payout(
            session,
            actor_telegram_id=123,
            payload=_payout_payload(),
        )
        payout.status = PayoutStatus.sending.value
        [recipient] = await add_recipients(session, payout, [user.id])
        recipient.status = RecipientStatus.payment_received.value
        recipient.payment_profile_snapshot = {"raw_payment_details": "Иван Иванов\n+79990000000\nT-Bank"}
        await session.commit()

    bot, recording_session = _make_bot()
    dispatcher = _make_dispatcher(session_factory, settings)

    await dispatcher.feed_update(
        bot,
        _make_callback_update(
            bot,
            callback_user_id=actor_telegram_id,
            message_from_user_id=1234567890,
            chat_id=actor_telegram_id,
            data=f"confirm_profile:{recipient.id}",
            update_id=3,
        ),
    )

    method = recording_session.methods[-1]
    assert method.__class__.__name__ == "AnswerCallbackQuery"
    assert method.text == "Данные уже подтверждены."
    assert len([m for m in recording_session.methods if m.__class__.__name__ == "EditMessageText"]) == 0

    events = _events(caplog)
    assert not any(event.get("event") == "bot_callback_rejected" for event in events)


@pytest.mark.asyncio
async def test_empty_payment_details_rejected(session_factory, settings):
    async with session_factory() as session:
        user = await get_or_create_user(session, 111, "Без ФИО")
        payout = await create_payout(
            session,
            actor_telegram_id=123,
            payload=_payout_payload(),
        )
        payout.status = PayoutStatus.sending.value
        [recipient] = await add_recipients(session, payout, [user.id])
        recipient.status = RecipientStatus.sent.value
        await session.commit()

    bot, recording_session = _make_bot()
    dispatcher = _make_dispatcher(session_factory, settings)

    await dispatcher.feed_update(bot, _make_update(bot, 111, "/start", 1))
    await dispatcher.feed_update(bot, _make_update(bot, 111, "Иван Иванов", 2))
    await dispatcher.feed_update(bot, _make_update(bot, 111, "что-то не то", 3))

    method = _last_method(recording_session)
    assert method.text == "Выберите одну из кнопок."
    assert _keyboard_texts(method.reply_markup) == [["Да", "Заполнить позже"]]

    await dispatcher.feed_update(bot, _make_update(bot, 111, "Да", 4))
    await dispatcher.feed_update(bot, _make_update(bot, 111, "   ", 5))

    method = _last_method(recording_session)
    assert method.text == "Отправьте непустые платёжные данные одним сообщением."
    assert isinstance(method.reply_markup, ReplyKeyboardRemove)

    async with session_factory() as session:
        db_user = await get_or_create_user(session, 111)
        profile = await get_payment_profile(session, db_user.id)
        assert profile is None
        recipient = await session.get(type(recipient), recipient.id)
        assert recipient.status == RecipientStatus.sent.value


@pytest.mark.asyncio
async def test_change_full_name_does_not_ask_payment_decision(session_factory, settings):
    async with session_factory() as session:
        user = await get_or_create_user(session, 111, "Без ФИО")
        await session.commit()

    bot, recording_session = _make_bot()
    dispatcher = _make_dispatcher(session_factory, settings)

    await dispatcher.feed_update(bot, _make_update(bot, 111, "/start", 1))
    await dispatcher.feed_update(bot, _make_update(bot, 111, "Иван Иванов", 2))
    await dispatcher.feed_update(bot, _make_update(bot, 111, "Заполнить позже", 3))
    await dispatcher.feed_update(bot, _make_update(bot, 111, "Изменить платёжные данные", 4))
    await dispatcher.feed_update(bot, _make_update(bot, 111, "Сбер\n+7 999 123 45 67\nИван", 5))

    async with session_factory() as session:
        db_user = await get_or_create_user(session, 111)
        assert db_user.id == user.id
        assert db_user.full_name == "Иван Иванов"
        profile_before = await session.scalar(select(PaymentProfile).where(PaymentProfile.user_id == db_user.id))
        assert profile_before is not None
        raw_payment_details = profile_before.raw_payment_details

    await dispatcher.feed_update(bot, _make_update(bot, 111, "Изменить ФИО", 6))
    await dispatcher.feed_update(bot, _make_update(bot, 111, "Иван Петров", 7))

    method = _last_method(recording_session)
    assert method.text == "ФИО обновлено."
    assert _keyboard_texts(method.reply_markup) == [
        ["Мои данные", "Изменить ФИО"],
        ["Изменить платёжные данные"],
    ]

    async with session_factory() as session:
        db_user = await get_or_create_user(session, 111)
        assert db_user.id == user.id
        assert db_user.full_name == "Иван Петров"
        profile_after = await session.scalar(select(PaymentProfile).where(PaymentProfile.user_id == db_user.id))
        assert profile_after is not None
        assert profile_after.raw_payment_details == raw_payment_details


@pytest.mark.asyncio
async def test_change_full_name_keeps_main_keyboard(session_factory, settings):
    async with session_factory() as session:
        await get_or_create_user(session, 111, "Без ФИО")
        await session.commit()

    bot, recording_session = _make_bot()
    dispatcher = _make_dispatcher(session_factory, settings)

    await dispatcher.feed_update(bot, _make_update(bot, 111, "/start", 1))
    await dispatcher.feed_update(bot, _make_update(bot, 111, "Иван Иванов", 2))
    await dispatcher.feed_update(bot, _make_update(bot, 111, "Заполнить позже", 3))
    await dispatcher.feed_update(bot, _make_update(bot, 111, "Изменить ФИО", 4))
    await dispatcher.feed_update(bot, _make_update(bot, 111, "Иван Петров", 5))

    method = _last_method(recording_session)
    assert method.text == "ФИО обновлено."
    assert _keyboard_texts(method.reply_markup) == [
        ["Мои данные", "Изменить ФИО"],
        ["Изменить платёжные данные"],
    ]


@pytest.mark.asyncio
async def test_payment_details_saved_admin_keeps_admin_button(session_factory, settings):
    async with session_factory() as session:
        await get_or_create_user(session, 123, "Админ Пользователь")
        payout = await create_payout(
            session,
            actor_telegram_id=123,
            payload=_payout_payload(),
        )
        payout.status = PayoutStatus.sending.value
        [recipient] = await add_recipients(session, payout, [123])
        recipient.status = RecipientStatus.sent.value
        await session.commit()

    bot, recording_session = _make_bot()
    dispatcher = _make_dispatcher(session_factory, settings)

    await dispatcher.feed_update(bot, _make_update(bot, 123, "/start", 1))
    await dispatcher.feed_update(bot, _make_update(bot, 123, "Изменить платёжные данные", 2))
    await dispatcher.feed_update(bot, _make_update(bot, 123, "Сбер\n+7 999 123 45 67\nИван", 3))

    method = _last_method(recording_session)
    assert method.text == "Платёжные данные сохранены."
    assert _keyboard_texts(method.reply_markup) == [
        ["Мои данные", "Изменить ФИО"],
        ["Изменить платёжные данные"],
    ]
    assert all(button.web_app is None for row in method.reply_markup.keyboard for button in row)


@pytest.mark.asyncio
async def test_change_full_name_admin_keeps_admin_button(session_factory, settings):
    async with session_factory() as session:
        await get_or_create_user(session, 123, "Админ Пользователь")
        await session.commit()

    bot, recording_session = _make_bot()
    dispatcher = _make_dispatcher(session_factory, settings)

    await dispatcher.feed_update(bot, _make_update(bot, 123, "/start", 1))
    await dispatcher.feed_update(bot, _make_update(bot, 123, "Изменить ФИО", 2))
    await dispatcher.feed_update(bot, _make_update(bot, 123, "Админ Новый", 3))

    method = _last_method(recording_session)
    assert method.text == "ФИО обновлено."
    assert _keyboard_texts(method.reply_markup) == [
        ["Мои данные", "Изменить ФИО"],
        ["Изменить платёжные данные"],
    ]
    assert all(button.web_app is None for row in method.reply_markup.keyboard for button in row)


@pytest.mark.asyncio
async def test_change_payment_details_same_telegram_user(session_factory, settings):
    async with session_factory() as session:
        await get_or_create_user(session, 111, "Без ФИО")
        await session.commit()

    bot, recording_session = _make_bot()
    dispatcher = _make_dispatcher(session_factory, settings)

    await dispatcher.feed_update(bot, _make_update(bot, 111, "/start", 1))
    await dispatcher.feed_update(bot, _make_update(bot, 111, "Иван Иванов", 2))
    await dispatcher.feed_update(bot, _make_update(bot, 111, "Заполнить позже", 3))
    await dispatcher.feed_update(bot, _make_update(bot, 111, "Изменить платёжные данные", 4))
    await dispatcher.feed_update(bot, _make_update(bot, 111, "Петр Петров\n+79990000001\nSber", 5))

    async with session_factory() as session:
        db_user = await get_or_create_user(session, 111)
        profile = await session.scalar(select(PaymentProfile).where(PaymentProfile.user_id == db_user.id))
        assert profile is not None
        assert profile.user_id == db_user.id
        assert profile.raw_payment_details == "Петр Петров\n+79990000001\nSber"

    method = _last_method(recording_session)
    assert method.text == "Платёжные данные сохранены."


@pytest.mark.asyncio
async def test_my_data_shows_raw_payment_details(session_factory, settings):
    async with session_factory() as session:
        user = await get_or_create_user(session, 111, "Иван Иванов")
        await upsert_payment_profile(session, user, "Иван Иванов\n+79990000000\nT-Bank")
        await session.commit()

    bot, recording_session = _make_bot()
    dispatcher = _make_dispatcher(session_factory, settings)

    await dispatcher.feed_update(bot, _make_update(bot, 111, "Мои данные", 6))

    method = _last_method(recording_session)
    assert method.text == "ФИО: Иван Иванов\nПлатёжные данные:\nИван Иванов\n+79990000000\nT-Bank"
    assert _keyboard_texts(method.reply_markup) == [
        ["Мои данные", "Изменить ФИО"],
        ["Изменить платёжные данные"],
    ]


@pytest.mark.asyncio
async def test_my_data_without_payment_details(session_factory, settings):
    async with session_factory() as session:
        await get_or_create_user(session, 111, "Иван Иванов")
        await session.commit()

    bot, recording_session = _make_bot()
    dispatcher = _make_dispatcher(session_factory, settings)

    await dispatcher.feed_update(bot, _make_update(bot, 111, "Мои данные", 7))

    method = _last_method(recording_session)
    assert method.text == "ФИО: Иван Иванов\nПлатёжные данные: не заполнены"
    assert _keyboard_texts(method.reply_markup) == [
        ["Мои данные", "Изменить ФИО"],
        ["Изменить платёжные данные"],
    ]


@pytest.mark.asyncio
async def test_legacy_payment_details_button_still_starts_flow(session_factory, settings):
    async with session_factory() as session:
        await get_or_create_user(session, 111, "Без ФИО")
        await session.commit()

    bot, recording_session = _make_bot()
    dispatcher = _make_dispatcher(session_factory, settings)

    await dispatcher.feed_update(bot, _make_update(bot, 111, "/start", 1))
    await dispatcher.feed_update(bot, _make_update(bot, 111, "Иван Иванов", 2))
    await dispatcher.feed_update(bot, _make_update(bot, 111, "Заполнить позже", 3))
    await dispatcher.feed_update(bot, _make_update(bot, 111, "Платёжные данные", 4))

    method = _last_method(recording_session)
    assert method.text == _expected_payment_prompt()
    assert isinstance(method.reply_markup, ReplyKeyboardRemove)


async def test_delete_payment_button_not_in_main_keyboard():
    keyboard = _keyboard_texts(main_keyboard())
    assert all("Удалить платёжные данные" not in row for row in keyboard)


async def test_admin_command_hidden_when_mini_app_url_missing(session_factory, settings, monkeypatch):
    monkeypatch.setattr(settings, "mini_app_url", None)
    async with session_factory() as session:
        await get_or_create_user(session, 123, "Админ Пользователь")
        await session.commit()

    bot, recording_session = _make_bot()
    dispatcher = _make_dispatcher(session_factory, settings)

    await dispatcher.feed_update(bot, _make_update(bot, 123, "/admin", 1))

    method = _last_method(recording_session)
    assert method.text == "Админка недоступна."
    assert method.reply_markup is None


async def test_main_keyboard_ignores_admin_flag_when_mini_app_url_missing():
    keyboard = main_keyboard(is_admin=True, mini_app_url=None)
    assert _keyboard_texts(keyboard) == [
        ["Мои данные", "Изменить ФИО"],
        ["Изменить платёжные данные"],
    ]


@pytest.mark.asyncio
async def test_duplicate_full_names_remain_separate_in_admin_api(client, session_factory, admin_init_data):
    async with session_factory() as session:
        user_one = await get_or_create_user(session, 111, "Иван Иванов")
        _user_two = await get_or_create_user(session, 222, "Иван Иванов")
        await session.commit()

    response = await client.get(
        "/api/admin/users",
        params={"search": "Иван Иванов"},
        headers={"X-Telegram-Init-Data": admin_init_data},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["has_more"] is False
    assert len(body["items"]) == 2
    assert {row["telegram_id"] for row in body["items"]} == {111, 222}
    assert all("raw_payment_details" not in row for row in body["items"])

    detail = await client.get(f"/api/admin/users/{user_one.id}", headers={"X-Telegram-Init-Data": admin_init_data})
    assert detail.status_code == 200
    assert detail.json()["user"]["telegram_id"] == 111


def test_bot_source_has_no_legacy_payment_parser():
    source = (Path(__file__).resolve().parents[1] / "src/topsbottg/bot.py").read_text(encoding="utf-8")
    assert "parse_payment_reply" not in source
