from dataclasses import replace
import asyncio
from datetime import UTC,datetime,timedelta
from types import SimpleNamespace

import pytest

from bot.database.db import Database
from bot.models import User
from bot.services.quiet_hours import QuietHoursService
from bot.services.reminders import ReminderWorker
from bot.services.summaries import SummaryService
from bot.services.task_service import TaskService,ValidationError
from bot.utils.quiet_hours import quiet_until


class Sender:
    def __init__(self):
        self.calls=[]
    async def send_message(self,**kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(message_id=len(self.calls))


@pytest.mark.parametrize('now,expected',[
    ('2026-10-25T19:59:00+00:00',None),
    ('2026-10-25T20:00:00+00:00','2026-10-26T05:00:00+00:00'),
    ('2026-10-26T04:59:00+00:00','2026-10-26T05:00:00+00:00'),
    ('2026-10-26T05:00:00+00:00',None),
])
def test_local_overnight_boundaries(now,expected):
    user=User(1,1,'Europe/Moscow',quiet_enabled=True)
    result=quiet_until(user,datetime.fromisoformat(now))
    assert result==(datetime.fromisoformat(expected) if expected else None)
    assert quiet_until(replace(user,quiet_enabled=False),datetime.fromisoformat(now)) is None


def test_daytime_high_exception_and_dst_gap_fold():
    user=User(1,1,'UTC',quiet_enabled=True,quiet_start='09:00',quiet_end='18:00')
    now=datetime(2026,10,25,10,tzinfo=UTC)
    assert quiet_until(user,now)==now.replace(hour=18)
    assert quiet_until(replace(user,quiet_exempt_high=True),now,priority='high') is None
    user=User(1,1,'Europe/Helsinki',quiet_enabled=True,quiet_start='23:00',quiet_end='03:30')
    # Spring gap: 03:30 moves to 04:30 local (01:30 UTC), remaining quiet at 04:10.
    assert quiet_until(user,datetime(2027,3,28,1,10,tzinfo=UTC))==datetime(2027,3,28,1,30,tzinfo=UTC)
    # Fall fold: quiet until the second 03:30, never a past retry time.
    assert quiet_until(user,datetime(2026,10,25,1,10,tzinfo=UTC))==datetime(2026,10,25,1,30,tzinfo=UTC)


async def quiet_task(service,clock,*,priority='medium',repeat='none'):
    clock.value=datetime(2026,10,25,19,59,tzinfo=UTC)
    await QuietHoursService(service).configure(1,enabled=True)
    task=await service.create_task(1,'Ночью',clock()+timedelta(minutes=2),repeat,priority=priority)
    clock.advance(minutes=2)
    return task


async def test_main_deferred_once_without_moving_schedule_or_inflating_attempts(service,clock):
    task=await quiet_task(service,clock)
    sender=Sender()
    worker=ReminderWorker(sender,service)
    assert await worker.tick()==0
    assert (await service.get_task(1,task.id)).remind_at==task.remind_at
    async with service.database.read() as connection:
        row=await (await connection.execute('SELECT * FROM deliveries')).fetchone()
        assert row['status']=='pending' and row['attempts']==0 and row['quiet_until'] is not None
    assert await worker.tick()==0
    clock.value=datetime(2026,10,26,5,tzinfo=UTC)
    assert await worker.tick()==1 and await worker.tick()==0
    assert len(sender.calls)==1


@pytest.mark.parametrize('change',['disable','exception','timezone'])
async def test_changes_wake_only_quiet_deferred_deliveries(service,clock,change):
    await quiet_task(service,clock,priority='high')
    worker=ReminderWorker(Sender(),service)
    assert await worker.tick()==0
    if change=='disable':
        await QuietHoursService(service).configure(1,enabled=False)
    elif change=='exception':
        await QuietHoursService(service).configure(1,exempt_high=True)
    else:
        await service.set_timezone(1,'America/New_York')
    assert await worker.tick()==1 and await worker.tick()==0


async def test_user_isolation_delete_and_reschedule(service,clock):
    task=await quiet_task(service,clock)
    await service.create_task(2,'Другой',clock()+timedelta(seconds=1))
    clock.advance(seconds=1)
    sender=Sender()
    worker=ReminderWorker(sender,service)
    assert await worker.tick()==1 and sender.calls[0]['chat_id']==2
    await service.reschedule_task(1,task.id,clock()+timedelta(days=1))
    clock.value=datetime(2026,10,26,5,tzinfo=UTC)
    assert await worker.tick()==0
    await service.delete_task(1,task.id)
    clock.advance(days=2)
    assert await worker.tick()==0


async def test_advance_expiry_keeps_main_and_daily_repeat(service,clock):
    task=await quiet_task(service,clock,repeat='daily')
    await service.set_advances(1,task.id,[5])
    worker=ReminderWorker(Sender(),service)
    assert await worker.tick()==0
    clock.value=datetime(2026,10,26,5,tzinfo=UTC)
    assert await worker.tick()==1
    current=await service.get_task(1,task.id)
    assert current.repeat=='daily' and current.status=='active'
    assert await worker.tick()==0


async def test_summary_respects_quiet_hours_and_releases_without_duplicates(service,clock):
    clock.value=datetime(2026,10,25,19,tzinfo=UTC)
    await QuietHoursService(service).configure(1,enabled=True)
    summaries=SummaryService(service)
    await summaries.configure(1,enabled=True,clock='23:15',send_empty=True)
    clock.advance(hours=1,minutes=15)
    sender=Sender()
    assert await summaries.send_due(sender)==0
    await QuietHoursService(service).configure(1,enabled=False)
    assert await summaries.send_due(sender)==1
    assert await summaries.send_due(sender)==0


async def test_quiet_deferral_survives_restart(tmp_path,clock):
    path=tmp_path/'quiet.sqlite3'
    async with Database(path) as db:
        service=TaskService(db,clock=clock)
        await service.ensure_user(1,1)
        await quiet_task(service,clock)
        assert await ReminderWorker(Sender(),service).tick()==0
    clock.value=datetime(2026,10,26,5,tzinfo=UTC)
    async with Database(path) as db:
        service=TaskService(db,clock=clock)
        assert (await service.get_user(1)).quiet_enabled
        worker=ReminderWorker(Sender(),service)
        assert await worker.tick()==1 and await worker.tick()==0


@pytest.mark.parametrize('start,end',[('23','23'),('25:00','8'),('bad','8')])
async def test_invalid_ranges(service,start,end):
    with pytest.raises(ValidationError):
        await QuietHoursService(service).configure(1,start=start,end=end)


async def test_quiet_configuration_does_not_reset_network_retry(service,clock):
    task=await service.create_task(1,'Сеть',clock()+timedelta(seconds=1))
    clock.advance(seconds=1)
    await service.materialize_due()
    claim=await service.claim_due()
    await service.retry_delivery(claim,delay=3600,reason='offline')
    await QuietHoursService(service).configure(1,enabled=False)
    assert await service.claim_due() is None
    assert (await service.get_task(1,task.id)).status=='active'


async def test_quiet_settings_serialize_with_inflight_delivery(service,clock):
    await service.create_task(1,'Одна отправка',clock()+timedelta(seconds=1))
    clock.advance(seconds=1)
    started,release=asyncio.Event(),asyncio.Event()
    class PausedSender(Sender):
        async def send_message(self,**kwargs):
            started.set()
            await release.wait()
            return await super().send_message(**kwargs)
    worker=ReminderWorker(PausedSender(),service)
    sending=asyncio.create_task(worker.tick())
    await asyncio.wait_for(started.wait(),2)
    changing=asyncio.create_task(QuietHoursService(service).configure(1,enabled=True))
    await asyncio.sleep(0)
    assert not changing.done()
    release.set()
    assert await sending==1
    await changing
    assert await worker.tick()==0


async def test_promoting_queued_task_applies_opted_in_high_exception(service,clock):
    task=await quiet_task(service,clock)
    await QuietHoursService(service).configure(1,exempt_high=True)
    worker=ReminderWorker(Sender(),service)
    assert await worker.tick()==0
    updated=await service.set_priority(1,task.id,'high')
    assert updated.remind_at==task.remind_at
    assert await worker.tick()==1
