from datetime import timedelta

import pytest

from bot.database.db import Database
from bot.services.subtasks import SubtaskService
from bot.services.task_service import TaskNotFound, TaskService, ValidationError


async def test_checklist_progress_rename_delete_and_parent_remains_active(service, clock):
    task = await service.create_task(1,'Экзамен',clock()+timedelta(days=1))
    children = SubtaskService(service)
    a = await children.add(1,task.id,'  Тема 1 ')
    b = await children.add(1,task.id,'Тема 2')
    assert a.title == 'Тема 1'
    await children.change(1,a.id,done=True,revision=a.revision)
    await children.change(1,b.id,done=True)
    parent = await service.get_task(1,task.id)
    assert parent.subtask_total == parent.subtask_done == 2
    assert parent.status == 'active' and parent.remind_at == task.remind_at
    await children.change(1,a.id,title='Повторить ещё раз',done=False)
    await children.change(1,b.id,deleted=True)
    parent = await service.get_task(1,task.id)
    assert parent.subtask_total == 1 and parent.subtask_done == 0
    with pytest.raises(TaskNotFound):
        await children.get(1,b.id)


async def test_foreign_stale_and_deleted_parent(service, clock):
    task = await service.create_task(1,'Своя',clock()+timedelta(days=1))
    children = SubtaskService(service)
    item = await children.add(1,task.id,'Пункт')
    for operation in ('list','get','change','add'):
        with pytest.raises(TaskNotFound):
            if operation == 'list':
                await children.list(2,task.id)
            elif operation == 'get':
                await children.get(2,item.id)
            elif operation == 'change':
                await children.change(2,item.id,done=True)
            else:
                await children.add(2,task.id,'Чужой')
    await children.change(1,item.id,done=True,revision=0)
    with pytest.raises(TaskNotFound):
        await children.change(1,item.id,done=False,revision=0)
    await service.delete_task(1,task.id)
    with pytest.raises(TaskNotFound):
        await children.list(1,task.id)


@pytest.mark.parametrize('title',['',' '*3,'x'*201,None])
async def test_invalid_title(service,clock,title):
    task = await service.create_task(1,'Задача',clock()+timedelta(days=1))
    with pytest.raises(ValidationError):
        await SubtaskService(service).add(1,task.id,title)


async def test_restart_and_recurring_checklist_not_automatically_reset(tmp_path,clock):
    path = tmp_path / 'children.sqlite3'
    async with Database(path) as db:
        service = TaskService(db,clock=clock)
        await service.ensure_user(1,1)
        task = await service.create_task(1,'Серия',clock()+timedelta(hours=1),'daily')
        item = await SubtaskService(service).add(1,task.id,'Постоянный пункт')
        await SubtaskService(service).change(1,item.id,done=True)
        await service.complete_task(1,task.id)
    async with Database(path) as db:
        service = TaskService(db,clock=clock)
        parent = await service.get_task(1,task.id)
        assert parent.subtask_done == 1 and parent.status == 'active'
        assert parent.remind_at == task.remind_at+timedelta(days=1)
