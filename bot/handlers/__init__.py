"""Build a fresh router for each bot or offline integration test."""

from aiogram import Router
from aiogram.types import CallbackQuery

from bot.handlers import planning, quick, settings, start, tasks
from bot.handlers.middleware import UserMiddleware, answer_callback


async def unknown_callback(callback: CallbackQuery) -> None:
    await answer_callback(callback, "Эта кнопка устарела. Открой /tasks.", alert=True)


def create_router() -> Router:
    router = Router(name="personal_tasks")
    router.message.outer_middleware(UserMiddleware())
    router.callback_query.outer_middleware(UserMiddleware())
    # Navigation is intentionally registered before every state-specific handler,
    # so /new, /tasks, /settings and the menu always leave a half-written form.
    start.register_navigation(router)
    tasks.register_navigation(router)
    settings.register_navigation(router)
    planning.register_navigation(router)
    tasks.register_handlers(router)
    settings.register_handlers(router)
    planning.register_handlers(router)
    quick.register_handlers(router)
    router.message.register(start.unknown_message)
    router.callback_query.register(unknown_callback)
    return router


__all__ = ["create_router"]
