from datetime import UTC, date, datetime, timedelta

import pytest

from bot.services.calendar import CalendarService
from bot.services.task_service import ValidationError


async def test_calendar_local_day_owner_and_projection(service, clock):
    task = await service.create_task(1,'Ежедневно',clock()+timedelta(hours=1),'daily')
    await service.create_task(2,'Чужая',clock()+timedelta(hours=1))
    calendar = CalendarService(service)
    month = await calendar.dates(1,date(2026,10,1),date(2026,11,1))
    assert set(month) == {date(2026,10,day) for day in range(23,32)}
    assert all(len(tasks) == 1 and tasks[0].id == task.id for tasks in month.values())
    assert await calendar.day(1,date(2026,10,22)) == []
    async with service.database.read() as connection:
        assert (await (await connection.execute('SELECT count(*) FROM deliveries')).fetchone())[0] == 0


async def test_weekdays_weekly_timezone_and_deleted(service, clock):
    clock.value = datetime(2026,10,23,20,tzinfo=UTC)
    await service.set_timezone(1,'Asia/Tokyo')
    task = await service.create_task(1,'Суббота',clock()+timedelta(hours=1),'weekly')
    calendar = CalendarService(service)
    assert [t.id for t in await calendar.day(1,date(2026,10,24))] == [task.id]
    assert [t.id for t in await calendar.day(1,date(2026,10,31))] == [task.id]
    assert not await calendar.day(1,date(2026,10,23))
    await service.delete_task(1,task.id)
    assert not await calendar.day(1,date(2026,10,24))


async def test_snoozed_occurrence_and_future_series_both_in_calendar(service, clock):
    task = await service.create_task(1,'Серия',clock()+timedelta(minutes=1),'daily')
    clock.advance(minutes=1)
    await service.materialize_due()
    main = await service.claim_due()
    await service.acknowledge_delivery(main,100)
    await service.snooze_delivery(1,main.id,clock()+timedelta(days=2,hours=4))
    calendar = CalendarService(service)
    day = date(2026,10,25)
    assert len(await calendar.day(1,day)) == 1  # Same task, earliest occurrence on this day.
    assert (await calendar.day(1,date(2026,10,24)))[0].id == task.id


async def test_dst_projection_preserves_wall_clock(service, clock):
    await service.set_timezone(1,'Europe/Helsinki')
    clock.value = datetime(2027,3,27,4,tzinfo=UTC)
    task = await service.create_task(1,'Полдень',datetime(2027,3,27,10,tzinfo=UTC),'daily')
    tasks = await CalendarService(service).day(1,date(2027,3,28))
    assert tasks[0].id == task.id and tasks[0].remind_at == datetime(2027,3,28,9,tzinfo=UTC)


async def test_calendar_range_is_bounded(service):
    with pytest.raises(ValidationError):
        await CalendarService(service).dates(1,date(2026,1,1),date(2027,1,1))
