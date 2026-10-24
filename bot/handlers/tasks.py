"""Task creation, lists, editing and notification actions."""

from datetime import date, datetime, timedelta
from hashlib import sha256
from html import escape
from uuid import uuid4
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from bot.handlers.middleware import answer_callback, edit_message
from bot.keyboards.keyboards import (
    MY_TASKS,
    NEW_TASK,
    REPEAT_LABELS,
    TODAY,
    inline,
    main_menu,
    reminder_keyboard,
    repeat_keyboard,
    snooze_keyboard,
    task_keyboard,
    tasks_keyboard,
    time_keyboard,
)
from bot.services.task_service import TaskNotFound, TaskService, ValidationError
from bot.utils.datetime_utils import combine_local, format_datetime, parse_datetime, parse_time, parse_user_date

PAGE_SIZE = 6
STALE_TEXT = "Эта кнопка устарела или задача недоступна. Открой /tasks."


class TaskFlow(StatesGroup):
    title = State()
    when = State()
    time = State()
    repeat = State()
    rename = State()


def _number(text: str, *, zero: bool = False) -> int:
    if not text.isascii() or not text.isdecimal() or len(text) > 19:
        raise ValueError("invalid id")
    value = int(text)
    if value < (0 if zero else 1) or value > 2**63 - 1:
        raise ValueError("invalid id")
    return value


def _title(text: str | None) -> str:
    title = " ".join((text or "").split())
    if not title or len(title) > 200 or any(ord(char) < 32 for char in title):
        raise ValueError("invalid title")
    return title


def _status(task, now: datetime) -> str:
    if task.status in {"completed", "done"}:
        return "✅ Выполнена"
    return "🔔 Ожидает выполнения" if task.remind_at <= now else "⏳ Запланирована"


def _task_version(task) -> str:
    """Identify the displayed occurrence without inflating callback payloads."""
    value = f"{task.id}:{task.generation}:{getattr(task, 'revision', 0)}:{int(task.remind_at.timestamp())}"
    return sha256(value.encode()).hexdigest()[:12]


def _task_text(task, timezone: str, now: datetime) -> str:
    repeat = REPEAT_LABELS.get(task.repeat, "Повторяется")
    text = (
        f"📝 <b>{escape(task.title)}</b>\n\n"
        f"📅 {format_datetime(task.remind_at, timezone)}\n"
        f"🌍 {escape(timezone)}\n"
        f"🔁 {repeat}\n"
        f"{_status(task, now)}"
    )
    if task.repeat != "none":
        text += "\n\n«Выполнено» завершает только ближайшее напоминание."
    return text


async def _stale(callback: CallbackQuery) -> None:
    await answer_callback(callback, STALE_TEXT, alert=True)


async def _start_schedule(
    message: Message,
    state: FSMContext,
    *,
    mode: str,
    title: str,
    **values,
) -> None:
    await state.clear()
    nonce = uuid4().hex[:10]
    await state.update_data(mode=mode, title=title, nonce=nonce, **values)
    await state.set_state(TaskFlow.when)
    await message.answer(
        f"📝 <b>{escape(title)}</b>\n\nКогда напомнить?\n"
        "Например: <code>25.10.2026 8:00</code> или <code>завтра в 9:30</code>.\n"
        "Можно сначала отправить только дату: <code>25.10.2026</code> или <code>завтра</code>.\n\nОтмена: /cancel",
        reply_markup=time_keyboard(nonce),
        parse_mode="HTML",
    )


