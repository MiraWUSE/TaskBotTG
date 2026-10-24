"""Private-chat boundary and registration of users."""

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import CallbackQuery, Message, TelegramObject

logger = logging.getLogger(__name__)


async def answer_callback(callback: CallbackQuery, text: str | None = None, *, alert: bool = False) -> None:
    """Old callback queries may expire while a user is offline."""
    try:
        await callback.answer(text=text, show_alert=alert)
    except TelegramAPIError:
        logger.debug("Could not answer an expired callback")


async def edit_message(message: Message, text: str, reply_markup=None) -> None:
    """Refresh a card, and recover if Telegram no longer permits editing it."""
    try:
        await message.edit_text(text, reply_markup=reply_markup, parse_mode="HTML")
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            await message.answer(text, reply_markup=reply_markup, parse_mode="HTML")


class UserMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        message = event.message if isinstance(event, CallbackQuery) else event
        if not isinstance(message, Message):
            if isinstance(event, CallbackQuery):
                await answer_callback(event, "Эта кнопка устарела. Открой /tasks.", alert=True)
            return None
        if message.chat.type != "private":
            if isinstance(event, CallbackQuery):
                await answer_callback(event, "Открой личный чат с ботом.", alert=True)
            elif message.text and message.text.startswith("/"):
                await message.answer("Для личных задач напиши мне в личном чате.")
            return None
        sender = event.from_user
        if sender is None or sender.is_bot:
            return None
        # Telegram private chat ids equal the user id. Reject forwarded callback
        # contexts rather than registering the wrong destination for reminders.
        if message.chat.id != sender.id:
            if isinstance(event, CallbackQuery):
                await answer_callback(event, "Эта кнопка недоступна.", alert=True)
            return None
        data["user"] = await data["service"].ensure_user(sender.id, message.chat.id)
        return await handler(event, data)
