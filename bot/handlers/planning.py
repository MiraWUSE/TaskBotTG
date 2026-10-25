"""Calendar navigation and the compact planning menu."""

import calendar
from datetime import date, timedelta
from html import escape
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.handlers.middleware import answer_callback, edit_message
from bot.handlers.tasks import PAGE_SIZE, TaskFlow, _number, _stale, _task_text, _task_version
from bot.keyboards.keyboards import PLANNING, inline, task_keyboard
from bot.services.calendar import CalendarService
from bot.services.task_service import TaskNotFound, TaskService
from bot.task_options import priority_icon

MONTHS = ('','Январь','Февраль','Март','Апрель','Май','Июнь','Июль','Август','Сентябрь','Октябрь','Ноябрь','Декабрь')


async def planning_menu(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer('🗓 Планирование', reply_markup=inline([[('📆 Календарь', 'cal:open')]]))


async def show_month(message: Message, user_id: int, service: TaskService, year: int, month: int, *, edit=True):
    first = date(year, month, 1)
    if not 2 <= year <= 9998:
        raise ValueError('Год вне диапазона календаря.')
    end = (first.replace(day=28)+timedelta(days=4)).replace(day=1)
    occupied = await CalendarService(service).dates(user_id,first,end)
    user = await service.get_user(user_id)
    today = service.now().astimezone(ZoneInfo(user.timezone)).date()
    rows = [[(label,'cal:noop') for label in ('Пн','Вт','Ср','Чт','Пт','Сб','Вс')]]
    for week in calendar.monthcalendar(year,month):
        row = []
        for number in week:
            if not number:
                row.append(('·','cal:noop'))
                continue
            day = date(year,month,number)
            label = f'{number}•' if day in occupied else str(number)
            if day == today:
                label = f'[{label}]'
            row.append((label,f'cal:day:{day.isoformat()}:0'))
        rows.append(row)
    previous = first-timedelta(days=1)
    rows.append([('◀️',f'cal:month:{previous:%Y-%m}'),(f'{MONTHS[month]} {year}','cal:noop'),('▶️',f'cal:month:{end:%Y-%m}')])
    rows.append([('Сегодня',f'cal:day:{today.isoformat()}:0'),('Текущий месяц','cal:open')])
    text = f'📆 <b>{MONTHS[month]} {year}</b>\n🌍 {escape(user.timezone)}\n• — есть задачи, [ ] — сегодня. Выбери день.'
    if edit:
        await edit_message(message,text,inline(rows))
    else:
        await message.answer(text,reply_markup=inline(rows),parse_mode='HTML')


async def show_calendar(message: Message, state: FSMContext, service: TaskService) -> None:
    await state.clear()
    user = await service.get_user(message.from_user.id)
    today = service.now().astimezone(ZoneInfo(user.timezone)).date()
    await show_month(message,user.id,service,today.year,today.month,edit=False)


async def calendar_callback(callback: CallbackQuery, state: FSMContext, service: TaskService) -> None:
    parts = (callback.data or '').split(':')
    try:
        if parts == ['cal','noop']:
            await answer_callback(callback)
            return
        user = await service.get_user(callback.from_user.id)
        if parts == ['cal','open']:
            today = service.now().astimezone(ZoneInfo(user.timezone)).date()
            await state.clear()
            await answer_callback(callback)
            await show_month(callback.message,user.id,service,today.year,today.month)
            return
        if len(parts) == 3 and parts[1] == 'month':
            day = date.fromisoformat(parts[2]+'-01')
            await state.clear()
            await answer_callback(callback)
            await show_month(callback.message,user.id,service,day.year,day.month)
            return
        if len(parts) == 4 and parts[1] == 'task':
            task = await service.get_task(user.id, _number(parts[2]))
            day = date.fromisoformat(parts[3])
            keyboard = task_keyboard(task.id,task.repeat,False,0,_task_version(task))
            keyboard.inline_keyboard += inline([[('📆 К выбранному дню',f'cal:day:{day.isoformat()}:0')]]).inline_keyboard
            await state.clear()
            await answer_callback(callback)
            await edit_message(callback.message, f'📆 Выбран день: {day:%d.%m.%Y}\nКарточка показывает ближайший срок задачи/серии.\n\n'+_task_text(task,user.timezone,service.now()),keyboard)
            return
        if len(parts) == 3 and parts[1] == 'new':
            day = date.fromisoformat(parts[2])
            if day < service.now().astimezone(ZoneInfo(user.timezone)).date():
                raise ValueError
            await state.clear()
            await state.set_state(TaskFlow.title)
            await state.update_data(calendar_day=day.isoformat())
            await answer_callback(callback)
            await callback.message.answer(f'📆 {day:%d.%m.%Y}\n📝 Введи название задачи. Далее выберем время. Отмена: /cancel')
            return
        if len(parts) != 4 or parts[1] != 'day':
            raise ValueError
        day = date.fromisoformat(parts[2])
        page = _number(parts[3],zero=True)
        if page > 1_000_000:
            raise ValueError
        tasks = await CalendarService(service).day(user.id,day)
        if tasks:
            page = min(page,(len(tasks)-1)//PAGE_SIZE)
        text = [f'📆 <b>{day:%d.%m.%Y}</b> · задач: {len(tasks)}',f'🌍 {escape(user.timezone)}']
        rows = []
        for task in tasks[page*PAGE_SIZE:(page+1)*PAGE_SIZE]:
            text.append(f'{priority_icon(task.priority)} {task.remind_at.astimezone(ZoneInfo(user.timezone)):%H:%M} — {escape(task.title[:100])}')
            rows.append([(task.title[:35],f'cal:task:{task.id}:{day.isoformat()}')])
        navigation = []
        if page:
            navigation.append(('◀️',f'cal:day:{day.isoformat()}:{page-1}'))
        if (page+1)*PAGE_SIZE < len(tasks):
            navigation.append(('▶️',f'cal:day:{day.isoformat()}:{page+1}'))
        if navigation:
            rows.append(navigation)
        if day >= service.now().astimezone(ZoneInfo(user.timezone)).date():
            rows.append([('➕ Создать на эту дату',f'cal:new:{day.isoformat()}')])
        rows.append([('◀️ К месяцу',f'cal:month:{day:%Y-%m}')])
        await state.clear()
        await answer_callback(callback)
        await edit_message(callback.message,'\n'.join(text),inline(rows))
    except (ValueError, OverflowError, TaskNotFound):
        await _stale(callback)


def register_navigation(router: Router) -> None:
    router.message.register(planning_menu,F.text == PLANNING)
    router.message.register(show_calendar,Command('calendar'))


def register_handlers(router: Router) -> None:
    router.callback_query.register(calendar_callback,F.data.startswith('cal:'))
