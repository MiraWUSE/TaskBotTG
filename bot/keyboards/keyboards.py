"""Small, mobile-friendly reply and inline keyboards."""

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)
from bot.task_options import priority_icon

NEW_TASK = "➕ Создать задачу"
MY_TASKS = "📋 Мои задачи"
TODAY = "📅 Задачи на сегодня"
SETTINGS = "⚙️ Настройки"
HELP = "❓ Помощь"
MAIN_MENU = "🏠 Главное меню"

REPEAT_LABELS = {
    "none": "Однократно",
    "daily": "Каждый день",
    "weekdays": "Каждый будний день",
    "weekly": "Каждую неделю",
}


def inline(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=text, callback_data=data) for text, data in row]
            for row in rows
        ]
    )


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=NEW_TASK)],
            [KeyboardButton(text=MY_TASKS), KeyboardButton(text=TODAY)],
            [KeyboardButton(text=SETTINGS), KeyboardButton(text=HELP)],
            [KeyboardButton(text=MAIN_MENU)],
        ],
        resize_keyboard=True,
        input_field_placeholder="Выбери действие или напиши /new",
    )


def time_keyboard(nonce: str) -> InlineKeyboardMarkup:
    return inline(
        [
            [("Через 15 минут", f"when:{nonce}:15"), ("Через 30 минут", f"when:{nonce}:30")],
            [("Через 1 час", f"when:{nonce}:60")],
            [("Сегодня", f"when:{nonce}:today"), ("Завтра", f"when:{nonce}:tomorrow")],
            [("📆 Ввести дату и время", f"when:{nonce}:manual")],
            [("Отмена", f"flow:cancel:{nonce}")],
        ]
    )


def repeat_keyboard(nonce: str) -> InlineKeyboardMarkup:
    return inline(
        [[(label, f"repeat:{nonce}:{value}")] for value, label in REPEAT_LABELS.items()]
        + [[("Отмена", f"flow:cancel:{nonce}")]]
    )


def tasks_keyboard(tasks: list, total: int, page: int, today: bool, page_size: int,
                   sort_by: str = 'time') -> InlineKeyboardMarkup:
    kind = int(today)
    rows = []
    for task in tasks:
        title = task.title.replace("\n", " ")
        if len(title) > 38:
            title = title[:37] + "…"
        rows.append([(f"{priority_icon(task.priority)} {title}", f"task:view:{task.id}:{kind}:{page}")])
    navigation = []
    if page:
        navigation.append(("◀️ Назад", f"list:{kind}:{page - 1}"))
    if (page + 1) * page_size < total:
        navigation.append(("Далее ▶️", f"list:{kind}:{page + 1}"))
    if navigation:
        rows.append(navigation)
    rows.append([("🔄 Обновить", f"list:{kind}:{page}")])
    rows.append([('⏰ По времени' if sort_by == 'priority' else '🚦 По приоритету',
                  f"sort:{'time' if sort_by == 'priority' else 'priority'}:{kind}")])
    rows.append([('🗂 Категории', f'filters:{kind}')])
    return inline(rows)


def task_keyboard(task_id: int, repeat: str, today: bool, page: int, version: str) -> InlineKeyboardMarkup:
    suffix = f"{task_id}:{int(today)}:{page}:{version}"
    rows = [
        [("✏️ Изменить название", f"task:rename:{suffix}")],
        [("📆 Изменить дату и время", f"task:time:{suffix}")],
        [('🚦 Приоритет', f'task:priority:{suffix}')],
        [('🗂 Категория', f'task:category:{suffix}')],
        [('⏳ Напомнить заранее', f'task:advance:{suffix}')],
        [("✅ Выполнено", f"task:done:{suffix}")],
    ]
    if repeat == "none":
        rows.append([("🗑 Удалить", f"task:delete:{suffix}")])
    else:
        rows.append([("🛑 Остановить повторение", f"task:stop:{suffix}")])
    rows.append([("◀️ К списку", f"list:{int(today)}:{page}")])
    return inline(rows)


def reminder_keyboard(delivery_id: int) -> InlineKeyboardMarkup:
    return inline(
        [
            [("✅ Выполнено", f"rem:done:{delivery_id}")],
            [("⏰ Отложить", f"rem:snooze:{delivery_id}"), ("🗑 Удалить", f"rem:delete:{delivery_id}")],
        ]
    )


def snooze_keyboard(delivery_id: int) -> InlineKeyboardMarkup:
    return inline(
        [
            [("На 10 минут", f"rem:delay:{delivery_id}:10"), ("На 30 минут", f"rem:delay:{delivery_id}:30")],
            [("На 1 час", f"rem:delay:{delivery_id}:60")],
            [("📆 Выбрать другое время", f"rem:delay:{delivery_id}:manual")],
            [("◀️ Назад", f"rem:back:{delivery_id}")],
        ]
    )


def settings_keyboard() -> InlineKeyboardMarkup:
    return inline([[("🌍 Изменить часовой пояс", "settings:timezone")], [('☀️ Утренняя сводка', 'summary:show')]])
