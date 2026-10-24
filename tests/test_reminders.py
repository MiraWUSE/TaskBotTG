import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError, TelegramRetryAfter
from aiogram.methods import SendMessage

from bot.services.reminders import ReminderWorker


class FakeBot:
    def __init__(self, failures=None):
        self.failures = list(failures or [])
        self.calls = []
        self.successes = []

    async def send_message(self, **kwargs):
        self.calls.append(kwargs)
        if self.failures:
            error = self.failures.pop(0)
            if error:
                raise error
        self.successes.append(kwargs)
        return SimpleNamespace(message_id=len(self.calls))


async def due_task(service, clock, user_id=1, repeat="none"):
    task = await service.create_task(user_id, '<проверка> & "кавычки"', clock() + timedelta(seconds=1), repeat)
    clock.advance(seconds=1)
    return task


async def test_worker_sends_once_and_escapes_html(service, clock):
    await due_task(service, clock)
    bot = FakeBot()
    worker = ReminderWorker(bot, service)
    assert await worker.tick() == 1
    assert await worker.tick() == 0
    assert len(bot.calls) == 1
    assert "&lt;проверка&gt; &amp; &quot;кавычки&quot;" in bot.calls[0]["text"]
    assert bot.calls[0]["chat_id"] == 1
    assert len(bot.calls[0]["reply_markup"].inline_keyboard) == 2


@pytest.mark.parametrize("error", [
    TelegramNetworkError(SendMessage(chat_id=1, text="test"), "network"),
    TelegramBadRequest(SendMessage(chat_id=1, text="test"), "temporary invalid request"),
    OSError("offline"), TimeoutError("timeout"),
])
async def test_retry_after_failure_with_delay(service, clock, error):
    await due_task(service, clock)
    bot = FakeBot([error])
    worker = ReminderWorker(bot, service)
    assert await worker.tick() == 0
    assert await worker.tick() == 0
    clock.advance(seconds=29)
    assert await worker.tick() == 0
    clock.advance(seconds=1)
    assert await worker.tick() == 1
    assert len(bot.calls) == 2
    assert await worker.tick() == 0


async def test_rate_limit_respects_telegram_retry_after(service, clock):
    await due_task(service, clock)
    bot = FakeBot([TelegramRetryAfter(SendMessage(chat_id=1, text="x"), "rate limit", retry_after=90)])
    worker = ReminderWorker(bot, service)
    await worker.tick()
    clock.advance(seconds=90)
    assert await worker.tick() == 0
    clock.advance(seconds=1)
    assert await worker.tick() == 1


async def test_failed_user_does_not_stop_other_users(service, clock):
    await service.create_task(1, "Один", clock() + timedelta(seconds=1))
    await service.create_task(2, "Два", clock() + timedelta(seconds=1))
    clock.advance(seconds=1)
    bot = FakeBot([OSError("network"), None])
    assert await ReminderWorker(bot, service).tick() == 1
    assert bot.successes[0]["chat_id"] == 2


async def test_forbidden_pauses_until_user_returns(service, clock):
    await due_task(service, clock)
    bot = FakeBot([TelegramForbiddenError(SendMessage(chat_id=1, text="x"), "blocked")])
    worker = ReminderWorker(bot, service)
    assert await worker.tick() == 0
    assert (await service.get_user(1)).blocked
    clock.advance(hours=2)
    assert await worker.tick() == 0
    await service.ensure_user(1, 1)
    assert await worker.tick() == 1


async def test_long_downtime_delivers_one_missed_occurrence(service, clock):
    task = await service.create_task(1, "Каждый день", clock() + timedelta(minutes=1), "daily")
    clock.advance(days=40)
    bot = FakeBot()
    worker = ReminderWorker(bot, service)
    assert await worker.tick() == 1
    assert await worker.tick() == 0
    await service.complete_delivery(1, 1)
    next_task = await service.get_task(1, task.id)
    assert clock() < next_task.remind_at <= clock() + timedelta(days=1)


async def test_completed_overdue_singletons_do_not_starve_later_tasks(service, clock):
    await due_task(service, clock)
    bot = FakeBot()
    worker = ReminderWorker(bot, service, batch_size=1)
    assert await worker.tick() == 1
    await due_task(service, clock, user_id=2)
    assert await worker.tick() == 1
    assert [message["chat_id"] for message in bot.successes] == [1, 2]


async def test_renamed_claim_uses_current_title(service, clock):
    task = await due_task(service, clock)
    await service.materialize_due()
    claim = await service.claim_due()
    await service.rename_task(1, task.id, "Новое название")
    assert (await service.refresh_claim(claim)).title == "Новое название"


async def test_edit_waits_for_in_flight_send(service, clock):
    task = await due_task(service, clock)
    entered, release = asyncio.Event(), asyncio.Event()

    class SlowBot(FakeBot):
        async def send_message(self, **kwargs):
            entered.set()
            await release.wait()
            return await super().send_message(**kwargs)

    worker = ReminderWorker(SlowBot(), service)
    sending = asyncio.create_task(worker.tick())
    await asyncio.wait_for(entered.wait(), 2)
    changing = asyncio.create_task(service.reschedule_task(1, task.id, clock() + timedelta(hours=1)))
    await asyncio.sleep(0)
    assert not changing.done()
    release.set()
    assert await sending == 1
    await changing
    assert await worker.tick() == 0


async def test_worker_shutdown_wakes_without_waiting_poll_interval(service):
    stop = asyncio.Event()
    worker = ReminderWorker(FakeBot(), service, poll_seconds=300)
    running = asyncio.create_task(worker.run(stop))
    await asyncio.sleep(0)
    stop.set()
    await asyncio.wait_for(running, 1)


async def test_cancellation_leaves_claim_for_lease_recovery(service, clock):
    await due_task(service, clock)
    entered = asyncio.Event()

    class CancelBot:
        async def send_message(self, **kwargs):
            entered.set()
            await asyncio.Event().wait()

    running = asyncio.create_task(ReminderWorker(CancelBot(), service).tick())
    await asyncio.wait_for(entered.wait(), 2)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert await service.claim_due() is None
    clock.advance(seconds=121)
    assert await service.claim_due() is not None


def test_exponential_backoff_is_bounded():
    assert [ReminderWorker.retry_delay(i) for i in [1, 2, 3, 8, 999]] == [30, 60, 120, 3600, 3600]
