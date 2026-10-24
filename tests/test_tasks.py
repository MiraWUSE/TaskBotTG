import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from bot.database.db import Database
from bot.services.task_service import TaskNotFound, TaskService, ValidationError


async def deliver(service, task, clock):
    clock.value = task.remind_at
    await service.materialize_due()
    delivery = await service.claim_due()
    assert delivery is not None
    assert await service.acknowledge_delivery(delivery, 100)
    return await service.get_delivery(task.user_id, delivery.id)


async def test_create_task_stores_utc_and_owner(service, clock):
    task = await service.create_task(1, "  Важная   задача  ", clock() + timedelta(hours=1))
    assert task.title == "Важная задача"
    assert task.user_id == 1 and task.status == "active" and task.repeat == "none"
    assert task.remind_at == clock() + timedelta(hours=1)
    assert task.created_at == clock() and task.remind_at.tzinfo == UTC
    assert await service.get_task(1, task.id) == task


@pytest.mark.parametrize("title", ["", "  ", "x" * 201, "a\x00b", None])
async def test_invalid_title(service, clock, title):
    with pytest.raises(ValidationError):
        await service.create_task(1, title, clock() + timedelta(hours=1))


@pytest.mark.parametrize("offset", [-1, 0])
async def test_no_past_tasks(service, clock, offset):
    with pytest.raises(ValidationError):
        await service.create_task(1, "Прошлое", clock() + timedelta(seconds=offset))


async def test_no_naive_time_or_invalid_repeat(service, clock):
    with pytest.raises(ValidationError):
        await service.create_task(1, "Дата", datetime(2026, 10, 25))
    with pytest.raises(ValidationError):
        await service.create_task(1, "Повтор", clock() + timedelta(days=1), "yearly")


async def test_list_sorted_paginated_and_isolated(service, clock):
    tasks = [await service.create_task(1, str(i), clock() + timedelta(minutes=i)) for i in [5, 1, 4, 2, 3]]
    await service.create_task(2, "Чужая", clock() + timedelta(seconds=1))
    page, total = await service.list_tasks(1, page=0, page_size=2)
    assert total == 5 and [task.title for task in page] == ["1", "2"]
    page, _ = await service.list_tasks(1, page=2, page_size=2)
    assert [task.title for task in page] == ["5"]
    assert len(tasks) == 5


async def test_delete_task_cancels_pending(service, clock):
    task = await service.create_task(1, "Удалить", clock() + timedelta(minutes=1))
    clock.advance(minutes=1)
    await service.materialize_due()
    await service.delete_task(1, task.id)
    assert await service.claim_due() is None
    assert await service.list_tasks(1) == ([], 0)
    with pytest.raises(TaskNotFound):
        await service.get_task(1, task.id)


async def test_rename(service, clock):
    task = await service.create_task(1, "Старое", clock() + timedelta(hours=1))
    task = await service.rename_task(1, task.id, "Новое")
    assert task.title == "Новое"


async def test_reschedule_invalidates_claim_and_old_buttons(service, clock):
    task = await service.create_task(1, "Перенос", clock() + timedelta(seconds=1))
    clock.advance(seconds=1)
    await service.materialize_due()
    old_claim = await service.claim_due()
    updated = await service.reschedule_task(1, task.id, clock() + timedelta(hours=1))
    assert updated.generation == task.generation + 1
    assert not await service.claim_is_current(old_claim)
    assert not await service.acknowledge_delivery(old_claim, 100)
    assert await service.claim_due() is None
    with pytest.raises(TaskNotFound):
        await service.get_delivery(1, old_claim.id)
    clock.advance(hours=1)
    assert await service.materialize_due() == 1
    assert (await service.claim_due()).id != old_claim.id


async def test_reschedule_in_past_keeps_original_task(service, clock):
    task = await service.create_task(1, "Будущее", clock() + timedelta(hours=1))
    with pytest.raises(ValidationError):
        await service.reschedule_task(1, task.id, clock())
    assert await service.get_task(1, task.id) == task


