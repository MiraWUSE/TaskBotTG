"""Calendar behaviour through the persistence/service boundary."""

from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
import pytest_asyncio

from bot.database.db import Database
from bot.services.task_service import TaskNotFound, TaskService
from bot.utils.datetime_utils import format_datetime, parse_datetime


@dataclass
class Clock:
    value: datetime

    def __call__(self) -> datetime:
        return self.value


@pytest_asyncio.fixture
async def service_clock(tmp_path):
    clock = Clock(datetime(2026, 1, 1, 0, tzinfo=UTC))
    async with Database(tmp_path / "calendar.sqlite3") as database:
        service = TaskService(database, default_timezone="Europe/Moscow", clock=clock)
        await service.ensure_user(1, 1)
        yield service, clock


@pytest.mark.asyncio
@pytest.mark.parametrize("mark_old_sent", [False, True])
async def test_today_series_is_visible_despite_unresolved_previous_occurrence(
    service_clock, mark_old_sent
):
    service, clock = service_clock
    first_due = datetime(2026, 1, 1, 6, tzinfo=UTC)
    task = await service.create_task(1, "Утренняя зарядка", first_due, "daily")
    clock.value = first_due
    assert await service.materialize_due() == 1
    if mark_old_sent:
        old = await service.claim_due()
        assert old is not None
        assert await service.acknowledge_delivery(old, message_id=101)

    second_due = datetime(2026, 1, 2, 6, tzinfo=UTC)
    clock.value = datetime(2026, 1, 2, 6, 1, tzinfo=UTC)
    assert await service.materialize_due() == 1

    tasks, total = await service.list_tasks(1, today=True)
    assert total == 1
    assert [item.id for item in tasks] == [task.id]
    assert tasks[0].remind_at == second_due
    tasks, total = await service.list_tasks(1)
    assert total == 1
    assert tasks[0].remind_at == first_due


@pytest.mark.asyncio
async def test_today_uses_each_users_local_calendar(service_clock):
    service, clock = service_clock
    clock.value = datetime(2026, 10, 23, 22, 30, tzinfo=UTC)
    await service.ensure_user(2, 2)
    await service.set_timezone(2, "America/New_York")
    same_utc = datetime(2026, 10, 24, 8, 30, tzinfo=UTC)
    moscow_task = await service.create_task(1, "Московский день", same_utc)
    await service.create_task(2, "Нью-йоркский день", same_utc)

    moscow, total = await service.list_tasks(1, today=True)
    assert total == 1
    assert moscow[0].id == moscow_task.id
    new_york, total = await service.list_tasks(2, today=True)
    assert total == 0
    assert new_york == []


@pytest.mark.asyncio
async def test_today_range_is_half_open(service_clock):
    service, clock = service_clock
    start = datetime(2026, 1, 1, 21, tzinfo=UTC)  # 02 January 00:00 Moscow.
    end = datetime(2026, 1, 2, 21, tzinfo=UTC)
    at_start = await service.create_task(1, "Начало дня", start)
    await service.create_task(1, "Начало следующего дня", end)
    clock.value = start
    tasks, total = await service.list_tasks(1, today=True)
    assert total == 1
    assert [item.id for item in tasks] == [at_start.id]


@pytest.mark.asyncio
async def test_timezone_change_preserves_existing_series_and_utc_instant(service_clock):
    service, clock = service_clock
    due = parse_datetime("02.01.2026 09:00", "Europe/Moscow", clock.value)
    task = await service.create_task(1, "Принимать витамины", due, "daily")
    await service.set_timezone(1, "Asia/Tokyo")
    preserved = await service.get_task(1, task.id)
    assert preserved.remind_at == due
    assert preserved.timezone == "Europe/Moscow"
    assert format_datetime(preserved.remind_at, (await service.get_user(1)).timezone) == "02.01.2026 15:00"

    clock.value = due
    await service.materialize_due()
    delivery = await service.claim_due()
    assert delivery is not None
    assert delivery.timezone == "Europe/Moscow"
    await service.acknowledge_delivery(delivery, message_id=101)
    await service.complete_delivery(1, delivery.id)
    following = await service.get_task(1, task.id)
    assert following.remind_at == datetime(2026, 1, 3, 6, tzinfo=UTC)
    assert following.timezone == "Europe/Moscow"


