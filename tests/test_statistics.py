from contextlib import closing
from datetime import UTC,datetime,timedelta
import sqlite3

import pytest

from bot.database.db import Database
from bot.database import migrations
from bot.services.statistics import StatisticsService
from bot.services.task_service import TaskService,TaskNotFound,ValidationError


async def test_counts_due_percentage_inbox_deleted_and_foreign(service,clock):
    done=await service.create_task(1,'Выполнить',clock()+timedelta(hours=1))
    late=await service.create_task(1,'Опоздать',clock()+timedelta(hours=1))
    removed=await service.create_task(1,'Удалить',clock()+timedelta(hours=1))
    inbox=await service.create_task(1,'Без даты',None)
    await service.create_task(2,'Чужая',clock()+timedelta(hours=1))
    clock.advance(hours=2)
    await service.complete_task(1,done.id)
    await service.complete_task(1,inbox.id)
    await service.delete_task(1,removed.id)
    stats=await StatisticsService(service).get(1)
    assert stats.today==stats.week==stats.month==2
    assert stats.overdue==1 and stats.due_completed==1 and stats.due_total==2 and stats.percent==50
    await service.reschedule_task(1,late.id,clock()+timedelta(days=1))
    assert (await StatisticsService(service).get(1)).percent==100


async def test_recurring_occurrences_counted_once_and_restart(tmp_path,clock):
    path=tmp_path/'stats.sqlite3'
    async with Database(path) as db:
        service=TaskService(db,clock=clock)
        await service.ensure_user(1,1)
        task=await service.create_task(1,'Серия',clock()+timedelta(minutes=1),'daily')
        for _ in range(2):
            clock.value=(await service.get_task(1,task.id)).remind_at
            await service.materialize_due()
            delivery=await service.claim_due()
            await service.acknowledge_delivery(delivery,100)
            await service.complete_delivery(1,delivery.id)
            with pytest.raises(TaskNotFound):
                await service.complete_delivery(1,delivery.id)
        assert (await StatisticsService(service).get(1)).week==2
    async with Database(path) as db:
        stats=await StatisticsService(TaskService(db,clock=clock)).get(1)
        assert stats.today==1 and stats.week==2 and stats.percent==100


async def test_card_completion_before_delivery_and_future_not_in_percentage(service,clock):
    task=await service.create_task(1,'Будущее',clock()+timedelta(days=1),'daily')
    await service.complete_task(1,task.id)
    stats=await StatisticsService(service).get(1)
    assert stats.today==1 and stats.due_total==0 and stats.percent is None
    clock.advance(days=1)
    assert (await StatisticsService(service).get(1)).due_completed==1


async def test_pending_and_sent_same_occurrence_not_double_counted(service,clock):
    await service.create_task(1,'Срок',clock()+timedelta(minutes=1))
    clock.advance(minutes=1)
    stats=StatisticsService(service)
    assert (await stats.get(1)).overdue==1
    await service.materialize_due()
    claim=await service.claim_due()
    await service.acknowledge_delivery(claim,100)
    assert (await stats.get(1)).overdue==1
    await service.snooze_delivery(1,claim.id,clock()+timedelta(hours=1))
    assert (await stats.get(1)).overdue==0


async def test_local_day_week_and_month_boundaries(service,clock):
    await service.set_timezone(1,'Asia/Tokyo')
    clock.value=datetime(2026,10,31,14,59,tzinfo=UTC)
    first=await service.create_task(1,'Октябрь',None)
    await service.complete_task(1,first.id)
    clock.advance(minutes=2)
    second=await service.create_task(1,'Ноябрь',None)
    await service.complete_task(1,second.id)
    stats=await StatisticsService(service).get(1)
    assert stats.today==stats.month==1 and stats.week==2
    with pytest.raises(ValidationError):
        await StatisticsService(service).get(1,'year')


async def test_legacy_completion_backfill_and_idempotence(tmp_path,clock):
    path=tmp_path/'v7.sqlite3'
    with closing(sqlite3.connect(path)) as c,c:
        for migration in migrations.MIGRATIONS[:7]:
            for statement in migration.statements:
                c.execute(statement)
        c.execute('PRAGMA user_version=7')
        c.execute("INSERT INTO users(id,chat_id,timezone,created_at,updated_at) VALUES (1,1,'UTC',1,1)")
        c.execute("INSERT INTO tasks(id,user_id,title,remind_at,repeat,status,timezone,created_at,updated_at) VALUES (1,1,'Старая',1,'none','completed','UTC',1,?)",(int(clock().timestamp()),))
        c.execute("INSERT INTO deliveries(task_id,generation,scheduled_at,due_at,status,next_attempt_at,created_at,updated_at) VALUES (1,0,1,1,'completed',1,1,?)",(int(clock().timestamp()),))
    for _ in range(2):
        async with Database(path) as db:
            stats=await StatisticsService(TaskService(db,clock=clock)).get(1)
            assert stats.today==1
