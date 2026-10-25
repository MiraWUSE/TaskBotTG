"""Paginated checklists and small stateful add/edit forms."""

from html import escape

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from bot.handlers.middleware import answer_callback, edit_message
from bot.handlers.tasks import PAGE_SIZE, _number, _stale, _task_version
from bot.keyboards.keyboards import inline
from bot.services.subtasks import SubtaskService
from bot.services.task_service import TaskNotFound, TaskService, ValidationError


class SubtaskFlow(StatesGroup):
    add = State()
    rename = State()


async def show_subtasks(message, user_id, service, task_id, page=0, *, edit=True):
    task = await service.get_task(user_id,task_id)
    items = await SubtaskService(service).list(user_id,task_id)
    page = min(page,max(0,(len(items)-1)//PAGE_SIZE))
    text = [f'☑️ <b>{escape(task.title)}</b>',f'Прогресс: {sum(i.done for i in items)} из {len(items)} выполнено']
    rows = []
    for item in items[page*PAGE_SIZE:(page+1)*PAGE_SIZE]:
        mark = '☑️' if item.done else '⬜'
        text.append(f'{mark} {escape(item.title[:80])}')
        rows.append([(f'{mark} {item.title[:32]}',f'sub:item:{item.id}')])
    nav = []
    if page:
        nav.append(('◀️',f'sub:list:{task_id}:{page-1}'))
    if (page+1)*PAGE_SIZE < len(items):
        nav.append(('▶️',f'sub:list:{task_id}:{page+1}'))
    if nav:
        rows.append(nav)
    rows += [[('➕ Добавить',f'sub:add:{task_id}')],[('◀️ К задаче',f'task:view:{task_id}:0:0')]]
    if edit:
        await edit_message(message,'\n'.join(text),inline(rows))
    else:
        await message.answer('\n'.join(text),reply_markup=inline(rows),parse_mode='HTML')


async def subtask_callback(callback: CallbackQuery, state: FSMContext, service: TaskService) -> None:
    parts = (callback.data or '').split(':')
    try:
        if len(parts) == 4 and parts[1] == 'list':
            task_id,page = _number(parts[2]),_number(parts[3],zero=True)
            if page > 1_000_000:
                raise ValueError
            await service.get_task(callback.from_user.id,task_id)
            await state.clear()
            await answer_callback(callback)
            await show_subtasks(callback.message,callback.from_user.id,service,task_id,page)
            return
        if len(parts) == 3 and parts[1] == 'add':
            task = await service.get_task(callback.from_user.id,_number(parts[2]))
            await state.clear()
            await state.set_state(SubtaskFlow.add)
            await state.update_data(task_id=task.id,version=_task_version(task))
            await answer_callback(callback)
            await callback.message.answer('➕ Название подзадачи — до 200 символов. Отмена: /cancel')
            return
        subtasks = SubtaskService(service)
        if len(parts) == 3 and parts[1] == 'item':
            item = await subtasks.get(callback.from_user.id,_number(parts[2]))
        elif len(parts) == 4 and parts[1] in {'done','undo','rename','delete','confirm'}:
            item = await subtasks.get(callback.from_user.id,_number(parts[2]))
            if item.revision != _number(parts[3],zero=True):
                raise ValueError
            if parts[1] == 'delete':
                await answer_callback(callback)
                await edit_message(callback.message,'🗑 Удалить подзадачу?\n'+escape(item.title),inline([
                    [('Да, удалить',f'sub:confirm:{item.id}:{item.revision}')], [('Отмена',f'sub:item:{item.id}')]]))
                return
            if parts[1] == 'rename':
                await state.clear()
                await state.set_state(SubtaskFlow.rename)
                await state.update_data(subtask_id=item.id,revision=item.revision)
                await answer_callback(callback)
                await callback.message.answer('✏️ Новое название подзадачи. Отмена: /cancel')
                return
            await subtasks.change(callback.from_user.id,item.id,done=None if parts[1]=='confirm' else parts[1]=='done',
                                  deleted=parts[1]=='confirm',revision=item.revision)
            await state.clear()
            await answer_callback(callback,'Готово')
            await show_subtasks(callback.message,callback.from_user.id,service,item.task_id)
            return
        else:
            raise ValueError
        await state.clear()
        await answer_callback(callback)
        await edit_message(callback.message,f'{"☑️" if item.done else "⬜"} <b>{escape(item.title)}</b>',inline([
            [('Снять отметку' if item.done else '✅ Выполнено',f'sub:{"undo" if item.done else "done"}:{item.id}:{item.revision}')],
            [('✏️ Изменить',f'sub:rename:{item.id}:{item.revision}'),('🗑 Удалить',f'sub:delete:{item.id}:{item.revision}')],
            [('◀️ К подзадачам',f'sub:list:{item.task_id}:0')]]))
    except (ValueError,TaskNotFound):
        await _stale(callback)


async def receive_subtask(message: Message, state: FSMContext, service: TaskService) -> None:
    data = await state.get_data()
    subtasks = SubtaskService(service)
    try:
        if await state.get_state() == SubtaskFlow.add.state:
            task = await service.get_task(message.from_user.id,data['task_id'])
            if _task_version(task) != data['version']:
                raise TaskNotFound()
            item = await subtasks.add(message.from_user.id,task.id,message.text)
            task_id = item.task_id
        else:
            item = await subtasks.get(message.from_user.id,data['subtask_id'])
            await subtasks.change(message.from_user.id,item.id,title=message.text or '',revision=data['revision'])
            task_id = item.task_id
    except (TaskNotFound,KeyError):
        await state.clear()
        await message.answer('Задача изменилась или недоступна. Открой её заново через /tasks.')
        return
    except ValidationError as error:
        await message.answer(str(error)+' Отмена: /cancel')
        return
    await state.clear()
    await show_subtasks(message,message.from_user.id,service,task_id,edit=False)


def register_handlers(router: Router) -> None:
    router.callback_query.register(subtask_callback,F.data.startswith('sub:'))
    router.message.register(receive_subtask,SubtaskFlow.add)
    router.message.register(receive_subtask,SubtaskFlow.rename)
