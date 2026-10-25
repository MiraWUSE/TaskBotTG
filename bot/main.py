"""Запуск: python -m bot.main."""

import asyncio
import logging
import sys
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage, SimpleEventIsolation
from aiogram.types import BotCommand, BotCommandScopeAllPrivateChats

from bot.config import Settings
from bot.database.db import Database
from bot.handlers import create_router
from bot.lifecycle import GracefulDispatcher, stop_dispatcher
from bot.services.reminders import ReminderWorker
from bot.services.task_service import TaskService


COMMANDS = [
    BotCommand(command="start", description="Главное меню"),
    BotCommand(command="new", description="Создать задачу"),
    BotCommand(command="tasks", description="Мои задачи"),
    BotCommand(command="today", description="Задачи на сегодня"),
    BotCommand(command="calendar", description="Календарь задач"),
    BotCommand(command="settings", description="Часовой пояс"),
    BotCommand(command="help", description="Справка"),
    BotCommand(command="cancel", description="Отменить текущее действие"),
]


class SecretFormatter(logging.Formatter):
    def __init__(self, token: str):
        super().__init__("%(asctime)s %(levelname)s %(name)s: %(message)s")
        self.token = token

    def format(self, record: logging.LogRecord) -> str:
        return super().format(record).replace(self.token, "[BOT_TOKEN]")


def configure_logging(settings: Settings) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(SecretFormatter(settings.bot_token))
    logging.basicConfig(level=settings.log_level, handlers=[handler], force=True)


async def run(settings: Settings) -> None:
    configure_logging(settings)
    session = AiohttpSession(timeout=settings.telegram_timeout_seconds)
    bot = Bot(settings.bot_token, session=session, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    storage = MemoryStorage()
    isolation = SimpleEventIsolation()
    dispatcher = GracefulDispatcher(shutdown_timeout=settings.telegram_timeout_seconds + 5,
                                    storage=storage, events_isolation=isolation)
    dispatcher.include_router(create_router())
    stop = asyncio.Event()
    worker_task: asyncio.Task | None = None
    polling_task: asyncio.Task | None = None
    try:
        async with Database(settings.database_path) as database:
            service = TaskService(database, settings.bot_timezone)
            await bot.delete_webhook(drop_pending_updates=False)
            await bot.set_my_commands(COMMANDS, scope=BotCommandScopeAllPrivateChats())
            worker = ReminderWorker(bot, service, poll_seconds=settings.reminder_poll_seconds,
                                    lease_seconds=settings.reminder_lease_seconds)
            worker_task = asyncio.create_task(worker.run(stop), name="reminders")
            polling_task = asyncio.create_task(dispatcher.start_polling(
                bot, service=service, allowed_updates=dispatcher.resolve_used_update_types(),
                close_bot_session=False, tasks_concurrency_limit=100,
            ), name="telegram-polling")
            try:
                done, _ = await asyncio.wait({worker_task, polling_task}, return_when=asyncio.FIRST_COMPLETED)
                for finished in done:
                    finished.result()
                if worker_task in done and not stop.is_set():
                    raise RuntimeError("Обработчик напоминаний неожиданно завершился.")
            finally:
                stop.set()
                try:
                    await stop_dispatcher(dispatcher, polling_task)
                finally:
                    if worker_task is not None:
                        try:
                            await asyncio.wait_for(worker_task, timeout=settings.telegram_timeout_seconds + 5)
                        except (TimeoutError, asyncio.CancelledError):
                            worker_task.cancel()
                            await asyncio.gather(worker_task, return_exceptions=True)
    finally:
        await storage.close()
        await isolation.close()
        await bot.session.close()


def main() -> None:
    try:
        settings = Settings.from_env()
    except ValueError as error:
        print(f"Ошибка конфигурации: {error}", file=sys.stderr)
        raise SystemExit(2) from None
    try:
        asyncio.run(run(settings))
    except KeyboardInterrupt:
        pass
    except Exception as error:
        # Ошибка может содержать URL Telegram с токеном; formatter скрывает его.
        logging.getLogger(__name__).error("Бот остановлен: %s", error)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
