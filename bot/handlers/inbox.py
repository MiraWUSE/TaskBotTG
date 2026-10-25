"""Explicit capture of undated tasks; plain chat text stays unchanged."""

from html import escape

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from bot.handlers.middleware import answer_callback, edit_message
from bot.handlers.tasks import PAGE_SIZE, _number, _stale, _task_text, _task_version
from bot.keyboards.keyboards import inline, task_keyboard
from bot.services.task_service import TaskService, ValidationError
from bot.task_options import priority_icon


class InboxFlow(StatesGroup):
    title = State()


async def inbox_list(message, user_id, service, page=0, *, edit=True):
    tasks,total = await service.list_tasks(user_id,page=page,page_size=PAGE_SIZE,inbox=True,category='all')
    if total and not tasks:
        page = (total-1)//PAGE_SIZE
        tasks,total = await service.list_tasks(user_id,page=page,page_size=PAGE_SIZE,inbox=True,category='all')
    lines = [f'📥 <b>Входящие</b> · {total}\nЗдесь задачи без даты и уведомлений.']
    rows = []
    for task in tasks:
        lines.append(f'{priority_icon(task.priority)} {escape(task.title[:80])}')
        rows.append([(task.title[:35],f'task:view:{task.id}:0:0')])
    nav = []
    if page:
        nav.append(('◀️',f'inbox:list:{page-1}'))
    if (page+1)*PAGE_SIZE < total:
        nav.append(('▶️',f'inbox:list:{page+1}'))
    if nav:
        rows.append(nav)
    rows += [[('➕ Записать задачу','inbox:add')],[('🔄 Обновить',f'inbox:list:{page}')]]
    if edit:
        await edit_message(message,'\n'.join(lines),inline(rows))
    else:
        await message.answer('\n'.join(lines),reply_markup=inline(rows),parse_mode='HTML')


async def show_inbox(message: Message, state: FSMContext, service: TaskService) -> None:
    await state.clear()
    # /inbox <title> is an explicit capture command, never a date guess.
    title = (message.text or '').partition(' ')[2].strip()
    if title:
        await save_inbox(message,state,service,title=title)
    else:
        await inbox_list(message,message.from_user.id,service,edit=False)


async def inbox_callback(callback: CallbackQuery, state: FSMContext, service: TaskService) -> None:
    parts = (callback.data or '').split(':')
    if parts == ['inbox','add']:
        await state.clear()
        await state.set_state(InboxFlow.title)
        await answer_callback(callback)
        await callback.message.answer('📥 Что записать? Название до 200 символов. Дату можно назначить позже. Отмена: /cancel')
        return
    try:
        if parts == ['inbox','open']:
            page = 0
        elif len(parts)==3 and parts[1]=='list':
            page = _number(parts[2],zero=True)
            if page > 1_000_000:
                raise ValueError
        else:
            raise ValueError
    except ValueError:
        await _stale(callback)
        return
    await state.clear()
    await answer_callback(callback)
    await inbox_list(callback.message,callback.from_user.id,service,page)


async def save_inbox(message: Message, state: FSMContext, service: TaskService, *, title=None) -> None:
    try:
        task = await service.create_task(message.from_user.id,message.text if title is None else title,None)
    except ValidationError as error:
        await message.answer(str(error)+' Отмена: /cancel')
        return
    await state.clear()
    user = await service.get_user(message.from_user.id)
    await message.answer('✅ Записано во входящие!\n\n'+_task_text(task,user.timezone,service.now()),
                         reply_markup=task_keyboard(task.id,task.repeat,False,0,_task_version(task),inbox=True),parse_mode='HTML')


def register_navigation(router: Router) -> None:
    router.message.register(show_inbox,Command('inbox'))


def register_handlers(router: Router) -> None:
    router.callback_query.register(inbox_callback,F.data.startswith('inbox:'))
    router.message.register(save_inbox,InboxFlow.title)
