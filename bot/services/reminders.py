"""Независимый планировщик с журналом отправок, lease и повторными попытками."""

import asyncio
import logging
from html import escape

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter

from bot.keyboards.keyboards import reminder_keyboard
from bot.services.task_service import TaskService
from bot.task_options import priority_icon

logger = logging.getLogger(__name__)


class ReminderWorker:
    def __init__(self, bot: Bot, service: TaskService, *, poll_seconds: float = 5,
                 lease_seconds: int = 120, batch_size: int = 50):
        self.bot = bot
        self.service = service
        self.poll_seconds = poll_seconds
        self.lease_seconds = lease_seconds
        self.batch_size = batch_size

    @staticmethod
    def retry_delay(attempts: int) -> int:
        return min(3600, 30 * 2 ** min(max(attempts - 1, 0), 7))

    async def tick(self) -> int:
        """Один проход. Используется также в тестах без сети и ожиданий."""
        await self.service.materialize_due(limit=self.batch_size * 2)
        sent = 0
        for _ in range(self.batch_size):
            claimed = await self.service.claim_due(self.lease_seconds)
            if claimed is None:
                break
            # Изменение времени/удаление ждёт окончания уже начатого запроса.
            # Изменения до захвата lock проверяются заново перед отправкой.
            async with self.service.task_lock(claimed.task_id):
                delivery = await self.service.refresh_claim(claimed)
                if delivery is None:
                    continue
                try:
                    message = await self.bot.send_message(
                        chat_id=delivery.chat_id,
                        text=f"🔔 <b>НАПОМИНАНИЕ!</b>\n\n{priority_icon(delivery.priority)} {escape(delivery.title)}\n\n⏰ Время выполнить задачу!",
                        reply_markup=reminder_keyboard(delivery.id), parse_mode="HTML",
                    )
                except asyncio.CancelledError:
                    # При неизвестном исходе API не делаем немедленный повтор:
                    # запись останется sending до истечения lease.
                    raise
                except Exception as error:
                    delay = self.retry_delay(delivery.attempts)
                    if isinstance(error, TelegramRetryAfter):
                        delay = max(delay, int(error.retry_after) + 1)
                    blocked = isinstance(error, TelegramForbiddenError)
                    await self.service.retry_delivery(delivery, delay=delay,
                                                      reason=type(error).__name__, blocked=blocked)
                    logger.warning("Доставка %s пользователю %s отложена: %s",
                                   delivery.id, delivery.user_id, type(error).__name__)
                    continue
                acknowledged = await self.service.acknowledge_delivery(delivery, message.message_id)
                if acknowledged:
                    sent += 1
                    logger.info("Напоминание %s доставлено", delivery.id)
                else:
                    logger.error("Не удалось подтвердить захват доставки %s", delivery.id)
        return sent

    async def run(self, stop: asyncio.Event) -> None:
        logger.info("Обработчик напоминаний запущен")
        while not stop.is_set():
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                # Временная ошибка SQLite не убивает фоновую задачу. Секреты
                # и содержимое пользовательских задач в журнал не попадают.
                logger.error("Ошибка обработчика напоминаний: %s", type(error).__name__)
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.poll_seconds)
            except TimeoutError:
                pass
        logger.info("Обработчик напоминаний остановлен")
