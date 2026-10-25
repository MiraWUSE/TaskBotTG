from datetime import UTC, datetime, timedelta
import asyncio
from types import SimpleNamespace

import pytest

from bot.database.db import Database
from bot.services.reminders import ReminderWorker
from bot.services.task_service import TaskNotFound, TaskService, ValidationError


class Sender:
    def __init__(self):
        self.calls = []

    async def send_message(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(message_id=len(self.calls))


async def setup_task(service, clock, *, repeat='none', hours=2, offsets=(5, 15, 60)):
    task = await service.create_task(1, '<Важное>', clock() + timedelta(hours=hours), repeat)
    await service.set_advances(1, task.id, offsets)
    return task


async def rows(service, kind='advance'):
    async with service.database.read() as connection:
        return await (await connection.execute('SELECT * FROM deliveries WHERE kind=? ORDER BY due_at,id', (kind,))).fetchall()


async def test_multiple_previews_then_main_once_and_no_side_effects(service, clock):
    task = await setup_task(service, clock)
    bot = Sender()
    worker = ReminderWorker(bot, service)
    assert await worker.tick() == 0
    clock.advance(hours=1)
    assert await worker.tick() == 1
    assert await worker.tick() == 0
    assert 'Скоро задача' in bot.calls[-1]['text'] and '&lt;Важное&gt;' in bot.calls[-1]['text']
    assert 'Начало через 60 мин.' in bot.calls[-1]['text']
    assert (await service.get_task(1, task.id)).remind_at == task.remind_at
    preview = (await rows(service))[0]
    for operation in (service.get_delivery, service.complete_delivery, service.delete_delivery):
        with pytest.raises(TaskNotFound):
            await operation(1, preview['id'])
    assert all(b.callback_data.startswith('task:view:') for row in bot.calls[-1]['reply_markup'].inline_keyboard for b in row)
    clock.advance(minutes=45)
    assert await worker.tick() == 1
    clock.advance(minutes=10)
    assert await worker.tick() == 1
    clock.advance(minutes=5)
    assert await worker.tick() == 1
    assert 'НАПОМИНАНИЕ' in bot.calls[-1]['text']
    assert (await service.get_task(1, task.id)).status == 'active'
    assert len(bot.calls) == 4 and await worker.tick() == 0


async def test_elapsed_offsets_not_backfilled_at_configuration(service, clock):
    task = await setup_task(service, clock, hours=0.5, offsets=(5,15,30,60,1440))
    await service.materialize_due()
    assert [r['offset_minutes'] for r in await rows(service)] == [15,5]
    assert (await service.get_task(1, task.id)).advance_offsets == (5,15,30,60,1440)
    clock.advance(minutes=30)
    bot = Sender()
    assert await ReminderWorker(bot, service).tick() == 1
    assert all('Скоро задача' not in c['text'] for c in bot.calls)


@pytest.mark.parametrize('operation', ['reschedule', 'delete', 'complete', 'disable'])
async def test_edit_invalidates_claim_and_obsolete_previews(service, clock, operation):
    task = await setup_task(service, clock)
    clock.advance(hours=1)
    await service.materialize_due()
    claim = await service.claim_due()
    assert claim.kind == 'advance'
    if operation == 'reschedule':
        await service.reschedule_task(1, task.id, clock() + timedelta(hours=5))
    elif operation == 'delete':
        await service.delete_task(1, task.id)
    elif operation == 'complete':
        await service.complete_task(1, task.id)
    else:
        await service.set_advances(1, task.id, [])
    assert await service.refresh_claim(claim) is None
    assert not await service.acknowledge_delivery(claim, 99)
    await service.materialize_due()
    assert await service.claim_due() is None


async def test_reenable_does_not_resend_already_sent_preview(service, clock):
    task = await setup_task(service, clock, offsets=(60,))
    clock.advance(hours=1)
    worker = ReminderWorker(Sender(), service)
    assert await worker.tick() == 1
    await service.set_advances(1, task.id, [])
    await service.set_advances(1, task.id, [60,15])
    assert await worker.tick() == 0
    clock.advance(minutes=45)
    assert await worker.tick() == 1


@pytest.mark.parametrize('repeat,days', [('daily',1),('weekdays',3),('weekly',7)])
async def test_recurring_preview_preserves_series_and_next_occurrence(service, clock, repeat, days):
    task = await setup_task(service, clock, repeat=repeat, offsets=(60,))
    bot = Sender()
    worker = ReminderWorker(bot, service)
    clock.advance(hours=1)
    assert await worker.tick() == 1
    clock.advance(hours=1)
    assert await worker.tick() == 1
    main = (await rows(service, 'main'))[0]
    await service.complete_delivery(1, main['id'])
    assert (await service.get_task(1, task.id)).remind_at == task.remind_at + timedelta(days=days)
    clock.advance(days=days, hours=-1)
    assert await worker.tick() == 1
    assert 'Скоро задача' in bot.calls[-1]['text']


async def test_advance_does_not_become_card_completion_target(service, clock):
    task = await setup_task(service, clock, repeat='daily', offsets=(60,1440))
    await service.materialize_due()
    assert (await service.get_task(1, task.id)).remind_at == task.remind_at
    assert (await service.list_tasks(1, today=True))[0][0].remind_at == task.remind_at
    updated = await service.complete_task(1, task.id)
    assert updated.remind_at == task.remind_at + timedelta(days=1)
    clock.value = task.remind_at - timedelta(minutes=30)
    assert await ReminderWorker(Sender(), service).tick() == 0


async def test_restart_recovers_without_duplicate_and_expired_claim_dropped(tmp_path, clock):
    path = tmp_path / 'restart.sqlite3'
    sender = Sender()
    async with Database(path) as database:
        service = TaskService(database, clock=clock)
        await service.ensure_user(1,1)
        await setup_task(service, clock, offsets=(60,))
        clock.advance(hours=1)
        assert await ReminderWorker(sender, service).tick() == 1
    async with Database(path) as database:
        service = TaskService(database, clock=clock)
        assert await ReminderWorker(sender, service).tick() == 0
        clock.advance(hours=1)
        assert await ReminderWorker(sender, service).tick() == 1
    assert len(sender.calls) == 2


async def test_failed_preview_retry_expiration_and_main_still_delivered(service, clock):
    await setup_task(service, clock, offsets=(5,))
    clock.advance(hours=1, minutes=55)
    await service.materialize_due()
    claim = await service.claim_due()
    await service.retry_delivery(claim, delay=3600, reason='offline')
    clock.advance(minutes=5)
    sender = Sender()
    assert await ReminderWorker(sender, service).tick() == 1
    assert 'НАПОМИНАНИЕ' in sender.calls[0]['text']
    assert (await rows(service))[0]['status'] == 'cancelled'


async def test_lease_recovery_and_no_send_after_deadline(service, clock):
    await setup_task(service, clock, offsets=(5,))
    clock.advance(hours=1, minutes=55)
    await service.materialize_due()
    first = await service.claim_due(lease_seconds=30)
    clock.advance(seconds=31)
    second = await service.claim_due()
    assert first.id == second.id and second.claim_token != first.claim_token
    assert not await service.acknowledge_delivery(first, 1)
    clock.advance(minutes=5)
    assert await service.refresh_claim(second) is None


async def test_timezone_change_and_dst_lookahead(service, clock):
    await service.set_timezone(1, 'Europe/Helsinki')
    clock.value = datetime(2027,3,27,4,tzinfo=UTC)
    task = await service.create_task(1, 'Серия', datetime(2027,3,27,10,tzinfo=UTC), 'daily')
    await service.set_advances(1, task.id, [1440])
    await service.materialize_due()
    # Next local noon lies 23 hours away: its preview is due before today's main.
    future_preview = (await rows(service))[0]
    assert future_preview['due_at'] == int(datetime(2027,3,27,9,tzinfo=UTC).timestamp())
    clock.value = datetime(2027,3,27,9,tzinfo=UTC)
    await service.set_timezone(1, 'Asia/Tokyo')
    sender = Sender()
    assert await ReminderWorker(sender, service).tick() == 1
    assert '28.03.2027 18:00' in sender.calls[0]['text']


@pytest.mark.parametrize('offsets', [[1], [1441], [-5], ['5'], [True], '5'])
async def test_invalid_offsets_and_owner(service, clock, offsets):
    task = await setup_task(service, clock)
    with pytest.raises(ValidationError):
        await service.set_advances(1, task.id, offsets)
    with pytest.raises(TaskNotFound):
        await service.set_advances(2, task.id, [5])


async def test_single_snooze_rebuilds_only_future_advance_intervals(service, clock):
    task = await setup_task(service, clock, offsets=(5,15))
    clock.value = task.remind_at
    await service.materialize_due()
    main = await service.claim_due()
    await service.acknowledge_delivery(main, 100)
    await service.snooze_delivery(1, main.id, clock() + timedelta(minutes=10))
    assert await ReminderWorker(Sender(), service).tick() == 0
    clock.advance(minutes=5)
    sender = Sender()
    assert await ReminderWorker(sender, service).tick() == 1
    assert 'Скоро задача' in sender.calls[0]['text']


async def test_two_connections_materialize_and_claim_preview_once(tmp_path, clock):
    path = tmp_path / 'concurrent.sqlite3'
    async with Database(path) as first, Database(path) as second:
        s1, s2 = TaskService(first, clock=clock), TaskService(second, clock=clock)
        await s1.ensure_user(1,1)
        await setup_task(s1, clock, offsets=(60,))
        clock.advance(hours=1)
        await asyncio.gather(s1.materialize_due(), s2.materialize_due())
        assert len(await rows(s1)) == 1
        claims = await asyncio.gather(s1.claim_due(), s2.claim_due())
        assert sum(claim is not None for claim in claims) == 1
