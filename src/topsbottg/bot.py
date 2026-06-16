from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    WebAppInfo,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from topsbottg.logging_utils import log_event
from topsbottg.models import PayoutRecipient
from topsbottg.services import (
    confirm_saved_profile,
    get_latest_active_recipient,
    get_or_create_user,
    get_payment_profile,
    save_profile_and_snapshot,
    start_user,
    upsert_full_name,
)

logger = logging.getLogger(__name__)


class RegistrationFSM(StatesGroup):
    registration_full_name = State()
    change_full_name = State()
    payment_decision = State()
    payment_details = State()


YES_BUTTON_TEXT = "Да"
PAYMENT_LATER_BUTTON_TEXT = "Заполнить позже"
MY_DATA_BUTTON_TEXT = "Мои данные"
CHANGE_NAME_BUTTON_TEXT = "Изменить ФИО"
PAYMENT_DETAILS_BUTTON_TEXT = "Платёжные данные"
PAYMENT_DETAILS_BUTTON_TEXT_ALT = "Платежные данные"
CHANGE_PAYMENT_DETAILS_BUTTON_TEXT = "Изменить платёжные данные"
ADMIN_ENTRYPOINT_TEXT = "Админка"
ADMIN_INLINE_BUTTON_TEXT = "Открыть админку"


def _is_admin(settings, telegram_id: int) -> bool:
    return telegram_id in settings.admin_ids_set


def main_keyboard(*, is_admin: bool = False, mini_app_url: str | None = None) -> ReplyKeyboardMarkup:
    keyboard = [
        [KeyboardButton(text=MY_DATA_BUTTON_TEXT), KeyboardButton(text=CHANGE_NAME_BUTTON_TEXT)],
        [KeyboardButton(text=CHANGE_PAYMENT_DETAILS_BUTTON_TEXT)],
    ]
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)


def _main_keyboard_for(message: Message, settings) -> ReplyKeyboardMarkup:
    return main_keyboard(is_admin=_is_admin(settings, message.from_user.id), mini_app_url=settings.mini_app_url)


def admin_keyboard(mini_app_url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=ADMIN_INLINE_BUTTON_TEXT, web_app=WebAppInfo(url=mini_app_url))]]
    )


async def _safe_message_answer(message: Message, telegram_user_id: int, *args, **kwargs):
    try:
        return await message.answer(*args, **kwargs)
    except Exception as exc:
        log_event(
            logger,
            "WARNING",
            "telegram_api_call_failed",
            "Ошибка вызова Telegram API",
            telegram_user_id=telegram_user_id,
            operation="message.answer",
            error_type=type(exc).__name__,
        )
        raise


async def _safe_callback_answer(callback: CallbackQuery, telegram_user_id: int, *args, **kwargs):
    try:
        return await callback.answer(*args, **kwargs)
    except Exception as exc:
        log_event(
            logger,
            "WARNING",
            "telegram_api_call_failed",
            "Ошибка вызова Telegram API",
            telegram_user_id=telegram_user_id,
            operation="callback.answer",
            error_type=type(exc).__name__,
        )
        raise


async def _show_admin_entrypoint(message: Message, settings) -> None:
    if not _is_admin(settings, message.from_user.id):
        log_event(
            logger,
            "WARNING",
            "bot_admin_entrypoint_denied",
            "Вход в админку отклонён",
            telegram_user_id=message.from_user.id,
            reason="non_admin",
        )
        await _safe_message_answer(message, message.from_user.id, "Команда доступна только админам.")
        return
    if not settings.mini_app_url:
        log_event(
            logger,
            "WARNING",
            "bot_admin_entrypoint_denied",
            "Вход в админку отклонён",
            telegram_user_id=message.from_user.id,
            reason="mini_app_url_missing",
        )
        await _safe_message_answer(message, message.from_user.id, "Админка недоступна.")
        return
    await _safe_message_answer(
        message,
        message.from_user.id,
        "Откройте админку.",
        reply_markup=admin_keyboard(settings.mini_app_url),
    )
    log_event(
        logger,
        "INFO",
        "bot_admin_entrypoint_sent",
        "Бот отправил inline-кнопку админки",
        telegram_user_id=message.from_user.id,
    )


