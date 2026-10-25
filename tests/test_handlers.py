"""Exercise the real aiogram router without a Telegram connection or token."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.fsm.storage.memory import SimpleEventIsolation
from aiogram.methods import AnswerCallbackQuery, EditMessageReplyMarkup, EditMessageText, SendMessage
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from bot.database.db import Database
from bot.handlers import create_router
from bot.handlers.tasks import TaskFlow
from bot.handlers.quick import QuickFlow
from bot.services.task_service import TaskNotFound, TaskService

HUMAN = User(id=11, is_bot=False, first_name="Тест")
BOT_USER = User(id=123456, is_bot=True, first_name="Бот")


class FakeSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.requests = []
        self.messages = {}
        self.latest = None
        self.sequence = 100

    async def close(self):
        pass

    async def make_request(self, bot, method, timeout=None):
        self.requests.append(method)
        if isinstance(method, AnswerCallbackQuery):
            return True
        if isinstance(method, (SendMessage, EditMessageText, EditMessageReplyMarkup)):
            self.sequence += 1
            message_id = getattr(method, "message_id", None) or self.sequence
            previous = self.messages.get((int(method.chat_id), message_id))
            text = getattr(method, "text", None) or (previous.text if previous else "Напоминание")
            message = Message(
                message_id=message_id,
                date=datetime.now(UTC),
                chat=Chat(id=int(method.chat_id), type="private"),
                from_user=BOT_USER,
                text=text,
                reply_markup=method.reply_markup if not isinstance(method, SendMessage) or hasattr(method.reply_markup, "inline_keyboard") else None,
            ).as_(bot)
            self.messages[(message.chat.id, message_id)] = message
            self.latest = message
            return message
        raise AssertionError(f"Unexpected Telegram API method: {type(method).__name__}")

    async def stream_content(self, *args, **kwargs):
        if False:
            yield b""


@dataclass
class Harness:
    service: TaskService
    clock: list
    dispatcher: Dispatcher
    bot: Bot
    session: FakeSession
    sequence: int = 0

    async def text(self, text, user=HUMAN, chat_type="private"):
        self.sequence += 1
        message = Message(
            message_id=self.sequence,
            date=datetime.now(UTC),
            chat=Chat(id=user.id if chat_type == "private" else -100, type=chat_type),
            from_user=user,
            text=text,
        )
        await self.dispatcher.feed_update(self.bot, Update(update_id=self.sequence, message=message))
        return self.session.latest

    async def click(self, data, *, message=None, user=HUMAN):
        self.sequence += 1
        source = message or self.session.latest
        query = CallbackQuery(id=str(self.sequence), from_user=user, message=source, chat_instance="offline", data=data)
        await self.dispatcher.feed_update(self.bot, Update(update_id=self.sequence, callback_query=query))
        return self.session.latest

    def button(self, startswith, message=None):
        source = message or self.session.latest
        return next(
            button.callback_data
            for row in source.reply_markup.inline_keyboard
            for button in row
            if button.callback_data.startswith(startswith)
        )

    async def state(self):
        context = self.dispatcher.fsm.get_context(bot=self.bot, chat_id=HUMAN.id, user_id=HUMAN.id)
        return await context.get_state()


@pytest_asyncio.fixture
async def ui(tmp_path):
    database = Database(tmp_path / "handlers.sqlite3")
    await database.connect()
    clock = [datetime(2026, 10, 23, 9, 0, tzinfo=UTC)]
    service = TaskService(database, "Europe/Moscow", clock=lambda: clock[0])
    session = FakeSession()
    bot = Bot(token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ123456789", session=session)
    dispatcher = Dispatcher(service=service, events_isolation=SimpleEventIsolation())
    dispatcher.include_router(create_router())
    harness = Harness(service, clock, dispatcher, bot, session)
    yield harness
    await dispatcher.fsm.close()
    await bot.session.close()
    await database.close()


async def create_in_ui(ui, title="Проверить проект", repeat="none"):
    await ui.text("/new")
    await ui.text(title)
    await ui.click(ui.button("when:"))
    await ui.click(next(
        button.callback_data for row in ui.session.latest.reply_markup.inline_keyboard for button in row
        if button.callback_data.endswith(":" + repeat)
    ))
    return (await ui.service.list_tasks(HUMAN.id))[0][-1]


async def test_create_split_date_time_and_escape_title(ui):
    await ui.text("/start")
    await ui.text("/new")
    await ui.text("Купить <витамины> & чай")
    await ui.text("26.10.2026")
    await ui.text("18:30")
    await ui.click(ui.button("repeat:"))
    tasks, total = await ui.service.list_tasks(HUMAN.id)
    assert total == 1
    assert tasks[0].remind_at == datetime(2026, 10, 26, 15, 30, tzinfo=UTC)
    assert "&lt;витамины&gt; &amp; чай" in ui.session.latest.text
    assert await ui.state() is None


@pytest.mark.parametrize("invalid,diagnostic", [
    ("31.02.2026 12:00", "не существует"),
    ("31.02.2026", "не существует"),
    ("01.01.2026 12:00", "прошедшее"),
    ("01.01.2026", "прошла"),
    ("завтра вечером", "8:00"),
    ("26.10.2026 25:00", "23"),
])
async def test_invalid_or_past_date_keeps_form(ui, invalid, diagnostic):
    await ui.text("/new")
    await ui.text("Задача")
    await ui.text(invalid)
    assert await ui.state() == TaskFlow.when.state
    assert (await ui.service.list_tasks(HUMAN.id))[1] == 0
    assert diagnostic in ui.session.latest.text
    assert "будням" not in ui.session.latest.text


@pytest.mark.parametrize("text,expected", [
    ("24.10.2026 8:00", datetime(2026, 10, 24, 5, 0, tzinfo=UTC)),
    ("24.10.2026 8:5", datetime(2026, 10, 24, 5, 5, tzinfo=UTC)),
    ("24.10.2026 8.30", datetime(2026, 10, 24, 5, 30, tzinfo=UTC)),
    ("24.10.2026 8", datetime(2026, 10, 24, 5, 0, tzinfo=UTC)),
    ("24.10.2026  в  8 : 00", datetime(2026, 10, 24, 5, 0, tzinfo=UTC)),
    ("24.10.2026\n8:00", datetime(2026, 10, 24, 5, 0, tzinfo=UTC)),
    ("24.10.2026\u00a08:00", datetime(2026, 10, 24, 5, 0, tzinfo=UTC)),
    ("ЗАВТРА В 8:00", datetime(2026, 10, 24, 5, 0, tzinfo=UTC)),
    ("послезавтра 8.30", datetime(2026, 10, 25, 5, 30, tzinfo=UTC)),
    ("сегодня в 18:00", datetime(2026, 10, 23, 15, 0, tzinfo=UTC)),
])
async def test_flexible_combined_datetime_creates_task(ui, text, expected):
    await ui.text("/new")
    await ui.text("Утреннее дело")
    await ui.text(text)
    assert await ui.state() == TaskFlow.repeat.state
    await ui.click(ui.button("repeat:"))
    tasks, total = await ui.service.list_tasks(HUMAN.id)
    assert total == 1
    assert tasks[0].remind_at == expected
    assert await ui.state() is None


@pytest.mark.parametrize("day,clock,expected", [
    ("24.10.2026", "8:00", datetime(2026, 10, 24, 5, 0, tzinfo=UTC)),
    ("завтра", "8.30", datetime(2026, 10, 24, 5, 30, tzinfo=UTC)),
    ("ПОСЛЕЗАВТРА", "8", datetime(2026, 10, 25, 5, 0, tzinfo=UTC)),
    ("24.10.2026", "8 : 5", datetime(2026, 10, 24, 5, 5, tzinfo=UTC)),
])
async def test_flexible_separate_date_and_clock_creates_task(ui, day, clock, expected):
    await ui.text("/new")
    await ui.text("Задача")
    await ui.text(day)
    assert await ui.state() == TaskFlow.time.state
    await ui.text(clock)
    assert await ui.state() == TaskFlow.repeat.state
    await ui.click(ui.button("repeat:"))
    assert (await ui.service.list_tasks(HUMAN.id))[0][0].remind_at == expected


async def test_relative_day_uses_user_timezone_near_midnight(ui):
    await ui.text("/start")
    await ui.service.set_timezone(HUMAN.id, "Asia/Vladivostok")
    ui.clock[0] = datetime(2026, 10, 23, 20, 0, tzinfo=UTC)  # 24 October, 06:00 locally.
    await ui.text("/new")
    await ui.text("Утром")
    await ui.text("сегодня в 8:00")
    await ui.click(ui.button("repeat:"))
    assert (await ui.service.list_tasks(HUMAN.id))[0][0].remind_at == datetime(2026, 10, 23, 22, 0, tzinfo=UTC)


async def test_same_day_past_input_does_not_roll_forward(ui):
    ui.clock[0] = datetime(2026, 10, 24, 9, 0, tzinfo=UTC)  # 12:00 Moscow.
    await ui.text("/new")
    await ui.text("Задача")
    await ui.text("24.10.2026 8:00")
    assert await ui.state() == TaskFlow.when.state
    assert "прошедшее" in ui.session.latest.text
    assert (await ui.service.list_tasks(HUMAN.id))[1] == 0
    await ui.text("завтра в 8:00")
    await ui.click(ui.button("repeat:"))
    assert (await ui.service.list_tasks(HUMAN.id))[0][0].remind_at == datetime(2026, 10, 25, 5, 0, tzinfo=UTC)


async def test_separate_clock_error_preserves_selected_day(ui):
    await ui.text("/new")
    await ui.text("Задача")
    await ui.text("завтра")
    await ui.text("25:00")
    assert await ui.state() == TaskFlow.time.state
    assert "23" in ui.session.latest.text
    assert "будням" not in ui.session.latest.text
    await ui.text("8.5")
    assert await ui.state() == TaskFlow.time.state
    await ui.text("8:5")
    await ui.click(ui.button("repeat:"))
    assert (await ui.service.list_tasks(HUMAN.id))[0][0].remind_at == datetime(2026, 10, 24, 5, 5, tzinfo=UTC)


async def test_menu_commands_escape_any_form_and_timezone_validation(ui):
    await ui.text("/settings")
    await ui.click("settings:timezone")
    await ui.text("Not/AZone")
    assert "Не удалось найти" in ui.session.latest.text
    await ui.text("/new")
    assert await ui.state() == TaskFlow.title.state
    await ui.text("/settings")
    await ui.click("settings:timezone")
    await ui.text("Europe/Helsinki")
    assert (await ui.service.get_user(HUMAN.id)).timezone == "Europe/Helsinki"
    assert await ui.state() is None


async def test_quick_create_cancel_and_stale_repeat_are_safe(ui):
    task = await create_in_ui(ui)
    assert task.remind_at == ui.clock[0] + timedelta(minutes=15)
    assert "Задача создана" in ui.session.latest.text
    await ui.click("repeat:expired:daily")
    assert (await ui.service.list_tasks(HUMAN.id))[1] == 1
    assert isinstance(ui.session.requests[-1], AnswerCallbackQuery)
    assert ui.session.requests[-1].show_alert is True
    await ui.text("/new")
    await ui.text("Не сохранять")
    await ui.click(ui.button("flow:cancel:"))
    assert await ui.state() is None
    assert (await ui.service.list_tasks(HUMAN.id))[1] == 1


async def test_weekday_creation_error_recovers_to_time_selection(ui):
    await ui.text("/new")
    await ui.text("По будням")
    await ui.text("24.10.2026 18:30")  # Saturday.
    weekday_button = next(
        button.callback_data for row in ui.session.latest.reply_markup.inline_keyboard for button in row
        if button.callback_data.endswith(":weekdays")
    )
    await ui.click(weekday_button)
    assert await ui.state() == TaskFlow.when.state
    assert "будням" in ui.session.latest.text
    await ui.text("26.10.2026 18:30")
    weekday_button = next(
        button.callback_data for row in ui.session.latest.reply_markup.inline_keyboard for button in row
        if button.callback_data.endswith(":weekdays")
    )
    await ui.click(weekday_button)
    assert (await ui.service.list_tasks(HUMAN.id))[0][0].repeat == "weekdays"


async def test_list_pagination_rename_and_reschedule(ui):
    await ui.text("/start")
    for offset in range(8):
        await ui.service.create_task(HUMAN.id, f"Дело {offset + 1}", ui.clock[0] + timedelta(hours=offset + 1))
    await ui.text("/tasks")
    assert "Страница 1/2" in ui.session.latest.text
    await ui.click("list:0:1")
    assert "Страница 2/2" in ui.session.latest.text
    await ui.click(ui.button("task:view:"))
    await ui.click(ui.button("task:rename:"))
    await ui.text("Новое название")
    assert "Название изменено" in ui.session.latest.text
    task_id = (await ui.service.list_tasks(HUMAN.id, page_size=20))[0][6].id
    await ui.click(ui.button("task:time:"))
    await ui.text("27.10.2026 20:15")
    task = await ui.service.get_task(HUMAN.id, task_id)
    assert task.title == "Новое название"
    assert task.remind_at == datetime(2026, 10, 27, 17, 15, tzinfo=UTC)


async def test_flexible_reschedule_updates_only_selected_task(ui):
    task = await create_in_ui(ui)
    other = await ui.service.create_task(HUMAN.id, "Другое дело", ui.clock[0] + timedelta(days=2))
    await ui.click(ui.button("task:time:"))
    await ui.text("завтра в 8:00")
    assert (await ui.service.get_task(HUMAN.id, task.id)).remind_at == datetime(2026, 10, 24, 5, 0, tzinfo=UTC)
    assert await ui.service.get_task(HUMAN.id, other.id) == other
    assert await ui.state() is None


async def test_stale_recurring_card_cannot_skip_next_occurrence(ui):
    task = await create_in_ui(ui, repeat="daily")
    old_done = ui.button("task:done:")
    await ui.click(old_done)
    next_time = (await ui.service.get_task(HUMAN.id, task.id)).remind_at
    assert next_time == task.remind_at + timedelta(days=1)
    await ui.click(old_done)
    assert (await ui.service.get_task(HUMAN.id, task.id)).remind_at == next_time
    assert ui.session.requests[-1].show_alert is True
    await ui.click(ui.button("task:stop:"))
    assert (await ui.service.list_tasks(HUMAN.id))[1] == 0


async def test_same_time_occurrences_still_invalidate_old_card(ui):
    task, delivery, original = await make_sent(ui, repeat="daily")
    next_time = ui.clock[0].replace(hour=9, minute=1) + timedelta(days=1)
    await ui.service.snooze_delivery(HUMAN.id, delivery.id, next_time)
    await ui.text("/tasks")
    await ui.click(ui.button("task:view:"))
    old_done = ui.button("task:done:")
    before = await ui.service.get_task(HUMAN.id, task.id)
    await ui.click(old_done)
    after = await ui.service.get_task(HUMAN.id, task.id)
    assert after.remind_at == before.remind_at == next_time
    assert ui.button("task:done:") != old_done
    await ui.click(old_done)
    assert ui.session.requests[-1].show_alert is True
    assert (await ui.service.get_task(HUMAN.id, task.id)).remind_at == next_time


@pytest.mark.parametrize("data", ["task:done:x:0:0", "task:delete:-1:0:0", "list:0:99999999999999", "rem:delay:1:999999", "rem:done:99999999999999999999", "random:data"])
async def test_forged_callback_payloads_are_rejected(ui, data):
    await ui.text("/start")
    await ui.click(data)
    assert isinstance(ui.session.requests[-1], AnswerCallbackQuery)
    assert ui.session.requests[-1].show_alert is True


async def test_another_user_cannot_open_task(ui):
    task = await create_in_ui(ui)
    stranger = User(id=22, is_bot=False, first_name="Другой")
    await ui.text("/start", user=stranger)
    await ui.click(f"task:view:{task.id}:0:0", user=stranger)
    assert ui.session.requests[-1].show_alert is True
    assert (await ui.service.get_task(HUMAN.id, task.id)).status == "active"
    assert "Проверить проект" not in ui.session.latest.text


async def test_group_chat_does_not_register_user(ui):
    await ui.text("/start", chat_type="group")
    with pytest.raises(ValueError):
        await ui.service.get_user(HUMAN.id)


async def make_sent(ui, *, repeat="none", message_id=700):
    await ui.text("/start")
    task = await ui.service.create_task(HUMAN.id, "Напоминание", ui.clock[0] + timedelta(minutes=1), repeat)
    ui.clock[0] += timedelta(minutes=2)
    await ui.service.materialize_due()
    delivery = await ui.service.claim_due()
    assert await ui.service.acknowledge_delivery(delivery, message_id)
    message = Message(
        message_id=message_id, date=datetime.now(UTC), chat=Chat(id=HUMAN.id, type="private"),
        from_user=BOT_USER, text="Напоминание",
    ).as_(ui.bot)
    return task, delivery, message


async def test_notification_snooze_and_old_controls_after_resend(ui):
    task, delivery, original = await make_sent(ui)
    await ui.click(f"rem:snooze:{delivery.id}", message=original)
    await ui.click(f"rem:delay:{delivery.id}:10")
    assert "Отложено" in ui.session.latest.text
    assert (await ui.service.get_task(HUMAN.id, task.id)).remind_at == ui.clock[0] + timedelta(minutes=10)
    await ui.click(f"rem:done:{delivery.id}", message=original)
    assert ui.session.requests[-1].show_alert is True
    ui.clock[0] += timedelta(minutes=11)
    resent = await ui.service.claim_due()
    await ui.service.acknowledge_delivery(resent, 701)
    await ui.click(f"rem:done:{delivery.id}", message=original)
    assert ui.session.requests[-1].show_alert is True
    assert (await ui.service.get_task(HUMAN.id, task.id)).status == "active"
    new_message = original.model_copy(update={"message_id": 701})
    await ui.click(f"rem:done:{delivery.id}", message=new_message)
    with pytest.raises(TaskNotFound):
        await ui.service.get_task(HUMAN.id, task.id)


async def test_manual_snooze_updates_original_controls_and_keeps_series(ui):
    task, delivery, original = await make_sent(ui, repeat="daily")
    await ui.click(f"rem:delay:{delivery.id}:manual", message=original)
    await ui.text("23.10.2026 18:30")
    assert "отложено" in ui.session.latest.text
    assert any(isinstance(request, EditMessageReplyMarkup) and request.message_id == 700 and request.reply_markup is None for request in ui.session.requests)
    assert (await ui.service.get_task(HUMAN.id, task.id)).repeat == "daily"


async def test_flexible_manual_snooze_preserves_other_tasks(ui):
    task, delivery, original = await make_sent(ui)
    other = await ui.service.create_task(HUMAN.id, "Без изменений", ui.clock[0] + timedelta(days=2))
    await ui.click(f"rem:delay:{delivery.id}:manual", message=original)
    await ui.text("сегодня в 18")
    assert (await ui.service.get_task(HUMAN.id, task.id)).remind_at == datetime(2026, 10, 23, 15, 0, tzinfo=UTC)
    assert await ui.service.get_task(HUMAN.id, other.id) == other
    assert await ui.state() is None


@pytest.mark.parametrize("action", ["done", "delete"])
async def test_notification_actions_preserve_recurring_series(ui, action):
    task, delivery, original = await make_sent(ui, repeat="daily")
    await ui.click(f"rem:{action}:{delivery.id}", message=original)
    assert (await ui.service.get_task(HUMAN.id, task.id)).repeat == "daily"
    with pytest.raises(TaskNotFound):
        await ui.service.get_delivery(HUMAN.id, delivery.id)


async def test_quick_confirmation_save_once_and_owner(ui):
    await ui.text('Завтра в 15:00 сходить в магазин')
    assert await ui.state() == QuickFlow.confirm.state
    assert (await ui.service.list_tasks(HUMAN.id))[1] == 0
    callback = ui.button('quick:')
    source = ui.session.latest
    stranger = User(id=99, is_bot=False, first_name='Другой')
    await ui.click(callback, user=stranger, message=source)
    assert (await ui.service.list_tasks(HUMAN.id))[1] == 0
    await ui.click(callback, message=source)
    tasks, total = await ui.service.list_tasks(HUMAN.id)
    assert total == 1 and tasks[0].title == 'Сходить в магазин'
    await ui.click(callback, message=source)
    assert (await ui.service.list_tasks(HUMAN.id))[1] == 1


async def test_quick_edit_cancel_and_clarification(ui):
    await ui.text('Завтра в 15:00 магазин')
    buttons = [b.callback_data for row in ui.session.latest.reply_markup.inline_keyboard for b in row]
    await ui.click(next(x for x in buttons if x.endswith(':edit')))
    await ui.text('Через 30 минут почта')
    assert 'Почта' in ui.session.latest.text
    buttons = [b.callback_data for row in ui.session.latest.reply_markup.inline_keyboard for b in row]
    await ui.click(next(x for x in buttons if x.endswith(':cancel')))
    assert (await ui.service.list_tasks(HUMAN.id))[1] == 0
    await ui.text('25.11 в 8 дело')
    assert await ui.state() == TaskFlow.when.state
    await ui.text('25.11.2026 8:00')
    assert await ui.state() == QuickFlow.confirm.state
    await ui.click(ui.button('quick:'))
    assert (await ui.service.list_tasks(HUMAN.id))[1] == 1


async def test_quick_does_not_steal_fsm_title_and_expired_confirmation(ui):
    await ui.text('/new')
    await ui.text('Завтра в 15:00 магазин')
    assert await ui.state() == TaskFlow.when.state
    await ui.text('/cancel')
    await ui.text('Через 1 минуту почта')
    source = ui.session.latest
    button = ui.button('quick:')
    ui.clock[0] += timedelta(minutes=2)
    await ui.click(button, message=source)
    assert (await ui.service.list_tasks(HUMAN.id))[1] == 0
    assert await ui.state() == QuickFlow.edit.state
