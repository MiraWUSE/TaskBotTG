"""Quick creation is registered after navigation and active FSM handlers."""

from datetime import datetime
from html import escape
from uuid import uuid4

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from bot.handlers.middleware import answer_callback, edit_message
from bot.keyboards.keyboards import inline, main_menu, task_keyboard
from bot.services.task_service import TaskService, ValidationError
from bot.utils.datetime_utils import format_datetime
from bot.utils.quick_task import parse_quick_task


class QuickFlow(StatesGroup):
    confirm = State()
    edit = State()


async def confirm_draft(message: Message, state: FSMContext, service: TaskService,
                        user_id: int, title: str, moment: datetime) -> None:
    user = await service.get_user(user_id)
    nonce = uuid4().hex[:10]
    await state.clear()
    await state.update_data(title=title, remind_at=moment.isoformat(), nonce=nonce)
    await state.set_state(QuickFlow.confirm)
    await message.answer(
        f'📝 <b>Новая задача</b>\n\nЗадача: {escape(title)}\n📅 {format_datetime(moment, user.timezone)}\n🌍 {escape(user.timezone)}',
        reply_markup=inline([
            [('✅ Сохранить', f'quick:{nonce}:save')],
            [('✏️ Изменить', f'quick:{nonce}:edit'), ('❌ Отмена', f'quick:{nonce}:cancel')],
        ]), parse_mode='HTML',
    )


async def receive_quick(message: Message, state: FSMContext, service: TaskService) -> None:
    from bot.handlers.start import unknown_message
    from bot.handlers.tasks import _start_schedule

    user = await service.get_user(message.from_user.id)
    try:
        draft = parse_quick_task(message.text, user.timezone, service.now())
        if draft is None:
            await unknown_message(message)
            return
        if draft.moment is None:
            await _start_schedule(message, state, mode='quick', title=draft.title)
            await message.answer(draft.clarification)
        else:
            await confirm_draft(message, state, service, user.id, draft.title, draft.moment)
    except ValueError as error:
        await message.answer(f'⚠️ {escape(str(error))}', parse_mode='HTML')


async def quick_callback(callback: CallbackQuery, state: FSMContext, service: TaskService) -> None:
    from bot.handlers.tasks import STALE_TEXT, _task_text, _task_version

    parts = (callback.data or '').split(':')
    data = await state.get_data()
    if (len(parts) != 3 or parts[1] != data.get('nonce')
            or await state.get_state() != QuickFlow.confirm.state
            or parts[2] not in {'save', 'edit', 'cancel'}):
        await answer_callback(callback, STALE_TEXT, alert=True)
        return
    if parts[2] == 'cancel':
        await state.clear()
        await answer_callback(callback, 'Отменено')
        await edit_message(callback.message, 'Создание отменено. Главное меню доступно внизу 👇')
        return
    if parts[2] == 'edit':
        await state.set_state(QuickFlow.edit)
        await answer_callback(callback)
        await callback.message.answer('✏️ Отправь исправленную задачу целиком, например «Завтра в 15:00 сходить в магазин». Отмена: /cancel')
        return
    try:
        task = await service.create_task(callback.from_user.id, data['title'], datetime.fromisoformat(data['remind_at']))
    except (ValueError, ValidationError):
        await state.set_state(QuickFlow.edit)
        await answer_callback(callback, 'Время прошло. Укажи задачу с будущей датой.', alert=True)
        return
    await state.clear()
    user = await service.get_user(callback.from_user.id)
    await answer_callback(callback, 'Задача создана')
    await edit_message(callback.message, '✅ Задача создана!\n\n' + _task_text(task, user.timezone, service.now()),
                       task_keyboard(task.id, task.repeat, False, 0, _task_version(task)))


async def confirmation_hint(message: Message) -> None:
    await message.answer('Проверь задачу и нажми «Сохранить», «Изменить» или «Отмена».', reply_markup=main_menu())


def register_handlers(router: Router) -> None:
    router.callback_query.register(quick_callback, F.data.startswith('quick:'))
    router.message.register(receive_quick, QuickFlow.edit, F.text)
    router.message.register(confirmation_hint, QuickFlow.confirm)
    router.message.register(receive_quick, StateFilter(None), F.text)
