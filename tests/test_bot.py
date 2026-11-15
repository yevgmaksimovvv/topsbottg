from __future__ import annotations

import asyncio
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

from topsbottg import bot as bot_module
from topsbottg.bot import (
    CHANGE_PAYMENT_DETAILS_BUTTON_TEXT,
    CHAT_ENTRYPOINT_TEXT,
    build_router,
    main_keyboard,
)
from topsbottg.main import create_bot_dispatcher
from topsbottg.models import (
    BotFSMState,
    BotSensitiveMessage,
    ChatMessage,
    Conversation,
    PaymentProfile,
    PayoutStatus,
    RecipientStatus,
    User,
)
from topsbottg.services import (
    add_recipients,
    create_payout,
    get_admin_events_broadcaster,
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
        ["Изменить группу"],
    ]


async def test_main_keyboard_admin_has_no_admin_button(settings):
    keyboard = main_keyboard(is_admin=True, mini_app_url=settings.mini_app_url)
    assert _keyboard_texts(keyboard) == [
        ["Мои данные", "Изменить ФИО"],
        ["Изменить платёжные данные"],
        ["Изменить группу"],
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
        ["Изменить группу"],
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
        ["Изменить группу"],
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
        ["Изменить группу"],
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
    assert method.text == "ФИО сохранено. Укажите учебную группу, например М3О-207СВ-25."
    assert isinstance(method.reply_markup, ReplyKeyboardRemove)
    async with session_factory() as session:
        db_user = await get_or_create_user(session, 111)
        assert db_user.full_name == "Иван Иванов"
        assert db_user.registration_completed_at is None

    await dispatcher.feed_update(bot, _make_update(bot, 111, "м3о - 207св - 25", 3))
    method = _last_method(recording_session)
    assert method.text == "Группа сохранена. Хотите добавить платёжные данные сейчас?"
    assert _keyboard_texts(method.reply_markup) == [["Да", "Заполнить позже"]]
    async with session_factory() as session:
        db_user = await get_or_create_user(session, 111)
        assert db_user.registration_completed_at is not None
        assert db_user.study_group_id is not None


@pytest.mark.asyncio
async def test_group_is_required_and_invalid_input_does_not_finish_registration(session_factory, settings):
    bot, recording_session = _make_bot()
    dispatcher = _make_dispatcher(session_factory, settings)

    await dispatcher.feed_update(bot, _make_update(bot, 111, "/start", 1))
    await dispatcher.feed_update(bot, _make_update(bot, 111, "Иван Иванов", 2))
    await dispatcher.feed_update(bot, _make_update(bot, 111, "Заполнить позже", 3))

    method = _last_method(recording_session)
    assert "Введите код учебной группы" in method.text
    async with session_factory() as session:
        user = await get_or_create_user(session, 111)
        assert user.registration_completed_at is None
        assert user.study_group_id is None

    await dispatcher.feed_update(bot, _make_update(bot, 111, "М3О-207СВ-25", 4))
    method = _last_method(recording_session)
    assert method.text == "Группа сохранена. Хотите добавить платёжные данные сейчас?"
    async with session_factory() as session:
        user = await get_or_create_user(session, 111)
        assert user.registration_completed_at is not None


@pytest.mark.asyncio
async def test_start_resumes_unfinished_group_registration_after_restart(session_factory, settings):
    bot, recording_session = _make_bot()
    dispatcher = _make_dispatcher(session_factory, settings)
    await dispatcher.feed_update(bot, _make_update(bot, 111, "/start", 1))
    await dispatcher.feed_update(bot, _make_update(bot, 111, "Иван Иванов", 2))

    # New dispatcher has no FSM state: registration progress lives in PostgreSQL/SQLite.
    restarted = _make_dispatcher(session_factory, settings)
    await restarted.feed_update(bot, _make_update(bot, 111, "/start", 3))
    assert _last_method(recording_session).text == "Укажите учебную группу, например М3О-207СВ-25."
    await restarted.feed_update(bot, _make_update(bot, 111, "М3О-207СВ-25", 4))
    assert _last_method(recording_session).text.startswith("Группа сохранена.")
    await restarted.feed_update(bot, _make_update(bot, 111, "Заполнить позже", 5))
    await restarted.feed_update(bot, _make_update(bot, 111, "/start", 6))
    assert _last_method(recording_session).text == "Вы уже зарегистрированы. Что хотите изменить?"


@pytest.mark.asyncio
async def test_existing_user_can_change_group_without_re_registration(session_factory, settings):
    async with session_factory() as session:
        user = await get_or_create_user(session, 111, "Иван Иванов")
        await upsert_payment_profile(session, user, "СБП +79990000000")
        completed_at = user.registration_completed_at
        await session.commit()

    bot, recording_session = _make_bot()
    dispatcher = _make_dispatcher(session_factory, settings)
    await dispatcher.feed_update(bot, _make_update(bot, 111, "/start", 1))
    assert _last_method(recording_session).text == "Вы уже зарегистрированы. Что хотите изменить?"
    await dispatcher.feed_update(bot, _make_update(bot, 111, "Изменить группу", 2))
    assert _last_method(recording_session).text == "Введите новую учебную группу."
    await dispatcher.feed_update(bot, _make_update(bot, 111, "bad group", 3))
    assert "Введите код учебной группы" in _last_method(recording_session).text
    await dispatcher.feed_update(bot, _make_update(bot, 111, "м3о - 207св - 25", 4))
    assert _last_method(recording_session).text == "Учебная группа изменена: М3О-207СВ-25."
    await dispatcher.feed_update(bot, _make_update(bot, 111, "Мои данные", 5))
    assert _last_method(recording_session).text == (
        "ФИО: Иван Иванов\nУчебная группа: М3О-207СВ-25"
        "\nПлатёжные данные:\nСБП +79990000000"
    )
    async with session_factory() as session:
        user = await get_or_create_user(session, 111)
        assert user.study_group_id is not None
        assert user.registration_completed_at is not None
        assert user.registration_completed_at.replace(tzinfo=None) == completed_at.replace(tzinfo=None)
        profile = await get_payment_profile(session, user.id)
        assert profile.raw_payment_details == "СБП +79990000000"

    await dispatcher.feed_update(bot, _make_update(bot, 111, "Изменить группу", 6))
    await dispatcher.feed_update(bot, _make_update(bot, 111, "/start", 7))
    assert _last_method(recording_session).text == "Вы уже зарегистрированы. Что хотите изменить?"


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
    await dispatcher.feed_update(bot, _make_update(bot, 111, "М3О-207СВ-25", 71))
    await dispatcher.feed_update(bot, _make_update(bot, 111, "Заполнить позже", 3))

    method = _last_method(recording_session)
    assert method.text == "Готово, вы зарегистрированы. Платёжные данные можно добавить позже через меню."
    assert _keyboard_texts(method.reply_markup) == [
        ["Мои данные", "Изменить ФИО"],
        ["Изменить платёжные данные"],
        ["Изменить группу"],
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
    await dispatcher.feed_update(bot, _make_update(bot, 111, "М3О-207СВ-25", 71))
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
        ["Изменить группу"],
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
    await dispatcher.feed_update(bot, _make_update(bot, 111, "М3О-207СВ-25", 71))
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
    await dispatcher.feed_update(bot, _make_update(bot, 111, "М3О-207СВ-25", 71))
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
        ["Изменить группу"],
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
    await dispatcher.feed_update(bot, _make_update(bot, 111, "М3О-207СВ-25", 71))
    await dispatcher.feed_update(bot, _make_update(bot, 111, "Заполнить позже", 3))
    await dispatcher.feed_update(bot, _make_update(bot, 111, "Изменить ФИО", 4))
    await dispatcher.feed_update(bot, _make_update(bot, 111, "Иван Петров", 5))

    method = _last_method(recording_session)
    assert method.text == "ФИО обновлено."
    assert _keyboard_texts(method.reply_markup) == [
        ["Мои данные", "Изменить ФИО"],
        ["Изменить платёжные данные"],
        ["Изменить группу"],
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
        ["Изменить группу"],
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
        ["Изменить группу"],
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
    await dispatcher.feed_update(bot, _make_update(bot, 111, "М3О-207СВ-25", 71))
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
@pytest.mark.asyncio
async def test_change_payment_details_does_not_confirm_pending_recipient(session_factory, settings):
    async with session_factory() as session:
        user = await get_or_create_user(session, 111, "Пользователь")
        payout = await create_payout(session, actor_telegram_id=123, payload=_payout_payload())
        payout.status = PayoutStatus.sending.value
        [recipient] = await add_recipients(session, payout, [user.id])
        recipient.status = RecipientStatus.sent.value
        await session.commit()

    bot, _ = _make_bot()
    dispatcher = _make_dispatcher(session_factory, settings)

    await dispatcher.feed_update(bot, _make_update(bot, 111, "/start", 1))
    await dispatcher.feed_update(bot, _make_update(bot, 111, CHANGE_PAYMENT_DETAILS_BUTTON_TEXT, 2))
    await dispatcher.feed_update(bot, _make_update(bot, 111, "Владелец\n+79990000001\nБанк", 3))

    async with session_factory() as session:
        db_recipient = await session.get(type(recipient), recipient.id)
        assert db_recipient is not None
        assert db_recipient.status == RecipientStatus.sent.value
        assert db_recipient.payment_profile_snapshot is None


async def test_my_data_shows_raw_payment_details(session_factory, settings):
    async with session_factory() as session:
        user = await get_or_create_user(session, 111, "Иван Иванов")
        await upsert_payment_profile(session, user, "Иван Иванов\n+79990000000\nT-Bank")
        await session.commit()

    bot, recording_session = _make_bot()
    dispatcher = _make_dispatcher(session_factory, settings)

    await dispatcher.feed_update(bot, _make_update(bot, 111, "Мои данные", 6))

    method = _last_method(recording_session)
    assert method.text == (
        "ФИО: Иван Иванов\nУчебная группа: не указана\n"
        "Платёжные данные:\nИван Иванов\n+79990000000\nT-Bank"
    )
    assert _keyboard_texts(method.reply_markup) == [
        ["Мои данные", "Изменить ФИО"],
        ["Изменить платёжные данные"],
        ["Изменить группу"],
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
    assert method.text == "ФИО: Иван Иванов\nУчебная группа: не указана\nПлатёжные данные: не заполнены"
    assert _keyboard_texts(method.reply_markup) == [
        ["Мои данные", "Изменить ФИО"],
        ["Изменить платёжные данные"],
        ["Изменить группу"],
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
    await dispatcher.feed_update(bot, _make_update(bot, 111, "М3О-207СВ-25", 71))
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
        ["Изменить группу"],
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

@pytest.mark.asyncio
async def test_database_admin_can_open_bot_admin_until_revoked(session_factory, settings):
    from topsbottg.access import grant_admin, revoke_admin

    async with session_factory() as session:
        await grant_admin(
            session, actor_telegram_id=123, target_telegram_id=555,
            owner_ids=settings.admin_ids_set,
        )
        await session.commit()

    instance, recording_session = _make_bot()
    dispatcher = _make_dispatcher(session_factory, settings)
    await dispatcher.feed_update(instance, _make_update(instance, 555, "/admin", 1))
    assert _last_message_method(recording_session).text == "Откройте админку."

    async with session_factory() as session:
        await revoke_admin(
            session, actor_telegram_id=123, target_telegram_id=555,
            owner_ids=settings.admin_ids_set,
        )
        await session.commit()
    await dispatcher.feed_update(instance, _make_update(instance, 555, "/admin", 2))
    assert _last_message_method(recording_session).text == "Команда доступна только админам."

    # A configured owner can use /admin even without a row in users.
    await dispatcher.feed_update(instance, _make_update(instance, 123, "/admin", 3))
    assert _last_message_method(recording_session).text == "Откройте админку."

@pytest.mark.asyncio
async def test_start_registers_telegram_id_without_profile_cache(session_factory, settings):
    bot, recording = _make_bot()
    dispatcher = _make_dispatcher(session_factory, settings)
    update = Update.model_validate(
        {
            "update_id": 1,
            "message": {
                "message_id": 11,
                "date": int(datetime.now(UTC).timestamp()),
                "chat": {"id": 555, "type": "private"},
                "from": {
                    "id": 555, "is_bot": False, "first_name": "Настоящее",
                    "last_name": "Имя", "username": "first_handle",
                },
                "text": "/start",
            },
        },
        context={"bot": bot},
    )
    await dispatcher.feed_update(bot, update)
    async with session_factory() as session:
        user = await session.scalar(select(User).where(User.telegram_id == 555))
        assert user.telegram_id == 555
        assert user.full_name == "Без ФИО"
    assert "Здравствуйте" in _last_message_method(recording).text


@pytest.mark.asyncio
async def test_invite_callback_requires_matching_telegram_user_then_grants_access(session_factory, settings):
    from topsbottg.access import get_principal
    from topsbottg.invitations import create_invitation
    from topsbottg.models import AdminInvitation

    async with session_factory() as session:
        await get_or_create_user(session, 555, "Иван Иванов")
        invitation = await create_invitation(
            session, actor_id=123, target_id=555, owner_ids=settings.admin_ids_set
        )
        invitation_id = invitation.id
        await session.commit()

    bot, recording = _make_bot()
    dispatcher = _make_dispatcher(session_factory, settings)
    await dispatcher.feed_update(
        bot, _make_callback_update(
            bot, callback_user_id=999, message_from_user_id=123, chat_id=999,
            data=f"admin_invite:accept:{invitation_id}", update_id=1,
        ),
    )
    assert _last_method(recording).text == "Это приглашение адресовано другому пользователю."
    async with session_factory() as session:
        assert await get_principal(session, 555, settings.admin_ids_set) is None

    await dispatcher.feed_update(
        bot, _make_callback_update(
            bot, callback_user_id=555, message_from_user_id=123, chat_id=555,
            data=f"admin_invite:accept:{invitation_id}", update_id=2,
        ),
    )
    assert _last_method(recording).text == "Доступ администратора предоставлен. Используйте /admin."
    async with session_factory() as session:
        assert (await get_principal(session, 555, settings.admin_ids_set)).role == "admin"
        assert (await session.get(AdminInvitation, invitation_id)).status == "accepted"

    await dispatcher.feed_update(bot, _make_update(bot, 555, "/admin", 3))
    assert _last_message_method(recording).text == "Откройте админку."


@pytest.mark.asyncio
async def test_invite_decline_button_never_grants_admin(session_factory, settings):
    from topsbottg.access import get_principal
    from topsbottg.invitations import create_invitation

    async with session_factory() as session:
        await get_or_create_user(session, 555, "Иван Иванов")
        invitation = await create_invitation(
            session, actor_id=123, target_id=555, owner_ids=settings.admin_ids_set
        )
        id_ = invitation.id
        await session.commit()
    bot, recording = _make_bot()
    dispatcher = _make_dispatcher(session_factory, settings)
    await dispatcher.feed_update(
        bot, _make_callback_update(
            bot, callback_user_id=555, message_from_user_id=123, chat_id=555,
            data=f"admin_invite:decline:{id_}", update_id=1,
        ),
    )
    assert _last_method(recording).text == "Приглашение отклонено."
    async with session_factory() as session:
        assert await get_principal(session, 555, settings.admin_ids_set) is None


def _chat_update(
    bot: Bot,
    user_id: int,
    text: str,
    update_id: int,
    *,
    chat_type: str = "private",
    chat_id: int | None = None,
    message_id: int | None = None,
    reply_to_id: int | None = None,
    is_bot: bool = False,
) -> Update:
    chat_id = chat_id if chat_id is not None else user_id
    payload = {
        "message_id": message_id if message_id is not None else 1000 + update_id,
        "date": int(datetime.now(UTC).timestamp()),
        "chat": {"id": chat_id, "type": chat_type},
        "from": {"id": user_id, "is_bot": is_bot, "first_name": "Test"},
        "text": text,
    }
    if reply_to_id is not None:
        payload["reply_to_message"] = {
            "message_id": reply_to_id,
            "date": int(datetime.now(UTC).timestamp()),
            "chat": {"id": chat_id, "type": chat_type},
            "from": {"id": 9999, "is_bot": True, "first_name": "Bot"},
            "text": "Ответ",
        }
    return Update.model_validate(
        {"update_id": update_id, "message": payload}, context={"bot": bot}
    )


@pytest.mark.asyncio
async def test_private_chat_persists_in_order_and_deduplicates_across_dispatchers(
    session_factory, settings, caplog
):
    caplog.set_level(logging.INFO)
    async with session_factory() as session:
        user = await get_or_create_user(session, 411, "Пользователь без группы")
        assert user.study_group_id is None
        await session.commit()

    events = get_admin_events_broadcaster().subscribe()
    bot, _ = _make_bot()
    dispatcher = _make_dispatcher(session_factory, settings)
    try:
        for number in range(5):
            await dispatcher.feed_update(
                bot, _chat_update(
                    bot, 411, f"Обращение {number}", 200 + number,
                    reply_to_id=9000 if number == 4 else None,
                ),
            )
        for _ in range(5):
            assert events.get_nowait()["event"] == "chat.changed"
        assert events.empty()

        restarted = _make_dispatcher(session_factory, settings)
        await restarted.feed_update(
            bot, _chat_update(bot, 411, "Повтор", 300, message_id=1203)
        )
        assert events.empty()
    finally:
        get_admin_events_broadcaster().unsubscribe(events)

    async with session_factory() as session:
        conversations = list((await session.scalars(select(Conversation))).all())
        assert len(conversations) == 1
        assert conversations[0].user_id == user.id
        rows = list((await session.scalars(select(ChatMessage).order_by(ChatMessage.id))).all())
        assert len(rows) == 5
        assert [row.telegram_message_id for row in rows] == [1200, 1201, 1202, 1203, 1204]
        assert [row.text for row in rows] == [f"Обращение {i}" for i in range(5)]
        assert all(row.telegram_chat_id == 411 for row in rows)
        assert rows[-1].reply_to_telegram_message_id == 9000
        assert rows[-1].source == "telegram_reply"
        assert all(row.source == "telegram" for row in rows[:-1])

    assert all(event.get("payload") is None for event in _events(caplog))
    assert "Обращение 4" not in caplog.text


@pytest.mark.asyncio
async def test_unregistered_draft_group_channel_bot_and_commands_do_not_create_chat(session_factory, settings):
    async with session_factory() as session:
        await get_or_create_user(session, 411, "Готовый пользователь")
        await get_or_create_user(session, 412, "Без ФИО")
        session.add(User(telegram_id=413, full_name="Регистрация не окончена"))
        await session.commit()

    bot, _ = _make_bot()
    dispatcher = _make_dispatcher(session_factory, settings)
    updates = [
        _chat_update(bot, 411, "Сообщение в группе", 410, chat_type="supergroup", chat_id=-100411),
        _chat_update(bot, 411, "Сообщение в канале", 411, chat_type="channel", chat_id=-100412),
        _chat_update(bot, 412, "Черновик", 412),
        _chat_update(bot, 413, "ФИО без завершения", 413),
        _chat_update(bot, 414, "Неизвестный пользователь", 414),
        _chat_update(bot, 411, "/неизвестная_команда", 415),
        _chat_update(bot, 411, "Сообщение бота", 416, is_bot=True),
    ]
    for update in updates:
        await dispatcher.feed_update(bot, update)
    async with session_factory() as session:
        assert (await session.scalars(select(Conversation))).all() == []
        assert (await session.scalars(select(ChatMessage))).all() == []


@pytest.mark.asyncio
async def test_chat_does_not_capture_registration_or_payment_fsm_and_preserves_payout(
    session_factory, settings, caplog
):
    caplog.set_level(logging.INFO)
    async with session_factory() as session:
        user = await get_or_create_user(session, 411, "Зарегистрированный пользователь")
        payout = await create_payout(session, actor_telegram_id=123, payload=_payout_payload())
        payout.status = PayoutStatus.sent.value
        [recipient] = await add_recipients(session, payout, [user.id])
        recipient.status = RecipientStatus.sent.value
        await session.commit()
        payout_id, recipient_id = payout.id, recipient.id

    bot, _ = _make_bot()
    dispatcher = _make_dispatcher(session_factory, settings)
    await dispatcher.feed_update(bot, _make_update(bot, 411, "/start", 501))
    await dispatcher.feed_update(bot, _make_update(bot, 411, "Изменить ФИО", 502))
    await dispatcher.feed_update(bot, _make_update(bot, 411, "Новое имя", 503))
    await dispatcher.feed_update(bot, _make_update(bot, 411, "Изменить группу", 504))
    await dispatcher.feed_update(bot, _make_update(bot, 411, "М3О-207СВ-25", 505))
    await dispatcher.feed_update(bot, _make_update(bot, 411, CHANGE_PAYMENT_DETAILS_BUTTON_TEXT, 506))
    confidential = "confidential-profile-value"
    await dispatcher.feed_update(bot, _make_update(bot, 411, confidential, 507))

    async with session_factory() as session:
        assert (await session.scalars(select(ChatMessage))).all() == []
        assert (await session.scalars(select(Conversation))).all() == []
        assert (await session.get(type(payout), payout_id)).status == PayoutStatus.sent.value
        assert (await session.get(type(recipient), recipient_id)).status == RecipientStatus.sent.value
        profile = await get_payment_profile(session, user.id)
        assert profile is not None and profile.raw_payment_details == confidential

    await dispatcher.feed_update(bot, _make_update(bot, 411, "Обычный текст", 508))
    async with session_factory() as session:
        messages = list((await session.scalars(select(ChatMessage))).all())
        assert len(messages) == 1 and messages[0].text == "Обычный текст"
    assert confidential not in caplog.text


@pytest.mark.asyncio
async def test_chat_entrypoint_is_hidden_by_default_and_enabled_only_with_flag(session_factory, settings):
    assert settings.chat_entrypoint_enabled is False
    assert all(CHAT_ENTRYPOINT_TEXT not in row for row in _keyboard_texts(main_keyboard()))
    enabled = settings.model_copy(update={"chat_entrypoint_enabled": True})
    assert _keyboard_texts(main_keyboard(chat_entrypoint_enabled=True))[-1] == [CHAT_ENTRYPOINT_TEXT]

    async with session_factory() as session:
        await get_or_create_user(session, 411, "Готовый пользователь")
        await session.commit()

    bot, recording = _make_bot()
    dispatcher = _make_dispatcher(session_factory, enabled)
    await dispatcher.feed_update(bot, _make_update(bot, 411, "/start", 600))
    assert _keyboard_texts(_last_message_method(recording).reply_markup)[-1] == [CHAT_ENTRYPOINT_TEXT]
    await dispatcher.feed_update(bot, _make_update(bot, 411, CHAT_ENTRYPOINT_TEXT, 601))
    assert _last_message_method(recording).text == "Напишите сообщение администратору."

    async with session_factory() as session:
        assert (await session.scalars(select(ChatMessage))).all() == []
    await dispatcher.feed_update(bot, _make_update(bot, 411, "Вопрос администратору", 602))
    async with session_factory() as session:
        messages = list((await session.scalars(select(ChatMessage))).all())
        assert len(messages) == 1 and messages[0].text == "Вопрос администратору"


@pytest.mark.asyncio
async def test_payment_details_survive_dispatcher_restart_and_never_enter_chat(
    session_factory, settings, caplog
):
    caplog.set_level(logging.INFO)
    async with session_factory() as session:
        user = await get_or_create_user(session, 711, "Зарегистрированный пользователь")
        payout = await create_payout(session, actor_telegram_id=123, payload=_payout_payload())
        payout.status = PayoutStatus.sent.value
        [recipient] = await add_recipients(session, payout, [user.id])
        recipient.status = RecipientStatus.sent.value
        await session.commit()
        recipient_id = recipient.id

    bot, _ = _make_bot()
    first = create_bot_dispatcher(session_factory, settings)
    await first.feed_update(bot, _make_update(bot, 711, CHANGE_PAYMENT_DETAILS_BUTTON_TEXT, 800))

    async with session_factory() as session:
        fsm_rows = list((await session.scalars(select(BotFSMState))).all())
        assert len(fsm_rows) == 1
        assert fsm_rows[0].state == "RegistrationFSM:payment_details"

    secret = "private-payment-snapshot-value"
    fresh = create_bot_dispatcher(session_factory, settings)
    await fresh.feed_update(bot, _make_update(bot, 711, secret, 801))

    async with session_factory() as session:
        profile = await get_payment_profile(session, user.id)
        assert profile is not None and profile.raw_payment_details == secret
        assert (await session.get(type(recipient), recipient_id)).status == RecipientStatus.sent.value
        assert (await session.scalars(select(ChatMessage))).all() == []
        assert (await session.scalars(select(Conversation))).all() == []
        assert (await session.scalars(select(BotSensitiveMessage))).all() != []

    # The payment update can be replayed after the FSM was cleared or the service restarted.
    another = create_bot_dispatcher(session_factory, settings)
    await another.feed_update(bot, _make_update(bot, 711, secret, 801))
    await another.feed_update(bot, _make_update(bot, 711, "Обычный вопрос", 802))
    async with session_factory() as session:
        messages = list((await session.scalars(select(ChatMessage))).all())
        assert len(messages) == 1
        assert messages[0].text == "Обычный вопрос"
    assert secret not in caplog.text


@pytest.mark.asyncio
async def test_concurrent_payment_updates_wait_for_fsm_before_chat_filter(
    session_factory, settings, monkeypatch, caplog
):
    caplog.set_level(logging.INFO)
    async with session_factory() as session:
        user = await get_or_create_user(session, 712, "Второй пользователь")
        await session.commit()

    entered = asyncio.Event()
    allow_payment_flow = asyncio.Event()
    original = bot_module.get_or_create_user

    async def delayed_user(session, telegram_id, *args, **kwargs):
        if telegram_id == 712 and not entered.is_set():
            entered.set()
            await allow_payment_flow.wait()
        return await original(session, telegram_id, *args, **kwargs)

    monkeypatch.setattr(bot_module, "get_or_create_user", delayed_user)
    bot, _ = _make_bot()
    dispatcher = create_bot_dispatcher(session_factory, settings)
    first = asyncio.create_task(
        dispatcher.feed_update(bot, _make_update(bot, 712, CHANGE_PAYMENT_DETAILS_BUTTON_TEXT, 900))
    )
    await asyncio.wait_for(entered.wait(), timeout=5)
    second = asyncio.create_task(
        dispatcher.feed_update(bot, _make_update(bot, 712, "private-racing-profile-value", 901))
    )
    try:
        await asyncio.sleep(0.1)
        assert not second.done()
    finally:
        allow_payment_flow.set()
    await asyncio.wait_for(asyncio.gather(first, second), timeout=10)

    async with session_factory() as session:
        profile = await get_payment_profile(session, user.id)
        assert profile is not None and profile.raw_payment_details == "private-racing-profile-value"
        assert (await session.scalars(select(ChatMessage))).all() == []
        assert (await session.scalars(select(Conversation))).all() == []
        assert (await session.scalars(select(BotSensitiveMessage))).all() != []
    assert "private-racing-profile-value" not in caplog.text


@pytest.mark.asyncio
async def test_first_rollout_drops_old_memory_storage_payment_input(
    session_factory, settings, caplog
):
    caplog.set_level(logging.INFO)
    async with session_factory() as session:
        user = await get_or_create_user(session, 715, "Пользователь со старым FSM")
        await session.commit()
        user_id = user.id

    bot, _ = _make_bot()
    old_bot = _make_dispatcher(session_factory, settings)
    await old_bot.feed_update(bot, _make_update(bot, 715, CHANGE_PAYMENT_DETAILS_BUTTON_TEXT, 1000))
    old_context = old_bot.fsm.get_context(bot=bot, chat_id=715, user_id=715)
    assert await old_context.get_state() == "RegistrationFSM:payment_details"

    async with session_factory() as session:
        assert (await session.scalars(select(BotFSMState))).all() == []

    secret = "rollout-confidential-bank-account"
    sse = get_admin_events_broadcaster().subscribe()
    try:
        first_release_settings = settings.model_copy(
            update={"chat_inbox_enabled": False, "chat_entrypoint_enabled": True}
        )
        first_release = create_bot_dispatcher(session_factory, first_release_settings)
        await first_release.feed_update(bot, _make_update(bot, 715, secret, 1001))
        assert sse.empty()

        # Even an accidentally enabled inbox cannot capture legacy pending input.
        enabled = create_bot_dispatcher(session_factory, settings)
        await enabled.feed_update(bot, _make_update(bot, 715, secret, 1002))
        assert sse.empty()
        async with session_factory() as session:
            assert (await session.scalars(select(BotFSMState))).all() == []
            assert (await session.scalars(select(Conversation))).all() == []
            assert (await session.scalars(select(ChatMessage))).all() == []
            assert (await session.scalar(select(User.id).where(User.id == user_id))) == user_id

        # An explicit /start in the new version resets the old flow and creates a trusted context.
        await enabled.feed_update(bot, _make_update(bot, 715, "/start", 1003))
        async with session_factory() as session:
            fsm_rows = list((await session.scalars(select(BotFSMState))).all())
            assert len(fsm_rows) == 1
            assert fsm_rows[0].state is None

        await enabled.feed_update(bot, _make_update(bot, 715, "Вопрос после /start", 1004))
        assert sse.get_nowait()["event"] == "chat.changed"
        async with session_factory() as session:
            messages = list((await session.scalars(select(ChatMessage))).all())
            assert len(messages) == 1
            assert messages[0].text == "Вопрос после /start"
    finally:
        get_admin_events_broadcaster().unsubscribe(sse)
    assert secret not in caplog.text


@pytest.mark.asyncio
async def test_disabled_inbox_cannot_capture_even_after_post_migration_start(session_factory, settings):
    async with session_factory() as session:
        await get_or_create_user(session, 716, "Зарегистрированная пользовательница")
        await session.commit()

    disabled = settings.model_copy(update={
        "chat_inbox_enabled": False, "chat_entrypoint_enabled": True,
    })
    bot, recorder = _make_bot()
    dispatcher = create_bot_dispatcher(session_factory, disabled)
    await dispatcher.feed_update(bot, _make_update(bot, 716, "/start", 1010))
    assert CHAT_ENTRYPOINT_TEXT not in str(_last_message_method(recorder).reply_markup)
    await dispatcher.feed_update(bot, _make_update(bot, 716, "Текст при выключенной переписке", 1011))
    async with session_factory() as session:
        assert (await session.scalars(select(BotFSMState))).all() != []
        assert (await session.scalars(select(ChatMessage))).all() == []
