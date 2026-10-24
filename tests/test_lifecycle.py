import asyncio
from types import SimpleNamespace

import pytest
from aiogram.fsm.storage.memory import MemoryStorage, SimpleEventIsolation

from bot.lifecycle import GracefulDispatcher, stop_dispatcher


class RecordingStorage(MemoryStorage):
    def __init__(self, events: list[str]):
        super().__init__()
        self.events = events

    async def close(self):
        self.events.append("storage closed")
        await super().close()


class RecordingIsolation(SimpleEventIsolation):
    def __init__(self, events: list[str]):
        super().__init__()
        self.events = events

    async def close(self):
        self.events.append("isolation closed")
        await super().close()


class OfflineDispatcher(GracefulDispatcher):
    """Use aiogram's real polling lifecycle without making any Telegram call."""

    def __init__(self, events: list[str], **kwargs):
        super().__init__(storage=RecordingStorage(events), events_isolation=RecordingIsolation(events), **kwargs)
        self.polling_started = asyncio.Event()
        self.polling_stopped = asyncio.Event()
        self.stop_called = asyncio.Event()

    async def _polling(self, *args, **kwargs):
        self.polling_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.polling_stopped.set()

    async def stop_polling(self):
        self.stop_called.set()
        await super().stop_polling()


def track_handler(dispatcher, coroutine):
    handler = asyncio.create_task(coroutine)
    dispatcher._handle_update_tasks.add(handler)
    handler.add_done_callback(dispatcher._handle_update_tasks.discard)
    return handler


def start_polling(dispatcher):
    bot = SimpleNamespace(id=123)
    return asyncio.create_task(dispatcher.start_polling(bot, close_bot_session=False, handle_signals=False))


async def test_shutdown_drains_handlers_before_closing_storage_and_isolation():
    events = []
    dispatcher = OfflineDispatcher(events, shutdown_timeout=1)
    release = asyncio.Event()
    entered = asyncio.Event()

    async def handler():
        entered.set()
        await release.wait()
        events.append("handler finished")

    task = track_handler(dispatcher, handler())
    await entered.wait()
    shutdown = asyncio.create_task(dispatcher.emit_shutdown())
    await asyncio.sleep(0)
    assert events == []
    release.set()
    await shutdown

    assert task.done() and not task.cancelled()
    assert events == ["handler finished", "storage closed", "isolation closed"]


async def test_shutdown_timeout_joins_handler_cancellation_cleanup():
    events = []
    dispatcher = OfflineDispatcher(events, shutdown_timeout=0.01)
    entered = asyncio.Event()

    async def handler():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            events.append("handler rolled back")

    task = track_handler(dispatcher, handler())
    await entered.wait()
    await dispatcher.emit_shutdown()

    assert task.cancelled()
    assert events == ["handler rolled back", "storage closed", "isolation closed"]


async def test_stop_joins_real_polling_children_and_update_handlers():
    events = []
    dispatcher = OfflineDispatcher(events, shutdown_timeout=1)
    release = asyncio.Event()

    async def handler():
        await release.wait()
        events.append("handler finished")

    handler_task = track_handler(dispatcher, handler())
    polling = start_polling(dispatcher)
    await dispatcher.polling_started.wait()
    cleanup = asyncio.create_task(stop_dispatcher(dispatcher, polling))
    await dispatcher.polling_stopped.wait()
    assert "storage closed" not in events
    release.set()
    await cleanup

    assert polling.done() and not polling.cancelled()
    assert handler_task.done()
    assert dispatcher._stop_signal.is_set()
    assert dispatcher._stopped_signal.is_set()
    assert events == ["handler finished", "storage closed", "isolation closed"]


async def test_stop_before_polling_starts_cancels_without_starting_children():
    dispatcher = OfflineDispatcher([])
    gate = asyncio.Event()

    async def not_yet_started():
        await gate.wait()
        await dispatcher.start_polling(SimpleNamespace(id=123), close_bot_session=False, handle_signals=False)

    polling = asyncio.create_task(not_yet_started())
    await stop_dispatcher(dispatcher, polling)

    assert polling.cancelled()
    assert not dispatcher.polling_started.is_set()
    assert not dispatcher._running_lock.locked()


async def test_startup_failure_does_not_strand_stop_waiter():
    dispatcher = OfflineDispatcher([])
    startup_entered = asyncio.Event()
    release = asyncio.Event()

    async def failing_startup():
        startup_entered.set()
        await release.wait()
        raise RuntimeError("startup failed")

    dispatcher.startup.register(failing_startup)
    polling = start_polling(dispatcher)
    await startup_entered.wait()
    cleanup = asyncio.create_task(stop_dispatcher(dispatcher, polling))
    await dispatcher.stop_called.wait()
    release.set()
    await asyncio.wait_for(cleanup, timeout=1)

    assert isinstance(polling.exception(), RuntimeError)
    assert not dispatcher.polling_started.is_set()
    assert not any(task.get_name() == "stop-telegram-polling" for task in asyncio.all_tasks())


async def test_cancelling_cleanup_still_drains_before_propagating_cancel():
    events = []
    dispatcher = OfflineDispatcher(events, shutdown_timeout=1)
    release = asyncio.Event()

    async def handler():
        await release.wait()
        events.append("handler finished")

    handler_task = track_handler(dispatcher, handler())
    polling = start_polling(dispatcher)
    await dispatcher.polling_started.wait()
    cleanup = asyncio.create_task(stop_dispatcher(dispatcher, polling))
    await dispatcher.polling_stopped.wait()
    cleanup.cancel()
    await asyncio.sleep(0)
    assert not cleanup.done()
    assert "storage closed" not in events
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await cleanup

    assert polling.done() and handler_task.done()
    assert events == ["handler finished", "storage closed", "isolation closed"]


async def test_stopping_already_failed_polling_consumes_exception():
    dispatcher = OfflineDispatcher([])

    async def fail():
        raise RuntimeError("polling failed")

    polling = asyncio.create_task(fail())
    await asyncio.sleep(0)
    await stop_dispatcher(dispatcher, polling)
    assert isinstance(polling.exception(), RuntimeError)


async def test_stopping_absent_polling_is_safe():
    await stop_dispatcher(OfflineDispatcher([]), None)