async def test_one_time_snooze_reuses_occurrence_without_duplicate(service, clock):
    task = await service.create_task(1, "Отложить", clock() + timedelta(minutes=1))
    delivery = await deliver(service, task, clock)
    next_at = clock() + timedelta(minutes=10)
    await service.snooze_delivery(1, delivery.id, next_at)
    assert (await service.get_task(1, task.id)).remind_at == next_at
    assert await service.claim_due() is None
    with pytest.raises(TaskNotFound):
        await service.snooze_delivery(1, delivery.id, next_at + timedelta(minutes=1))
    clock.advance(minutes=10)
    assert await service.materialize_due() == 0
    snoozed = await service.claim_due()
    assert snoozed.id == delivery.id
    await service.acknowledge_delivery(snoozed, 101)
    assert await service.claim_due() is None
    await service.complete_delivery(1, delivery.id)
    assert await service.list_tasks(1) == ([], 0)


@pytest.mark.parametrize("operation", ["get", "rename", "reschedule", "complete", "delete"])
async def test_foreign_task_rejected(service, clock, operation):
    task = await service.create_task(1, "Секрет", clock() + timedelta(hours=1))
    with pytest.raises(TaskNotFound):
        if operation == "get":
            await service.get_task(2, task.id)
        elif operation == "rename":
            await service.rename_task(2, task.id, "Взлом")
        elif operation == "reschedule":
            await service.reschedule_task(2, task.id, clock() + timedelta(days=1))
        elif operation == "complete":
            await service.complete_task(2, task.id)
        else:
            await service.delete_task(2, task.id)
    assert await service.get_task(1, task.id) == task


@pytest.mark.parametrize("operation", ["get", "complete", "delete", "snooze"])
async def test_foreign_delivery_rejected(service, clock, operation):
    task = await service.create_task(1, "Личное", clock() + timedelta(minutes=1))
    delivery = await deliver(service, task, clock)
    with pytest.raises(TaskNotFound):
        if operation == "get":
            await service.get_delivery(2, delivery.id)
        elif operation == "complete":
            await service.complete_delivery(2, delivery.id)
        elif operation == "delete":
            await service.delete_delivery(2, delivery.id)
        else:
            await service.snooze_delivery(2, delivery.id, clock() + timedelta(minutes=10))
    assert (await service.get_delivery(1, delivery.id)).status == "sent"


async def test_complete_current_recurring_occurrence_keeps_series(service, clock):
    task = await service.create_task(1, "Витамины", clock() + timedelta(minutes=1), "daily")
    delivery = await deliver(service, task, clock)
    await service.complete_delivery(1, delivery.id)
    next_task = await service.get_task(1, task.id)
    assert next_task.status == "active" and next_task.remind_at == clock() + timedelta(days=1)
    with pytest.raises(TaskNotFound):
        await service.complete_delivery(1, delivery.id)
    clock.advance(days=1)
    await service.materialize_due()
    assert (await service.claim_due()).id != delivery.id


async def test_delete_occurrence_keeps_series_stop_cancels_everything(service, clock):
    task = await service.create_task(1, "Серия", clock() + timedelta(minutes=1), "daily")
    delivery = await deliver(service, task, clock)
    await service.delete_delivery(1, delivery.id)
    assert (await service.get_task(1, task.id)).status == "active"
    await service.delete_task(1, task.id)
    clock.advance(days=3)
    assert await service.materialize_due() == 0
    assert await service.claim_due() is None


async def test_recurring_snooze_does_not_move_next_schedule(service, clock):
    task = await service.create_task(1, "Серия", clock() + timedelta(minutes=1), "daily")
    delivery = await deliver(service, task, clock)
    await service.snooze_delivery(1, delivery.id, clock() + timedelta(days=2))
    # Завтрашнее регулярное напоминание раньше отложенного и видно в списке.
    assert (await service.get_task(1, task.id)).remind_at == clock() + timedelta(days=1)
    clock.advance(days=1)
    assert await service.materialize_due() == 1
    assert (await service.claim_due()).id != delivery.id


async def test_complete_recurring_card_skips_only_one_occurrence(service, clock):
    task = await service.create_task(1, "Серия", clock() + timedelta(minutes=1), "daily")
    updated = await service.complete_task(1, task.id)
    assert updated.remind_at == task.remind_at + timedelta(days=1)
    assert updated.status == "active"
    delivery = await deliver(service, updated, clock)
    updated = await service.complete_task(1, task.id)
    assert updated.remind_at == clock() + timedelta(days=1)
    with pytest.raises(TaskNotFound):
        await service.get_delivery(1, delivery.id)


