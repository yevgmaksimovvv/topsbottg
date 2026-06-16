from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup, Message, ReplyKeyboardMarkup, ReplyKeyboardRemove, Update, WebAppInfo
from sqlalchemy import select

from topsbottg.bot import build_router, main_keyboard
from topsbottg.models import PaymentProfile, PayoutStatus, RecipientStatus
from topsbottg.services import add_recipients, create_payout, get_or_create_user, get_payment_profile


class RecordingSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.methods: list = []

    async def make_request(self, bot: Bot, method, timeout=None):  # noqa: ANN001
        self.methods.append(method)
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


def _last_method(session: RecordingSession):
    assert session.methods, "bot did not send any message"
    return session.methods[-1]


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

    method = _last_method(recording_session)
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

    method = _last_method(recording_session)
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
async def test_admin_command_shows_inline_web_app_button_for_admin(session_factory, settings):
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


@pytest.mark.asyncio
async def test_admin_command_is_not_available_for_non_admin(session_factory, settings):
    async with session_factory() as session:
        await get_or_create_user(session, 111, "Обычный Пользователь")
        await session.commit()

    bot, recording_session = _make_bot()
    dispatcher = _make_dispatcher(session_factory, settings)

    await dispatcher.feed_update(bot, _make_update(bot, 111, "/admin", 1))

    method = _last_method(recording_session)
    assert method.text == "Команда доступна только админам."
    assert method.reply_markup is None


@pytest.mark.asyncio
async def test_registration_full_name_asks_payment_decision_only_on_first_registration(session_factory, settings):
    async with session_factory() as session:
        user = await get_or_create_user(session, 111, "Без ФИО")
        payout = await create_payout(
            session,
            actor_telegram_id=123,
            payload={
                "title": "Июнь",
                "period_from": datetime(2026, 3, 1).date(),
                "period_to": datetime(2026, 3, 31).date(),
                "message_template": None,
            },
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
            payload={
                "title": "Июнь",
                "period_from": datetime(2026, 3, 1).date(),
                "period_to": datetime(2026, 3, 31).date(),
                "message_template": None,
            },
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
async def test_payment_details_saved_returns_main_keyboard(session_factory, settings):
    async with session_factory() as session:
        user = await get_or_create_user(session, 111, "Без ФИО")
        payout = await create_payout(
            session,
            actor_telegram_id=123,
            payload={
                "title": "Июнь",
                "period_from": datetime(2026, 3, 1).date(),
                "period_to": datetime(2026, 3, 31).date(),
                "message_template": None,
            },
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

    method = _last_method(recording_session)
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


@pytest.mark.asyncio
async def test_empty_payment_details_rejected(session_factory, settings):
    async with session_factory() as session:
        user = await get_or_create_user(session, 111, "Без ФИО")
        payout = await create_payout(
            session,
            actor_telegram_id=123,
            payload={
                "title": "Июнь",
                "period_from": datetime(2026, 3, 1).date(),
                "period_to": datetime(2026, 3, 31).date(),
                "message_template": None,
            },
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
            payload={
                "title": "Июнь",
                "period_from": datetime(2026, 3, 1).date(),
                "period_to": datetime(2026, 3, 31).date(),
                "message_template": None,
            },
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