def payment_decision_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=YES_BUTTON_TEXT), KeyboardButton(text=PAYMENT_LATER_BUTTON_TEXT)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def payment_details_message() -> str:
    return (
        "Отправьте платёжные данные одним сообщением в свободной форме.\n\n"
        "Например:\n"
        "СБП +7 999 999-99-99, Т-Банк, Иван Иванович И.\n"
        "или\n"
        "карта 1111 1111 1111 1111, Сбер, Иван Иванович И."
    )


def recipient_actions_keyboard(recipient_id: int, *, has_profile: bool) -> InlineKeyboardMarkup:
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


async def _start_payment_details_flow(
    message: Message,
    state: FSMContext,
    session_factory: async_sessionmaker,
    *,
    active_recipient_id: int | None = None,
) -> None:
    if active_recipient_id is None:
        async with session_factory() as session:
            user = await get_or_create_user(session, message.from_user.id)
            active_recipient = await get_latest_active_recipient(session, user.id)
            log_event(
                logger,
                "INFO",
                "bot_payment_details_update_started",
                "Пользователь начал изменение платежных данных",
                telegram_user_id=message.from_user.id,
                user_id=user.id,
                active_recipient_id=getattr(active_recipient, "id", None),
            )
        active_recipient_id = active_recipient.id if active_recipient is not None else None
    else:
        async with session_factory() as session:
            user = await get_or_create_user(session, message.from_user.id)
            log_event(
                logger,
                "INFO",
                "bot_payment_details_update_started",
                "Пользователь начал изменение платежных данных",
                telegram_user_id=message.from_user.id,
                user_id=user.id,
                active_recipient_id=active_recipient_id,
            )
    await state.clear()
    await state.set_state(RegistrationFSM.payment_details)
    await state.update_data(active_recipient_id=active_recipient_id)
    await _safe_message_answer(
        message,
        message.from_user.id,
        payment_details_message(),
        reply_markup=ReplyKeyboardRemove(),
    )