async def test_complete_card_targets_next_schedule_before_later_snooze(service, clock):
    task = await service.create_task(1, "Серия", clock() + timedelta(minutes=1), "daily")
    delivery = await deliver(service, task, clock)
    await service.snooze_delivery(1, delivery.id, clock() + timedelta(days=1, hours=9))
    before = await service.get_task(1, task.id)
    assert before.remind_at == clock() + timedelta(days=1)
    updated = await service.complete_task(1, task.id)
    assert updated.remind_at == clock() + timedelta(days=1, hours=9)
    assert updated.revision > before.revision
    # Отложенный экземпляр остался, регулярное расписание продвинулось на день.
    async with service.database.read() as connection:
        row = await (await connection.execute("SELECT status FROM deliveries WHERE id=?", (delivery.id,))).fetchone()
    assert row["status"] == "pending"


async def test_revision_changes_when_two_occurrences_share_same_time(service, clock):
    task = await service.create_task(1, "Серия", clock() + timedelta(minutes=1), "daily")
    delivery = await deliver(service, task, clock)
    tomorrow = clock() + timedelta(days=1)
    await service.snooze_delivery(1, delivery.id, tomorrow)
    before = await service.get_task(1, task.id)
    after = await service.complete_task(1, task.id)
    assert before.remind_at == after.remind_at == tomorrow
    assert before.revision != after.revision


async def test_today_chooses_regular_schedule_before_later_snooze(service, clock):
    task = await service.create_task(1, "Серия", clock() + timedelta(minutes=1), "daily")
    delivery = await deliver(service, task, clock)
    next_due = clock() + timedelta(days=1)
    await service.snooze_delivery(1, delivery.id, next_due + timedelta(hours=9))
    clock.value = next_due
    tasks, total = await service.list_tasks(1, today=True)
    assert total == 1
    assert tasks[0].remind_at == next_due


async def test_persistent_tasks_and_overdue_after_restart(tmp_path, clock):
    path = tmp_path / "persistent.sqlite3"
    async with Database(path) as database:
        service = TaskService(database, clock=clock)
        await service.ensure_user(1, 1)
        task = await service.create_task(1, "После перезапуска", clock() + timedelta(minutes=1))
    clock.advance(hours=1)
    async with Database(path) as database:
        service = TaskService(database, clock=clock)
        assert (await service.get_task(1, task.id)).title == task.title
        assert await service.materialize_due() == 1
        delivery = await service.claim_due()
        assert delivery.task_id == task.id
        await service.acknowledge_delivery(delivery, 123)
    async with Database(path) as database:
        service = TaskService(database, clock=clock)
        assert await service.materialize_due() == 0
        assert await service.claim_due() is None


async def test_two_connections_claim_only_once(tmp_path, clock):
    path = tmp_path / "concurrent.sqlite3"
    async with Database(path) as first, Database(path) as second:
        s1, s2 = TaskService(first, clock=clock), TaskService(second, clock=clock)
        await s1.ensure_user(1, 1)
        await s1.create_task(1, "Одна отправка", clock() + timedelta(seconds=1))
        clock.advance(seconds=1)
        results = await asyncio.gather(s1.materialize_due(), s2.materialize_due())
        assert sum(results) == 1
        claims = await asyncio.gather(s1.claim_due(), s2.claim_due())
        assert sum(claim is not None for claim in claims) == 1


async def test_expired_lease_recovered_old_worker_cannot_ack(service, clock):
    await service.create_task(1, "Восстановить", clock() + timedelta(seconds=1))
    clock.advance(seconds=1)
    await service.materialize_due()
    first = await service.claim_due(lease_seconds=120)
    assert await service.claim_due() is None
    clock.advance(seconds=121)
    second = await service.claim_due()
    assert first.id == second.id and first.claim_token != second.claim_token
    assert second.attempts == 2
    assert not await service.acknowledge_delivery(first, 1)
    assert await service.acknowledge_delivery(second, 2)


async def test_transaction_rolls_back_on_error(service):
    with pytest.raises(RuntimeError):
        async with service.database.transaction() as connection:
            await connection.execute("UPDATE users SET timezone='UTC' WHERE id=1")
            raise RuntimeError("Откат")
    assert (await service.get_user(1)).timezone == "Europe/Moscow"


@pytest.mark.parametrize("page,size", [(-1, 6), (0, 0), (0, 21)])
async def test_bad_pagination(service, page, size):
    with pytest.raises(ValidationError):
        await service.list_tasks(1, page=page, page_size=size)


async def test_only_private_chat_can_be_registered(service):
    with pytest.raises(ValidationError):
        await service.ensure_user(1, -100)
