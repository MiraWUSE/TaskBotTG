"""Per-user IANA timezone selection."""

from html import escape
import re

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from bot.handlers.middleware import answer_callback, edit_message
from bot.keyboards.keyboards import SETTINGS, inline, main_menu, settings_keyboard
from bot.services.summaries import SummaryService
from bot.services.quiet_hours import QuietHoursService
from bot.services.task_service import TaskService, ValidationError
from bot.utils.datetime_utils import validate_timezone


class TimezoneFlow(StatesGroup):
    timezone = State()
    summary_time = State()
    quiet_range = State()


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
    router.callback_query.register(quiet_callback,F.data.startswith('quiet:'))
    router.message.register(save_quiet_range,TimezoneFlow.quiet_range)
    router.callback_query.register(summary_callback, F.data.startswith('summary:'))
    router.message.register(save_summary_time, TimezoneFlow.summary_time)
    router.callback_query.register(timezone_prompt, F.data == "settings:timezone")
    router.message.register(save_timezone, TimezoneFlow.timezone)


async def summary_callback(callback: CallbackQuery, state: FSMContext, service: TaskService) -> None:
    action = (callback.data or '').removeprefix('summary:')
    if action not in {'show','enable','disable','empty_yes','empty_no','time'}:
        await answer_callback(callback, 'Кнопка устарела.', alert=True)
        return
    await state.clear()
    if action == 'time':
        await state.set_state(TimezoneFlow.summary_time)
        await answer_callback(callback)
        await callback.message.answer('☀️ Во сколько присылать сводку? Например 8:00. Время в твоём часовом поясе. Отмена: /cancel')
        return
    summary = SummaryService(service)
    if action in {'enable','disable'}:
        await summary.configure(callback.from_user.id, enabled=action == 'enable')
    elif action.startswith('empty_'):
        await summary.configure(callback.from_user.id, send_empty=action == 'empty_yes')
    user = await service.get_user(callback.from_user.id)
    await answer_callback(callback)
    await edit_message(callback.message,
        f'☀️ <b>Утренняя сводка</b>\n\n{"Включена" if user.summary_enabled else "Выключена"} · {user.summary_time}\n🌍 {escape(user.timezone)}\nПустые дни: {"присылать" if user.summary_empty else "пропускать"}',
        inline([[('Выключить' if user.summary_enabled else 'Включить', 'summary:disable' if user.summary_enabled else 'summary:enable')],
                [('⏰ Изменить время', 'summary:time')],
                [('Пропускать пустые дни' if user.summary_empty else 'Присылать в пустые дни', 'summary:empty_no' if user.summary_empty else 'summary:empty_yes')]]))


async def save_summary_time(message: Message, state: FSMContext, service: TaskService) -> None:
    try:
        await SummaryService(service).configure(message.from_user.id, clock=message.text or '')
    except ValidationError as error:
        await message.answer(str(error) + ' Отмена: /cancel')
        return
    await state.clear()
    await message.answer('✅ Время сводки сохранено. Изменения начнут действовать со следующего будущего времени отправки.', reply_markup=settings_keyboard())


async def quiet_callback(callback: CallbackQuery,state: FSMContext,service: TaskService) -> None:
    action=(callback.data or '').removeprefix('quiet:')
    if action not in {'show','enable','disable','range','except_yes','except_no'}:
        await answer_callback(callback,'Кнопка устарела.',alert=True)
        return
    await state.clear()
    if action=='range':
        await state.set_state(TimezoneFlow.quiet_range)
        await answer_callback(callback)
        await callback.message.answer('🌙 Введи интервал, например 23:00–08:00. Можно через дефис: 23:00-8:00. В личном часовом поясе. Отмена: /cancel')
        return
    quiet=QuietHoursService(service)
    if action in {'enable','disable'}:
        await quiet.configure(callback.from_user.id,enabled=action=='enable')
    elif action.startswith('except_'):
        await quiet.configure(callback.from_user.id,exempt_high=action=='except_yes')
    user=await service.get_user(callback.from_user.id)
    await answer_callback(callback)
    await edit_message(callback.message,
        f'🌙 <b>Тихие часы</b>\n\n{"Включены" if user.quiet_enabled else "Выключены"} · {user.quiet_start}–{user.quiet_end}\n'
        f'🌍 {escape(user.timezone)}\n🔴 Высокий приоритет: {"пропускать сразу" if user.quiet_exempt_high else "ждать окончания"}\n\n'
        'Обычные напоминания и сводка ждут окончания интервала. Исходный срок задачи сохраняется.',
        inline([[('Выключить' if user.quiet_enabled else 'Включить','quiet:disable' if user.quiet_enabled else 'quiet:enable')],
                [('⏰ Изменить интервал','quiet:range')],
                [('🔴 Тоже откладывать' if user.quiet_exempt_high else '🔴 Разрешить высокий приоритет','quiet:except_no' if user.quiet_exempt_high else 'quiet:except_yes')]]))


async def save_quiet_range(message: Message,state: FSMContext,service: TaskService) -> None:
    try:
        match=re.fullmatch(r'\s*(.+?)\s*[-–—]\s*(.+?)\s*',message.text or '')
        if match is None:
            raise ValidationError('Укажи интервал, например 23:00–08:00.')
        await QuietHoursService(service).configure(message.from_user.id,start=match[1],end=match[2])
    except ValidationError as error:
        await message.answer(str(error)+' Отмена: /cancel')
        return
    await state.clear()
    await message.answer('✅ Интервал сохранён. Включение и исключения — в настройках тихих часов.',reply_markup=settings_keyboard())
