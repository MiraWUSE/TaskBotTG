"""Small personal statistics view, without extra main-menu buttons."""

from html import escape

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.handlers.middleware import answer_callback, edit_message
from bot.keyboards.keyboards import inline
from bot.services.statistics import StatisticsService
from bot.services.task_service import TaskService


async def statistics_view(user_id,service,period='week'):
    stats = await StatisticsService(service).get(user_id,period)
    user = await service.get_user(user_id)
    label = {'day':'сегодня','week':'неделю','month':'месяц'}[period]
    percentage = f'{stats.percent}%' if stats.percent is not None else 'нет наступивших сроков'
    text = (f'📊 <b>Статистика</b>\n🌍 {escape(user.timezone)}\n\n'
            f'✅ Выполнено сегодня: {stats.today}\nЗа неделю: {stats.week}\nЗа месяц: {stats.month}\n'
            f'⌛ Сейчас просрочено: {stats.overdue}\n\n🎯 По срокам за {label}: {percentage}\n'
            f'Выполнено {stats.due_completed} из {stats.due_total} наступивших сроков.\n\n'
            'У повторов учитываются отдельные выполнения. Входящие без даты не входят в процент; удалённые и перенесённые сроки исключены.')
    return text,inline([[('Сегодня','stats:day'),('Неделя','stats:week'),('Месяц','stats:month')]])


async def show_statistics(message: Message,state: FSMContext,service: TaskService) -> None:
    await state.clear()
    text,keyboard = await statistics_view(message.from_user.id,service)
    await message.answer(text,reply_markup=keyboard,parse_mode='HTML')


async def statistics_callback(callback: CallbackQuery,state: FSMContext,service: TaskService) -> None:
    period = (callback.data or '').removeprefix('stats:')
    if period not in {'day','week','month'}:
        await answer_callback(callback,'Кнопка устарела.',alert=True)
        return
    await state.clear()
    await answer_callback(callback)
    text,keyboard = await statistics_view(callback.from_user.id,service,period)
    await edit_message(callback.message,text,keyboard)


def register_navigation(router: Router) -> None:
    router.message.register(show_statistics,Command('stats'))


def register_handlers(router: Router) -> None:
    router.callback_query.register(statistics_callback,F.data.startswith('stats:'))