async def _save_payment_details(
    message: Message,
    state: FSMContext,
    session_factory: async_sessionmaker,
    settings,
) -> None:
    raw_text = (message.text or "").strip()
    if not raw_text:
        await _safe_message_answer(
            message,
            message.from_user.id,
            "Отправьте непустые платёжные данные одним сообщением.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return
    data = await state.get_data()
    active_recipient_id = data.get("active_recipient_id")
    async with session_factory() as session:
        user = await get_or_create_user(session, message.from_user.id)
        active_recipient = None
        if active_recipient_id is not None:
            active_recipient = await session.get(PayoutRecipient, active_recipient_id)
            if active_recipient is not None and active_recipient.user_id != user.id:
                active_recipient = None
        if active_recipient is None:
            active_recipient = await get_latest_active_recipient(session, user.id)
        await save_profile_and_snapshot(
            session,
            user=user,
            payload=raw_text,
            active_recipient=active_recipient,
        )
        await session.commit()
    await state.clear()
    log_event(
        logger,
        "INFO",
        "bot_payment_details_update_completed",
        "Платёжные данные пользователя обновлены",
        telegram_user_id=message.from_user.id,
        user_id=user.id,
    )
    await _safe_message_answer(
        message,
        message.from_user.id,
        "Платёжные данные сохранены.",
        reply_markup=_main_keyboard_for(message, settings),
    )


def build_router(session_factory: async_sessionmaker, _settings) -> Router:
    router = Router()

    @router.message(CommandStart())
    async def start(message: Message, state: FSMContext):
        log_event(
            logger,
            "INFO",
            "bot_start_received",
            "Пользователь отправил /start",
            telegram_user_id=message.from_user.id,
            is_admin=_is_admin(_settings, message.from_user.id),
        )
        async with session_factory() as session:
            _user, needs_name = await start_user(session, message.from_user.id)
            await session.commit()
        await state.clear()
        if needs_name:
            await state.set_state(RegistrationFSM.registration_full_name)
            log_event(
                logger,
                "INFO",
                "bot_registration_started",
                "Пользователь начал регистрацию",
                telegram_user_id=message.from_user.id,
            )
            await _safe_message_answer(message, message.from_user.id, "Здравствуйте. Для регистрации укажите ФИО.")
            return
        main_markup = _main_keyboard_for(message, _settings)
        await _safe_message_answer(
            message,
            message.from_user.id,
            "Вы уже зарегистрированы. Что хотите изменить?",
            reply_markup=main_markup,
        )

    @router.message(Command("admin"))
    async def admin_command(message: Message):
        log_event(
            logger,
            "INFO",
            "bot_admin_entrypoint_requested",
            "Пользователь запросил вход в админку",
            telegram_user_id=message.from_user.id,
            is_admin=_is_admin(_settings, message.from_user.id),
            mini_app_url_present=bool(_settings.mini_app_url),
        )
        await _show_admin_entrypoint(message, _settings)

    @router.message(F.text == ADMIN_ENTRYPOINT_TEXT)
    async def admin_text(message: Message):
        log_event(
            logger,
            "INFO",
            "bot_admin_entrypoint_requested",
            "Пользователь запросил вход в админку",
            telegram_user_id=message.from_user.id,
            is_admin=_is_admin(_settings, message.from_user.id),
            mini_app_url_present=bool(_settings.mini_app_url),
        )
        await _show_admin_entrypoint(message, _settings)

    @router.message(RegistrationFSM.registration_full_name)
    async def save_registration_name(message: Message, state: FSMContext):
        full_name = (message.text or "").strip()
        if not full_name:
            await _safe_message_answer(message, message.from_user.id, "Введите ФИО.")
            return
        async with session_factory() as session:
            user = await upsert_full_name(session, message.from_user.id, full_name)
            active_recipient = await get_latest_active_recipient(session, user.id)
            await session.commit()
        await state.clear()
        await state.set_state(RegistrationFSM.payment_decision)
        await state.update_data(active_recipient_id=active_recipient.id if active_recipient is not None else None)
        await _safe_message_answer(
            message,
            message.from_user.id,
            "ФИО сохранено. Хотите добавить платёжные данные сейчас?",
            reply_markup=payment_decision_keyboard(),
        )

    @router.message(RegistrationFSM.change_full_name)
    async def save_changed_name(message: Message, state: FSMContext):
        full_name = (message.text or "").strip()
        if not full_name:
            await _safe_message_answer(message, message.from_user.id, "Введите новое ФИО.")
            return
        async with session_factory() as session:
            await upsert_full_name(session, message.from_user.id, full_name)
            await session.commit()
        await state.clear()
        await _safe_message_answer(
            message,
            message.from_user.id,
            "ФИО обновлено.",
            reply_markup=_main_keyboard_for(message, _settings),
        )

    @router.message(RegistrationFSM.payment_decision, F.text == YES_BUTTON_TEXT)
    async def yes_fill_payment(message: Message, state: FSMContext):
        data = await state.get_data()
        await _start_payment_details_flow(
            message,
            state,
            session_factory,
            active_recipient_id=data.get("active_recipient_id"),
        )

    @router.message(RegistrationFSM.payment_decision, F.text == PAYMENT_LATER_BUTTON_TEXT)
    async def maybe_later(message: Message, state: FSMContext):
        await state.clear()
        main_markup = _main_keyboard_for(message, _settings)
        await _safe_message_answer(
            message,
            message.from_user.id,
            "Готово, вы зарегистрированы. Платёжные данные можно добавить позже через меню.",
            reply_markup=main_markup,
        )

    @router.message(RegistrationFSM.payment_decision, F.text)
    async def payment_decision_invalid(message: Message):
        await _safe_message_answer(
            message,
            message.from_user.id,
            "Выберите одну из кнопок.",
            reply_markup=payment_decision_keyboard(),
        )

    @router.message(F.text == MY_DATA_BUTTON_TEXT)
    async def my_data(message: Message):
        async with session_factory() as session:
            user = await get_or_create_user(session, message.from_user.id)
            profile = await get_payment_profile(session, user.id)
            has_payment_details = bool(profile and profile.raw_payment_details)
            text = (
                "Ваши данные\n\n"
                f"ФИО: {user.full_name}\n"
                f"Платёжные данные: {'есть' if has_payment_details else 'не заполнены'}"
            )
            if not has_payment_details:
                text += "\nИх можно добавить через «Изменить платёжные данные»."
        await _safe_message_answer(
            message,
            message.from_user.id,
            text,
            reply_markup=_main_keyboard_for(message, _settings),
        )

    @router.message(F.text == CHANGE_NAME_BUTTON_TEXT)
    async def change_name(message: Message, state: FSMContext):
        await state.clear()
        await state.set_state(RegistrationFSM.change_full_name)
        await _safe_message_answer(message, message.from_user.id, "Введите новое ФИО.")

    @router.message(F.text.in_([PAYMENT_DETAILS_BUTTON_TEXT, PAYMENT_DETAILS_BUTTON_TEXT_ALT]))
    async def payment_start(message: Message, state: FSMContext):
        await _start_payment_details_flow(message, state, session_factory)

    @router.message(F.text == CHANGE_PAYMENT_DETAILS_BUTTON_TEXT)
    async def change_payment_details(message: Message, state: FSMContext):
        await _start_payment_details_flow(message, state, session_factory)

    @router.message(RegistrationFSM.payment_details, F.text)
    async def payment_details(message: Message, state: FSMContext):
        await _save_payment_details(message, state, session_factory, _settings)

    @router.callback_query(F.data.startswith("fill_payment:"))
    async def fill_payment(callback: CallbackQuery, state: FSMContext):
        recipient_id = int(callback.data.split(":", 1)[1])
        async with session_factory() as session:
            user = await get_or_create_user(session, callback.from_user.id)
            recipient = await session.scalar(
                select(PayoutRecipient).where(PayoutRecipient.id == recipient_id, PayoutRecipient.user_id == user.id)
            )
            if recipient is None:
                log_event(
                    logger,
                    "WARNING",
                    "bot_callback_rejected",
                    "Callback отклонён",
                    telegram_user_id=callback.from_user.id,
                    callback_type="fill_payment",
                    reason="recipient_not_found",
                )
                await _safe_callback_answer(
                    callback,
                    callback.from_user.id,
                    "Не удалось открыть данные.",
                    show_alert=False,
                )
                return
        if callback.message is None:
            log_event(
                logger,
                "WARNING",
                "bot_callback_rejected",
                "Callback отклонён",
                telegram_user_id=callback.from_user.id,
                callback_type="fill_payment",
                reason="missing_message",
            )
            await _safe_callback_answer(
                callback,
                callback.from_user.id,
                "Не удалось открыть данные.",
                show_alert=False,
            )
            return
        await _start_payment_details_flow(
            callback.message,
            state,
            session_factory,
            active_recipient_id=recipient_id,
        )
        await _safe_callback_answer(callback, callback.from_user.id)

    @router.callback_query(F.data.startswith("confirm_profile:"))
    async def confirm_saved_data(callback: CallbackQuery):
        recipient_id = int(callback.data.split(":", 1)[1])
        async with session_factory() as session:
            user = await get_or_create_user(session, callback.from_user.id)
            recipient = await confirm_saved_profile(session, recipient_id=recipient_id, user_id=user.id)
            if recipient is None:
                raw_recipient = await session.scalar(select(PayoutRecipient).where(PayoutRecipient.id == recipient_id))
                if raw_recipient is not None and raw_recipient.user_id != user.id:
                    log_event(
                        logger,
                        "WARNING",
                        "bot_callback_rejected",
                        "Callback отклонён",
                        telegram_user_id=callback.from_user.id,
                        callback_type="confirm_profile",
                        reason="idor",
                    )
                else:
                    log_event(
                        logger,
                        "WARNING",
                        "bot_callback_rejected",
                        "Callback отклонён",
                        telegram_user_id=callback.from_user.id,
                        callback_type="confirm_profile",
                        reason="recipient_not_found_or_invalid_status",
                    )
                await session.rollback()
                await _safe_callback_answer(
                    callback,
                    callback.from_user.id,
                    "Не удалось подтвердить данные.",
                    show_alert=False,
                )
                return
            await session.commit()
        log_event(
            logger,
            "INFO",
            "bot_profile_confirmed",
            "Пользователь подтвердил профиль",
            telegram_user_id=callback.from_user.id,
            user_id=user.id,
        )
        await _safe_callback_answer(callback, callback.from_user.id, "Ваше сообщение сохранено.", show_alert=False)

    return router
