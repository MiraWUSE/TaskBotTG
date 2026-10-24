"""Drain concurrent Telegram handlers before closing FSM storage and SQLite."""

from __future__ import annotations

import asyncio
from typing import Any

from aiogram import Dispatcher


class GracefulDispatcher(Dispatcher):
    def __init__(self, *, shutdown_timeout: float = 35, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.shutdown_timeout = shutdown_timeout

    async def emit_shutdown(self, *args: Any, **kwargs: Any) -> None:
        # aiogram 3.31 tracks concurrent updates privately but does not drain them.
        # Polling has already stopped producing updates when this hook is called.
        handlers = set(self._handle_update_tasks)
        if handlers:
            _, unfinished = await asyncio.wait(handlers, timeout=self.shutdown_timeout)
            for task in unfinished:
                task.cancel()
            # Await cancellation cleanup/transaction rollbacks before FSM/DB close.
            await asyncio.gather(*handlers, return_exceptions=True)
        await super().emit_shutdown(*args, **kwargs)


async def _stop_dispatcher(dispatcher: GracefulDispatcher, polling_task: asyncio.Task[Any]) -> None:
    if polling_task.done():
        await asyncio.gather(polling_task, return_exceptions=True)
        return

    # With pinned aiogram 3.31 the lock is acquired before any polling child is
    # created. This check and cancel have no intervening await: only a task that
    # has not entered polling can be cancelled directly without leaking children.
    if not dispatcher._running_lock.locked():
        polling_task.cancel()
        await asyncio.gather(polling_task, return_exceptions=True)
        return

    stopper = asyncio.create_task(dispatcher.stop_polling(), name="stop-telegram-polling")
    try:
        # A startup callback can fail before aiogram sets _stopped_signal. Race
        # against the polling task so that its failure cannot strand the waiter.
        await asyncio.wait({stopper, polling_task}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        if not stopper.done():
            stopper.cancel()
        await asyncio.gather(stopper, return_exceptions=True)
    await asyncio.gather(polling_task, return_exceptions=True)


async def stop_dispatcher(
    dispatcher: GracefulDispatcher, polling_task: asyncio.Task[Any] | None
) -> None:
    """Stop polling and join handlers even when the caller receives Ctrl+C.

    Call in the application's finally block, before closing the database/session.
    The application owns polling_task and must use this helper rather than
    cancelling Dispatcher.start_polling directly while it is running.
    """
    if polling_task is None:
        return
    cleanup = asyncio.create_task(_stop_dispatcher(dispatcher, polling_task), name="drain-telegram-polling")
    try:
        await asyncio.shield(cleanup)
    except asyncio.CancelledError:
        # Finish teardown before allowing the enclosing DB context to exit.
        await asyncio.shield(cleanup)
        raise
