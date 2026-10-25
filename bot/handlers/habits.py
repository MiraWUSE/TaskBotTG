"""Personal habits, today's check-in, history and reversible archiving."""

from datetime import date
from html import escape

from aiogram import F,Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State,StatesGroup
from aiogram.types import CallbackQuery,Message

from bot.handlers.middleware import answer_callback,edit_message
from bot.handlers.tasks import _number,_stale
from bot.keyboards.keyboards import inline
from bot.services.habits import HabitService
from bot.services.task_service import TaskService,TaskNotFound,ValidationError


class HabitFlow(StatesGroup):
    title=State()
    rename=State()


async def show_list(message,user_id,service,archived=False,page=0,*,edit=True):
    habits=HabitService(service)
    items,total=await habits.list(user_id,archived=archived,page=page)
    if total and not items:
        page=(total-1)//6
        items,total=await habits.list(user_id,archived=archived,page=page)
    text=f'🌱 <b>{"Архив привычек" if archived else "Привычки"}</b> · {total}\nЕжедневные отметки хранятся отдельно от задач.'
    rows=[[(f'{"☑️" if item.checked_today else "⬜"} {item.title[:32]} · 🔥 {item.streak}',f'habit:view:{item.id}')] for item in items]
    nav=[]
    if page:
        nav.append(('◀️',f'habit:list:{int(archived)}:{page-1}'))
    if (page+1)*6<total:
        nav.append(('▶️',f'habit:list:{int(archived)}:{page+1}'))
    if nav:
        rows.append(nav)
    rows+=[[('➕ Новая привычка','habit:new')],[('Активные' if archived else 'Архив',f'habit:list:{int(not archived)}:0')]]
    if edit:
        await edit_message(message,text,inline(rows))
    else:
        await message.answer(text,reply_markup=inline(rows),parse_mode='HTML')


async def card(message,user_id,service,item,*,edit=True):
    user=await service.get_user(user_id)
    text=(f'🌱 <b>{escape(item.title)}</b>\n📅 {item.today:%d.%m.%Y} · {escape(user.timezone)}\n\n'
          f'{"✅ Сегодня выполнено" if item.checked_today else "⬜ Сегодня ещё не отмечено"}\n'
          f'🔥 Серия: {item.streak} дн. · рекорд: {item.best}\n'
          f'Всего отметок: {item.total}\nЗа 7 дней: {item.week}\n'
          f'За последние {item.month_days} дн.: {item.month} ({round(100*item.month/item.month_days)}%)')
    rows=[]
    if item.active:
        action='undo' if item.checked_today else 'done'
        rows.append([('Снять отметку' if item.checked_today else '✅ Выполнено сегодня',f'habit:{action}:{item.id}:{item.revision}:{item.today.isoformat()}')])
    else:
        text+='\n🗃 В архиве'
    rows+=[[('📜 История',f'habit:history:{item.id}'),('✏️ Название',f'habit:rename:{item.id}:{item.revision}')],
           [('🗃 В архив' if item.active else '♻️ Восстановить',f'habit:{"archive" if item.active else "restore"}:{item.id}:{item.revision}')],
           [('◀️ К привычкам',f'habit:list:{int(not item.active)}:0')]]
    if edit:
        await edit_message(message,text,inline(rows))
    else:
        await message.answer(text,reply_markup=inline(rows),parse_mode='HTML')


async def show_habits(message: Message,state: FSMContext,service: TaskService) -> None:
    await state.clear()
    await show_list(message,message.from_user.id,service,edit=False)


async def habit_callback(callback: CallbackQuery,state: FSMContext,service: TaskService) -> None:
    parts=(callback.data or '').split(':')
    habits=HabitService(service)
    try:
        if parts==['habit','new']:
            await state.clear()
            await state.set_state(HabitFlow.title)
            await answer_callback(callback)
            await callback.message.answer('🌱 Какую привычку будем отмечать каждый день? До 200 символов. Отмена: /cancel')
            return
        if len(parts)==4 and parts[1]=='list' and parts[2] in {'0','1'}:
            page=_number(parts[3],zero=True)
            await state.clear()
            await answer_callback(callback)
            await show_list(callback.message,callback.from_user.id,service,parts[2]=='1',page)
            return
        if len(parts)<3:
            raise ValueError
        item=await habits.get(callback.from_user.id,_number(parts[2]))
        action=parts[1]
        if len(parts)==3 and action in {'view','history'}:
            if action=='history':
                text=f'📜 <b>{escape(item.title)}</b>\nПоследние отметки (до 30):\n'+('\n'.join(item.history) or 'Пока нет отметок.')
                await answer_callback(callback)
                await edit_message(callback.message,text,inline([[('◀️ Назад',f'habit:view:{item.id}')]]))
                return
        elif len(parts) in {4,5} and action in {'done','undo','rename','archive','confirm','restore'}:
            if _number(parts[3],zero=True)!=item.revision:
                raise ValueError
            if action in {'done','undo'}:
                if len(parts)!=5:
                    raise ValueError
                item=await habits.check(callback.from_user.id,item.id,done=action=='done',day=date.fromisoformat(parts[4]),revision=item.revision)
            elif len(parts)!=4:
                raise ValueError
            elif action=='rename':
                await state.clear()
                await state.set_state(HabitFlow.rename)
                await state.update_data(habit_id=item.id,revision=item.revision)
                await answer_callback(callback)
                await callback.message.answer('✏️ Новое название привычки. Отмена: /cancel')
                return
            elif action=='archive':
                await answer_callback(callback)
                await edit_message(callback.message,'Убрать привычку в архив? История сохранится.',inline([
                    [('Да, в архив',f'habit:confirm:{item.id}:{item.revision}')],[('Отмена',f'habit:view:{item.id}')]]))
                return
            else:
                item=await habits.change(callback.from_user.id,item.id,active=action=='restore',revision=item.revision)
        else:
            raise ValueError
        await state.clear()
        await answer_callback(callback)
        await card(callback.message,callback.from_user.id,service,item)
    except ValidationError as error:
        await answer_callback(callback,str(error),alert=True)
    except (ValueError,TaskNotFound):
        await _stale(callback)


async def receive_title(message: Message,state: FSMContext,service: TaskService) -> None:
    habits=HabitService(service)
    data=await state.get_data()
    try:
        if await state.get_state()==HabitFlow.title.state:
            item=await habits.add(message.from_user.id,message.text)
        else:
            item=await habits.change(message.from_user.id,data['habit_id'],title=message.text or '',revision=data['revision'])
    except ValidationError as error:
        await message.answer(str(error)+' Отмена: /cancel')
        return
    except (KeyError,TaskNotFound):
        await state.clear()
        await message.answer('Привычка изменилась. Открой /habits заново.')
        return
    await state.clear()
    await card(message,message.from_user.id,service,item,edit=False)


def register_navigation(router: Router) -> None:
    router.message.register(show_habits,Command('habits'))


def register_handlers(router: Router) -> None:
    router.callback_query.register(habit_callback,F.data.startswith('habit:'))
    router.message.register(receive_title,HabitFlow.title)
    router.message.register(receive_title,HabitFlow.rename)