@pytest.mark.asyncio
async def test_explicit_reschedule_adopts_current_timezone_for_series(service_clock):
    service, clock = service_clock
    original_due = parse_datetime("02.01.2026 09:00", "Europe/Moscow", clock.value)
    task = await service.create_task(1, "Проверить почту", original_due, "daily")
    await service.set_timezone(1, "Asia/Tokyo")
    new_due = parse_datetime("02.01.2026 09:00", "Asia/Tokyo", clock.value)
    updated = await service.reschedule_task(1, task.id, new_due)
    assert updated.remind_at == datetime(2026, 1, 2, 0, tzinfo=UTC)
    assert updated.timezone == "Asia/Tokyo"
    assert updated.generation == task.generation + 1

    clock.value = new_due
    await service.materialize_due()
    delivery = await service.claim_due()
    assert delivery is not None
    await service.acknowledge_delivery(delivery, message_id=101)
    await service.complete_delivery(1, delivery.id)
    following = await service.get_task(1, task.id)
    assert following.remind_at == datetime(2026, 1, 3, 0, tzinfo=UTC)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("repeat", "next_due"),
    [
        ("daily", datetime(2026, 11, 3, 6, tzinfo=UTC)),
        ("weekdays", datetime(2026, 11, 3, 6, tzinfo=UTC)),
        ("weekly", datetime(2026, 11, 6, 6, tzinfo=UTC)),
    ],
)
async def test_downtime_coalesces_repeated_occurrences_and_advances_calendar(
    service_clock, repeat, next_due
):
    service, clock = service_clock
    first_due = datetime(2026, 10, 23, 6, tzinfo=UTC)  # Friday, 09:00 Moscow.
    task = await service.create_task(1, "Регулярное напоминание", first_due, repeat)
    clock.value = datetime(2026, 11, 2, 7, tzinfo=UTC)  # Monday, after 09:00.
    assert await service.materialize_due() == 1
    assert await service.materialize_due() == 0
    delivery = await service.claim_due()
    assert delivery is not None
    assert delivery.scheduled_at == first_due
    assert await service.acknowledge_delivery(delivery, message_id=101)
    await service.complete_delivery(1, delivery.id)
    assert await service.claim_due() is None
    following = await service.get_task(1, task.id)
    assert following.remind_at == next_due
    assert following.repeat == repeat
    assert following.status == "active"


@pytest.mark.asyncio
async def test_last_representable_recurrence_is_delivered_without_blocking_other_tasks(
    service_clock,
):
    service, clock = service_clock
    await service.set_timezone(1, "UTC")
    clock.value = datetime(9999, 12, 30, tzinfo=UTC)
    final_due = datetime(9999, 12, 31, 9, tzinfo=UTC)
    terminal = await service.create_task(1, "Последняя дата серии", final_due, "daily")
    independent = await service.create_task(1, "Отдельная задача", final_due)
    clock.value = final_due

    assert await service.materialize_due() == 2
    assert await service.materialize_due() == 0
    delivery = await service.claim_due()
    assert delivery is not None
    assert delivery.task_id == terminal.id
    assert delivery.repeat == "none"
    assert await service.acknowledge_delivery(delivery, message_id=101)
    await service.complete_delivery(1, delivery.id)
    with pytest.raises(TaskNotFound):
        await service.get_task(1, terminal.id)

    other = await service.claim_due()
    assert other is not None
    assert other.task_id == independent.id
    assert await service.acknowledge_delivery(other, message_id=102)
    assert await service.claim_due() is None
