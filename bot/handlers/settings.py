"""Per-user IANA timezone selection."""

from html import escape

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from bot.handlers.middleware import answer_callback
from bot.keyboards.keyboards import SETTINGS, main_menu, settings_keyboard
from bot.services.task_service import TaskService, ValidationError
from bot.utils.datetime_utils import validate_timezone


class TimezoneFlow(StatesGroup):
    timezone = State()


async def show_settings(message: Message, state: FSMContext, service: TaskService) -> None:
    await state.clear()
    user = await service.get_user(message.from_user.id)
    await message.answer(
        f"⚙️ <b>Настройки</b>\n\n🌍 Часовой пояс: <code>{escape(user.timezone)}</code>\n"
        "Даты и время ввода и списков учитывают этот пояс.",
        reply_markup=settings_keyboard(),
        parse_mode="HTML",
    )


async def timezone_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(TimezoneFlow.timezone)
    await answer_callback(callback)
    await callback.message.answer(
        "🌍 Введи часовой пояс IANA, например:\n"
        "<code>Europe/Moscow</code>\n<code>Europe/Helsinki</code>\n"
        "<code>Asia/Yekaterinburg</code>\n\nОтмена: /cancel",
        reply_markup=main_menu(),
        parse_mode="HTML",
    )


async def save_timezone(message: Message, state: FSMContext, service: TaskService) -> None:
    try:
        timezone = validate_timezone((message.text or "").strip())
        user = await service.set_timezone(message.from_user.id, timezone)
    except (ValidationError, ValueError):
        await message.answer(
            "Не удалось найти этот часовой пояс. Введи IANA-идентификатор, "
            "например <code>Europe/Moscow</code>. Отмена: /cancel",
            parse_mode="HTML",
        )
        return
    await state.clear()
    await message.answer(
        f"✅ Часовой пояс сохранён: <code>{escape(user.timezone)}</code>",
        reply_markup=main_menu(),
        parse_mode="HTML",
    )


def register_navigation(router: Router) -> None:
    router.message.register(show_settings, Command("settings"))
    router.message.register(show_settings, F.text == SETTINGS)


def register_handlers(router: Router) -> None:
    router.callback_query.register(timezone_prompt, F.data == "settings:timezone")
    router.message.register(save_timezone, TimezoneFlow.timezone)
