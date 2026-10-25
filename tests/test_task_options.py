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
        await service.set_category(1, task.id, 'work')
        await service.set_category_filter(1, 'work')
    clock.advance(minutes=1)
    async with Database(path) as db:
        service = TaskService(db, clock=clock)
        assert (await service.get_user(1)).task_sort == 'priority'
        assert (await service.get_user(1)).task_category == 'work'
        assert (await service.get_task(1, task.id)).category_label == '💼 Работа'
        await service.materialize_due()
        assert (await service.claim_due()).priority == 'high'
        assert (await service.get_task(1, task.id)).priority == 'high'


async def test_categories_default_change_filter_and_pagination(service, clock):
    first = await service.create_task(1, 'Работа', clock() + timedelta(hours=1), 'daily', category='work')
    other = await service.create_task(1, 'Другое', clock() + timedelta(hours=2))
    assert other.category == 'other' and other.category_label == '📌 Другое'
    assert (await service.list_tasks(1, category='work'))[1] == 1
    changed = await service.set_category(1, other.id, 'work')
    assert changed.remind_at == other.remind_at
    await service.set_category_filter(1, 'work')
    page, total = await service.list_tasks(1, page_size=1, page=1)
    assert total == 2 and page[0].id == other.id
    assert (await service.list_tasks(1, today=True))[1] == 2
    assert (await service.list_tasks(2))[1] == 0
    with pytest.raises(TaskNotFound):
        await service.set_category(2, first.id, 'study')
    with pytest.raises(ValidationError):
        await service.create_task(1, 'Ошибка', clock() + timedelta(hours=1), category='missing')
    await service.set_category_filter(1, 'all')
    assert (await service.get_user(1)).task_category == 'all'


async def test_category_catalog_supports_future_owned_categories(service, clock):
    async with service.database.transaction() as connection:
        await connection.execute("INSERT INTO categories VALUES ('custom-1','🧪 Проект',1)")
    assert ('custom-1', '🧪 Проект') in await service.list_categories(1)
    assert ('custom-1', '🧪 Проект') not in await service.list_categories(2)
    task = await service.create_task(1, 'Своя', clock() + timedelta(hours=1), category='custom-1')
    assert task.category_label == '🧪 Проект'
    with pytest.raises(ValidationError):
        await service.create_task(2, 'Чужая', clock() + timedelta(hours=1), category='custom-1')
    with pytest.raises(ValidationError):
        await service.set_category_filter(2, 'custom-1')
