"""Main menu, cancellation and Russian help."""

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from bot.keyboards.keyboards import HELP, MAIN_MENU, main_menu

HELP_TEXT = (
    "/stats — статистика, /habits — привычки. Тихие часы включаются в настройках.\n\n"
    "Планирование: /calendar — календарь, /inbox — задачи без даты.\n"
    "В карточке есть подзадачи и прогресс; в настройках — утренняя сводка.\n\n"
    "Быстро: отправь «Завтра в 15:00 сходить в магазин» и подтверди сохранение.\n\n"
    "❓ <b>Как пользоваться ботом</b>\n\n"
    "Нажми «➕ Создать задачу», введи название и выбери время. "
    "Например: <code>25.10.2026 8:00</code> или <code>завтра в 9:30</code>. "
    "Можно отправить дату отдельно: <code>25.10.2026</code> или <code>сегодня</code>, затем время. "
    "Время можно написать как <code>8:00</code>, <code>8.30</code> или <code>8</code> (08:00).\n\n"
    "Поддерживаются ежедневные, будние и еженедельные повторы. "
    "Кнопки под напоминанием завершают или откладывают конкретное напоминание. "
    "В карточке задачи можно остановить всю серию.\n\n"
    "/new — создать задачу\n"
    "/tasks — мои задачи\n"
    "/today — задачи на сегодня\n"
    "/settings — часовой пояс\n"
    "/cancel — отменить ввод\n"
    "/start — главное меню\n\n"
    "Время указано в твоём часовом поясе из настроек. "
    "Любая команда меню прерывает текущий ввод."
)


async def show_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "👋 Привет! Я помогу помнить о важных делах.\n\n"
        "Создавай задачи, выбирай время и получай напоминания прямо здесь. "
        "Часовой пояс можно проверить в настройках.\n\nВыбери действие 👇",
        reply_markup=main_menu(),
    )


async def show_help(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(HELP_TEXT, reply_markup=main_menu(), parse_mode="HTML")


async def cancel(message: Message, state: FSMContext) -> None:
    current = await state.get_state()
    await state.clear()
    await message.answer(
        "Действие отменено. Выбери, что сделать дальше 👇" if current else "Главное меню 👇",
        reply_markup=main_menu(),
    )


def register_navigation(router: Router) -> None:
    router.message.register(show_start, Command("start"))
    router.message.register(show_start, F.text == MAIN_MENU)
    router.message.register(show_help, Command("help"))
    router.message.register(show_help, F.text == HELP)
    router.message.register(cancel, Command("cancel"))


async def unknown_message(message: Message) -> None:
    await message.answer(
        "Выбери действие в меню. Чтобы создать задачу, нажми «➕ Создать задачу» или отправь /new.",
        reply_markup=main_menu(),
    )