async def start_creation(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(TaskFlow.title)
    await message.answer(
        "📝 Какую задачу тебе нужно напомнить?\nВведи название до 200 символов.\n\nОтмена: /cancel",
        reply_markup=main_menu(),
    )


async def receive_title(message: Message, state: FSMContext) -> None:
    try:
        title = _title(message.text)
    except ValueError:
        await message.answer("Напиши непустое название задачи — до 200 символов. Отмена: /cancel")
        return
    await _start_schedule(message, state, mode="create", title=title)


async def _remove_original_controls(bot: Bot, data: dict) -> None:
    if "source_chat_id" not in data or "source_message_id" not in data:
        return
    try:
        await bot.edit_message_reply_markup(
            chat_id=data["source_chat_id"], message_id=data["source_message_id"], reply_markup=None
        )
    except TelegramAPIError:
        # Delivery state in SQLite makes the old buttons harmless even if the
        # original notification can no longer be edited.
        pass


async def _choose_datetime(
    message: Message,
    state: FSMContext,
    service: TaskService,
    user_id: int,
    moment: datetime,
    bot: Bot,
) -> None:
    data = await state.get_data()
    user = await service.get_user(user_id)
    if moment <= service.now():
        raise ValueError("Это время уже прошло. Выбери будущие дату и время.")
    if data.get("mode") == "create":
        await state.update_data(remind_at=moment.isoformat())
        await state.set_state(TaskFlow.repeat)
        await message.answer(
            f"📅 {format_datetime(moment, user.timezone)}\n\n🔁 Как часто повторять напоминание?",
            reply_markup=repeat_keyboard(data["nonce"]),
        )
    elif data.get("mode") == "reschedule":
        task = await service.reschedule_task(user_id, data["task_id"], moment)
        await state.clear()
        await message.answer(
            "✅ Время задачи изменено!\n\n" + _task_text(task, user.timezone, service.now()),
            reply_markup=task_keyboard(task.id, task.repeat, data.get("today", False), data.get("page", 0), _task_version(task)),
            parse_mode="HTML",
        )
    elif data.get("mode") == "snooze":
        await service.snooze_delivery(user_id, data["delivery_id"], moment)
        await state.clear()
        await _remove_original_controls(bot, data)
        await message.answer(
            f"⏰ Напоминание отложено на {format_datetime(moment, user.timezone)}.\n"
            f"📝 {escape(data['title'])}",
            reply_markup=main_menu(),
            parse_mode="HTML",
        )
    else:
        await state.clear()
        await message.answer("Ввод устарел. Начни заново с /new или открой /tasks.", reply_markup=main_menu())


async def choose_quick_time(callback: CallbackQuery, state: FSMContext, service: TaskService, bot: Bot) -> None:
    parts = (callback.data or "").split(":")
    data = await state.get_data()
    if len(parts) != 3 or parts[1] != data.get("nonce") or await state.get_state() not in {
        TaskFlow.when.state, TaskFlow.time.state
    }:
        await _stale(callback)
        return
    choice = parts[2]
    if choice not in {"15", "30", "60", "today", "tomorrow", "manual"}:
        await _stale(callback)
        return
    await answer_callback(callback)
    user = await service.get_user(callback.from_user.id)
    if choice in {"15", "30", "60"}:
        moment = service.now() + timedelta(minutes=int(choice))
        try:
            await _choose_datetime(callback.message, state, service, callback.from_user.id, moment, bot)
        except TaskNotFound:
            await state.clear()
            await callback.message.answer(STALE_TEXT, reply_markup=main_menu())
        except (ValidationError, ValueError) as error:
            await callback.message.answer(
                f"⚠️ {escape(str(error))}\n\nПопробуй другое время или /cancel.",
                parse_mode="HTML",
            )
        return
    if choice == "manual":
        await state.set_state(TaskFlow.when)
        await state.update_data(day=None)
        await callback.message.answer(
            "Введи дату и время: <code>25.10.2026 8:00</code> или <code>завтра в 9:30</code>.\n"
            "Можно сначала дату: <code>25.10.2026</code> или <code>завтра</code>.\nОтмена: /cancel",
            parse_mode="HTML",
        )
        return
    day = service.now().astimezone(ZoneInfo(user.timezone)).date()
    if choice == "tomorrow":
        day += timedelta(days=1)
    await state.update_data(day=day.isoformat())
    await state.set_state(TaskFlow.time)
    await callback.message.answer(
        f"📅 {day.strftime('%d.%m.%Y')}\nВо сколько напомнить? Например: <code>8:00</code>, "
        "<code>8.30</code> или <code>8</code> (08:00).\nОтмена: /cancel",
        parse_mode="HTML",
    )


async def receive_when(message: Message, state: FSMContext, service: TaskService, bot: Bot) -> None:
    text = (message.text or "").strip()
    user = await service.get_user(message.from_user.id)
    now = service.now()
    try:
        try:
            moment = parse_datetime(text, user.timezone, now=now)
        except ValueError as datetime_error:
            # Date-only input is also valid. Do not guess from whitespace:
            # clocks and the optional «в» can themselves contain spaces.
            try:
                day = parse_user_date(text, user.timezone, now=now)
            except ValueError:
                raise datetime_error from None
            if day < now.astimezone(ZoneInfo(user.timezone)).date():
                raise ValueError("Эта дата уже прошла. Выбери сегодня или будущую дату.")
            await state.update_data(day=day.isoformat())
            await state.set_state(TaskFlow.time)
            await message.answer(
                f"📅 {day.strftime('%d.%m.%Y')}\nВведи время: <code>8:00</code>, "
                "<code>8.30</code> или <code>8</code> (08:00).\nОтмена: /cancel",
                parse_mode="HTML",
            )
            return
        await _choose_datetime(message, state, service, message.from_user.id, moment, bot)
    except TaskNotFound:
        await state.clear()
        await message.answer(STALE_TEXT, reply_markup=main_menu())
    except (ValidationError, ValueError) as error:
        await message.answer(
            f"⚠️ {escape(str(error))}\n\n"
            "Например: <code>25.10.2026 8:00</code> или <code>завтра в 9:30</code>.\nОтмена: /cancel",
            parse_mode="HTML",
        )


async def receive_time(message: Message, state: FSMContext, service: TaskService, bot: Bot) -> None:
    data = await state.get_data()
    user = await service.get_user(message.from_user.id)
    try:
        day = date.fromisoformat(data["day"])
        clock = parse_time((message.text or "").strip())
        moment = combine_local(day, clock, user.timezone, now=service.now())
        await _choose_datetime(message, state, service, message.from_user.id, moment, bot)
    except TaskNotFound:
        await state.clear()
        await message.answer(STALE_TEXT, reply_markup=main_menu())
    except KeyError:
        await message.answer("Выбери дату кнопкой «Ввести дату и время» выше или отправь /cancel.")
    except (ValidationError, ValueError) as error:
        await message.answer(
            f"⚠️ {escape(str(error))}\n\n"
            "Время можно написать так: <code>8:00</code>, <code>8.30</code> или <code>8</code>.\n"
            "Если нужен другой день, нажми «Ввести дату и время» выше. Отмена: /cancel",
            parse_mode="HTML",
        )


async def choose_repeat(callback: CallbackQuery, state: FSMContext, service: TaskService) -> None:
    parts = (callback.data or "").split(":")
    data = await state.get_data()
    if (
        len(parts) != 3 or parts[1] != data.get("nonce")
        or parts[2] not in REPEAT_LABELS or await state.get_state() != TaskFlow.repeat.state
    ):
        await _stale(callback)
        return
    try:
        moment = datetime.fromisoformat(data["remind_at"])
        task = await service.create_task(callback.from_user.id, data["title"], moment, parts[2])
    except (KeyError, ValidationError, ValueError) as error:
        # A weekend cannot anchor a weekday series. Returning to scheduling also
        # recovers from an expired date after a long pause in the conversation.
        await state.set_state(TaskFlow.when)
        await state.update_data(day=None)
        await answer_callback(callback)
        explanation = str(error) if isinstance(error, ValidationError) else "Выбери дату и время заново."
        await callback.message.answer(
            f"⚠️ {escape(explanation)}\n\nВыбери другую дату и время.",
            reply_markup=time_keyboard(data["nonce"]),
            parse_mode="HTML",
        )
        return
    await state.clear()
    await answer_callback(callback, "Задача создана")
    user = await service.get_user(callback.from_user.id)
    await edit_message(
        callback.message,
        "✅ <b>Задача создана!</b>\n\n" + _task_text(task, user.timezone, service.now()),
        task_keyboard(task.id, task.repeat, False, 0, _task_version(task)),
    )


async def repeat_text_hint(message: Message) -> None:
    await message.answer("Выбери частоту повторения кнопкой под сообщением выше. Отмена: /cancel")


async def cancel_flow(callback: CallbackQuery, state: FSMContext) -> None:
    parts = (callback.data or "").split(":")
    data = await state.get_data()
    if len(parts) != 3 or parts[1] != "cancel" or parts[2] != data.get("nonce"):
        await _stale(callback)
        return
    await state.clear()
    await answer_callback(callback, "Действие отменено")
    await edit_message(callback.message, "Действие отменено. Главное меню доступно внизу 👇")


async def _show_list(
    message: Message,
    user_id: int,
    state: FSMContext,
    service: TaskService,
    *,
    today: bool,
    page: int = 0,
    edit: bool = False,
) -> None:
    await state.clear()
    tasks, total = await service.list_tasks(user_id, today=today, page=page, page_size=PAGE_SIZE)
    if total and not tasks and page:
        page = (total - 1) // PAGE_SIZE
        tasks, total = await service.list_tasks(user_id, today=today, page=page, page_size=PAGE_SIZE)
    user = await service.get_user(user_id)
    if not tasks:
        text = "🎉 На сегодня задач нет!" if today else "📋 Задач пока нет. Создай первую через /new."
    else:
        heading = "📅 <b>Задачи на сегодня</b>" if today else "📋 <b>Мои задачи</b>"
        lines = [heading, f"🌍 {escape(user.timezone)} · Страница {page + 1}/{(total + PAGE_SIZE - 1) // PAGE_SIZE}"]
        for offset, task in enumerate(tasks, start=page * PAGE_SIZE + 1):
            lines.append(
                f"\n{offset}. <b>{escape(task.title)}</b>\n"
                f"📅 {format_datetime(task.remind_at, user.timezone)} · {_status(task, service.now())}"
            )
        text = "\n".join(lines)
    keyboard = tasks_keyboard(tasks, total, page, today, PAGE_SIZE)
    if edit:
        await edit_message(message, text, keyboard)
    else:
        await message.answer(text, reply_markup=keyboard, parse_mode="HTML")


async def show_tasks(message: Message, state: FSMContext, service: TaskService) -> None:
    await _show_list(message, message.from_user.id, state, service, today=False)


async def show_today(message: Message, state: FSMContext, service: TaskService) -> None:
    await _show_list(message, message.from_user.id, state, service, today=True)


async def list_callback(callback: CallbackQuery, state: FSMContext, service: TaskService) -> None:
    parts = (callback.data or "").split(":")
    try:
        if len(parts) != 3 or parts[1] not in {"0", "1"}:
            raise ValueError
        page = _number(parts[2], zero=True)
        # Bound offsets to keep deliberately forged pagination cheap.
        if page > 1_000_000:
            raise ValueError
    except ValueError:
        await _stale(callback)
        return
    await answer_callback(callback)
    await _show_list(callback.message, callback.from_user.id, state, service, today=parts[1] == "1", page=page, edit=True)


async def task_callback(callback: CallbackQuery, state: FSMContext, service: TaskService) -> None:
    parts = (callback.data or "").split(":")
    try:
        if len(parts) not in {5, 6} or parts[1] not in {"view", "rename", "time", "done", "delete", "stop"} or parts[3] not in {"0", "1"}:
            raise ValueError
        task_id = _number(parts[2])
        page = _number(parts[4], zero=True)
        if page > 1_000_000:
            raise ValueError
        task = await service.get_task(callback.from_user.id, task_id)
        if parts[1] != "view" and (len(parts) != 6 or parts[5] != _task_version(task)):
            raise ValueError
    except (ValueError, TaskNotFound):
        await _stale(callback)
        return
    action = parts[1]
    today = parts[3] == "1"
    user = await service.get_user(callback.from_user.id)
    if action == "view":
        await state.clear()
        await answer_callback(callback)
        await edit_message(callback.message, _task_text(task, user.timezone, service.now()), task_keyboard(task.id, task.repeat, today, page, _task_version(task)))
        return
    if action == "rename":
        await state.clear()
        await state.update_data(task_id=task.id, today=today, page=page)
        await state.set_state(TaskFlow.rename)
        await answer_callback(callback)
        await callback.message.answer("✏️ Введи новое название задачи — до 200 символов.\nОтмена: /cancel", reply_markup=main_menu())
        return
    if action == "time":
        await answer_callback(callback)
        await _start_schedule(callback.message, state, mode="reschedule", title=task.title, task_id=task.id, today=today, page=page)
        return
    try:
        if action == "done":
            updated = await service.complete_task(callback.from_user.id, task.id)
            await state.clear()
            await answer_callback(callback, "Выполнено")
            if task.repeat != "none":
                await edit_message(
                    callback.message,
                    "✅ Ближайшее напоминание выполнено. Серия продолжается.\n\n"
                    + _task_text(updated, user.timezone, service.now()),
                    task_keyboard(updated.id, updated.repeat, today, page, _task_version(updated)),
                )
            else:
                await edit_message(
                    callback.message, f"✅ Задача выполнена!\n📝 {escape(task.title)}",
                    inline([[("◀️ К списку", f"list:{int(today)}:{page}")]]),
                )
        else:
            await service.delete_task(callback.from_user.id, task.id)
            await state.clear()
            await answer_callback(callback, "Повторение остановлено" if task.repeat != "none" else "Задача удалена")
            text = "🛑 Повторение остановлено. Серия удалена." if task.repeat != "none" else "🗑 Задача удалена."
            await edit_message(callback.message, f"{text}\n📝 {escape(task.title)}", inline([[("◀️ К списку", f"list:{int(today)}:{page}")]]))
    except TaskNotFound:
        await _stale(callback)
    except ValidationError:
        await answer_callback(callback, "Не удалось изменить задачу. Обнови список через /tasks.", alert=True)


async def rename_task(message: Message, state: FSMContext, service: TaskService) -> None:
    data = await state.get_data()
    try:
        title = _title(message.text)
        task = await service.rename_task(message.from_user.id, data["task_id"], title)
    except TaskNotFound:
        await state.clear()
        await message.answer(STALE_TEXT, reply_markup=main_menu())
        return
    except (KeyError, ValidationError, ValueError):
        await message.answer("Название должно содержать от 1 до 200 символов. Попробуй ещё раз или /cancel.")
        return
    await state.clear()
    user = await service.get_user(message.from_user.id)
    await message.answer(
        "✅ Название изменено!\n\n" + _task_text(task, user.timezone, service.now()),
        reply_markup=task_keyboard(task.id, task.repeat, data.get("today", False), data.get("page", 0), _task_version(task)),
        parse_mode="HTML",
    )


async def reminder_callback(callback: CallbackQuery, state: FSMContext, service: TaskService) -> None:
    parts = (callback.data or "").split(":")
    try:
        if len(parts) not in {3, 4} or parts[1] not in {"done", "delete", "snooze", "delay", "back"}:
            raise ValueError
        action = parts[1]
        if (action == "delay") != (len(parts) == 4):
            raise ValueError
        if action == "delay" and parts[3] not in {"10", "30", "60", "manual"}:
            raise ValueError
        delivery_id = _number(parts[2])
        delivery = await service.get_delivery(callback.from_user.id, delivery_id)
        if delivery.status != "sent" or (
            delivery.message_id is not None and delivery.message_id != callback.message.message_id
        ):
            raise TaskNotFound()
    except (ValueError, TaskNotFound):
        await _stale(callback)
        return
    user = await service.get_user(callback.from_user.id)
    try:
        if action == "snooze":
            await answer_callback(callback)
            await edit_message(callback.message, f"⏰ На сколько отложить?\n📝 {escape(delivery.title)}", snooze_keyboard(delivery_id))
            return
        if action == "back":
            await answer_callback(callback)
            await edit_message(callback.message, f"🔔 <b>НАПОМИНАНИЕ!</b>\n\n📝 {escape(delivery.title)}\n\n⏰ Время выполнить задачу!", reminder_keyboard(delivery_id))
            return
        if action == "delay" and parts[3] == "manual":
            await answer_callback(callback)
            await _start_schedule(
                callback.message, state, mode="snooze", title=delivery.title, delivery_id=delivery_id,
                source_chat_id=callback.message.chat.id, source_message_id=callback.message.message_id,
            )
            return
        await state.clear()
        if action == "done":
            await service.complete_delivery(callback.from_user.id, delivery_id)
            text = f"✅ Выполнено!\n📝 {escape(delivery.title)}"
            if delivery.repeat != "none":
                text += "\n🔁 Следующие напоминания остаются в расписании."
        elif action == "delete":
            await service.delete_delivery(callback.from_user.id, delivery_id)
            text = f"🗑 Напоминание удалено.\n📝 {escape(delivery.title)}"
            if delivery.repeat != "none":
                text += "\n🔁 Серия продолжается. Остановить её можно в /tasks."
        else:
            moment = service.now() + timedelta(minutes=int(parts[3]))
            await service.snooze_delivery(callback.from_user.id, delivery_id, moment)
            text = f"⏰ Отложено на {format_datetime(moment, user.timezone)}.\n📝 {escape(delivery.title)}"
        await answer_callback(callback, "Готово")
        await edit_message(callback.message, text)
    except TaskNotFound:
        await _stale(callback)
    except (ValidationError, ValueError):
        await answer_callback(callback, "Не удалось перенести напоминание. Открой /tasks и выбери другое время.", alert=True)


def register_navigation(router: Router) -> None:
    router.message.register(start_creation, Command("new"))
    router.message.register(start_creation, F.text == NEW_TASK)
    router.message.register(show_tasks, Command("tasks"))
    router.message.register(show_tasks, F.text == MY_TASKS)
    router.message.register(show_today, Command("today"))
    router.message.register(show_today, F.text == TODAY)


def register_handlers(router: Router) -> None:
    router.callback_query.register(choose_quick_time, F.data.startswith("when:"))
    router.callback_query.register(choose_repeat, F.data.startswith("repeat:"))
    router.callback_query.register(cancel_flow, F.data.startswith("flow:"))
    router.callback_query.register(list_callback, F.data.startswith("list:"))
    router.callback_query.register(task_callback, F.data.startswith("task:"))
    router.callback_query.register(reminder_callback, F.data.startswith("rem:"))
    router.message.register(receive_title, TaskFlow.title)
    router.message.register(receive_when, TaskFlow.when)
    router.message.register(receive_time, TaskFlow.time)
    router.message.register(repeat_text_hint, TaskFlow.repeat)
    router.message.register(rename_task, TaskFlow.rename)
