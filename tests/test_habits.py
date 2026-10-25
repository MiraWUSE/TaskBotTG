from datetime import UTC,datetime,timedelta

import pytest

from bot.database.db import Database
from bot.services.habits import HabitService
from bot.services.task_service import TaskService,TaskNotFound,ValidationError


async def test_daily_streak_duplicate_undo_gap_and_history(service,clock):
    habits=HabitService(service)
    item=await habits.add(1,'Читать')
    assert not item.checked_today and item.streak==0
    for _ in range(3):
        item=await habits.check(1,item.id)
        await habits.check(1,item.id)  # One row per day despite repeated API calls.
        clock.advance(days=1)
    item=await habits.get(1,item.id)
    assert item.streak==item.best==item.total==3 and not item.checked_today
    item=await habits.check(1,item.id)
    assert item.streak==4
    item=await habits.check(1,item.id,done=False)
    assert item.streak==3 and item.total==3
    clock.advance(days=1)
    item=await habits.check(1,item.id)
    assert item.streak==1 and item.best==3 and item.total==4
    assert len(item.history)==4 and item.week==4


async def test_timezone_midnight_stale_callback_and_no_task_side_effects(service,clock):
    await service.set_timezone(1,'Asia/Tokyo')
    clock.value=datetime(2026,10,25,14,59,tzinfo=UTC)
    habits=HabitService(service)
    item=await habits.add(1,'Английский')
    await habits.check(1,item.id,day=item.today,revision=item.revision)
    clock.advance(minutes=2)
    with pytest.raises(ValidationError):
        await habits.check(1,item.id,day=item.today)
    assert (await habits.get(1,item.id)).streak==1
    assert (await service.list_tasks(1))[1]==0
    await service.materialize_due()
    assert await service.claim_due() is None


async def test_foreign_stale_archive_restore_and_rename(service):
    habits=HabitService(service)
    item=await habits.add(1,'Старое')
    with pytest.raises(TaskNotFound):
        await habits.get(2,item.id)
    with pytest.raises(TaskNotFound):
        await habits.check(2,item.id)
    with pytest.raises(TaskNotFound):
        await habits.change(2,item.id,active=False)
    changed=await habits.change(1,item.id,title='Новое',revision=item.revision)
    with pytest.raises(TaskNotFound):
        await habits.check(1,item.id,revision=item.revision)
    archived=await habits.change(1,item.id,active=False,revision=changed.revision)
    assert not archived.active and (await habits.list(1))[1]==0
    assert (await habits.list(1,archived=True))[1]==1
    with pytest.raises(TaskNotFound):
        await habits.check(1,item.id)
    assert (await habits.change(1,item.id,active=True)).active


async def test_restart_dst_and_saved_local_dates(tmp_path,clock):
    path=tmp_path/'habits.sqlite3'
    clock.value=datetime(2027,3,27,9,tzinfo=UTC)
    async with Database(path) as db:
        service=TaskService(db,clock=clock)
        await service.ensure_user(1,1)
        await service.set_timezone(1,'Europe/Helsinki')
        habits=HabitService(service)
        item=await habits.add(1,'Зарядка')
        await habits.check(1,item.id)
    clock.advance(hours=23)
    async with Database(path) as db:
        service=TaskService(db,clock=clock)
        habits=HabitService(service)
        item=await habits.check(1,item.id)
        assert item.streak==2 and item.month==item.month_days==2
        await service.set_timezone(1,'Asia/Tokyo')
        assert (await habits.get(1,item.id)).total==2


@pytest.mark.parametrize('title',['','  ','x'*201,None])
async def test_bad_title(service,title):
    with pytest.raises(ValidationError):
        await HabitService(service).add(1,title)
