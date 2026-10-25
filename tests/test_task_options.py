from datetime import timedelta

import pytest

from bot.database.db import Database
from bot.services.task_service import TaskNotFound, TaskService, ValidationError


async def test_priorities_sort_default_and_time_unchanged(service, clock):
    low = await service.create_task(1, 'Низкий', clock() + timedelta(hours=1), priority='low')
    high = await service.create_task(1, 'Высокий', clock() + timedelta(hours=3), 'daily', priority='high')
    medium = await service.create_task(1, 'Средний', clock() + timedelta(hours=2))
    assert medium.priority == 'medium'
    assert [t.id for t in (await service.list_tasks(1))[0]] == [low.id, medium.id, high.id]
    await service.set_task_sort(1, 'priority')
    assert [t.id for t in (await service.list_tasks(1))[0]] == [high.id, medium.id, low.id]
    changed = await service.set_priority(1, high.id, 'low')
    assert changed.remind_at == high.remind_at and changed.repeat == 'daily'
    assert changed.revision > high.revision
    with pytest.raises(TaskNotFound):
        await service.set_priority(2, high.id, 'high')
    with pytest.raises(ValidationError):
        await service.set_priority(1, high.id, 'urgent')
    with pytest.raises(ValidationError):
        await service.list_tasks(1, sort_by='SQL')


async def test_priority_and_sort_persist_and_recurrence_uses_same_label(tmp_path, clock):
    path = tmp_path / 'persistent.sqlite3'
    async with Database(path) as db:
        service = TaskService(db, clock=clock)
        await service.ensure_user(1,1)
        task = await service.create_task(1, 'Серия', clock() + timedelta(minutes=1), 'daily', priority='high')
        await service.set_task_sort(1, 'priority')
    clock.advance(minutes=1)
    async with Database(path) as db:
        service = TaskService(db, clock=clock)
        assert (await service.get_user(1)).task_sort == 'priority'
        await service.materialize_due()
        assert (await service.claim_due()).priority == 'high'
        assert (await service.get_task(1, task.id)).priority == 'high'
