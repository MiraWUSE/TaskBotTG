from contextlib import closing
from datetime import date, timedelta
import sqlite3

import pytest

from bot.database.db import Database
from bot.database import migrations
from bot.services.calendar import CalendarService
from bot.services.reminders import ReminderWorker
from bot.services.subtasks import SubtaskService
from bot.services.task_service import TaskNotFound, TaskService, ValidationError


async def test_undated_task_null_and_excluded_from_all_schedules(service,clock):
    task = await service.create_task(1,'Изучить Docker',None)
    await service.set_advances(1,task.id,[5,60])
    assert task.remind_at is None
    assert (await service.list_tasks(1))[1] == 0
    assert (await service.list_tasks(1,today=True))[1] == 0
    assert (await service.list_tasks(1,inbox=True))[1] == 1
    assert await CalendarService(service).day(1,date(2026,10,23)) == []
    clock.advance(days=100)
    assert await service.materialize_due() == 0 and await service.claim_due() is None
    async with service.database.read() as connection:
        assert (await (await connection.execute('SELECT remind_at FROM tasks')).fetchone())[0] is None
        assert (await (await connection.execute('SELECT COUNT(*) FROM deliveries')).fetchone())[0] == 0


async def test_assign_date_preserves_metadata_and_checklist_then_delivers(service,clock):
    task = await service.create_task(1,'Книга',None,priority='high',category='study')
    child = await SubtaskService(service).add(1,task.id,'Глава 1')
    due = clock()+timedelta(hours=2)
    updated = await service.reschedule_task(1,task.id,due)
    assert updated.remind_at == due and updated.priority == 'high' and updated.category == 'study'
    assert (await SubtaskService(service).list(1,task.id))[0].id == child.id
    assert (await service.list_tasks(1,inbox=True))[1] == 0
    assert (await service.list_tasks(1))[1] == 1
    clock.value = due
    await service.materialize_due()
    assert (await service.claim_due()).task_id == task.id


async def test_undated_owner_completion_and_invalid_repeat(service,clock):
    task = await service.create_task(1,'Без даты',None)
    with pytest.raises(TaskNotFound):
        await service.reschedule_task(2,task.id,clock()+timedelta(hours=1))
    with pytest.raises(ValidationError):
        await service.create_task(1,'Серия без даты',None,'daily')
    await service.rename_task(1,task.id,'Другое имя')
    completed = await service.complete_task(1,task.id)
    assert completed.status == 'completed' and completed.remind_at is None
    assert (await service.list_tasks(1,inbox=True))[1] == 0


async def test_undated_tasks_survive_restart_and_paginate(tmp_path,clock):
    path = tmp_path/'inbox.sqlite3'
    async with Database(path) as db:
        service = TaskService(db,clock=clock)
        await service.ensure_user(1,1)
        for i in range(8):
            await service.create_task(1,f'Задача {i}',None)
    async with Database(path) as db:
        service = TaskService(db,clock=clock)
        tasks,total = await service.list_tasks(1,inbox=True,page=1,page_size=6)
        assert total == 8 and len(tasks) == 2 and all(t.remind_at is None for t in tasks)


def v6_database(path):
    with closing(sqlite3.connect(path)) as connection, connection:
        for migration in migrations.MIGRATIONS[:6]:
            for statement in migration.statements:
                connection.execute(statement)
        connection.execute('PRAGMA user_version=6')
        connection.execute("INSERT INTO users(id,chat_id,timezone,created_at,updated_at) VALUES (1,1,'Europe/Moscow',1,2)")
        connection.execute("INSERT INTO tasks(id,user_id,title,remind_at,repeat,timezone,created_at,updated_at) VALUES (12,1,'Не потерять',2000000000,'daily','Europe/Moscow',1,2)")
        connection.execute('INSERT INTO task_advances VALUES (12,60)')
        connection.execute("INSERT INTO subtasks(id,task_id,title,done,created_at,updated_at) VALUES (27,12,'Пункт',1,1,2)")
        connection.execute("INSERT INTO deliveries(id,task_id,generation,scheduled_at,due_at,next_attempt_at,created_at,updated_at) VALUES (35,12,0,1900000000,1900000000,1900000000,1,2)")


async def test_v7_rebuild_preserves_all_children_and_foreign_keys(tmp_path):
    path = tmp_path/'v6.sqlite3'
    v6_database(path)
    async with Database(path) as db:
        async with db.read() as connection:
            assert (await (await connection.execute('PRAGMA foreign_keys')).fetchone())[0] == 1
            assert await (await connection.execute('PRAGMA foreign_key_check')).fetchall() == []
            assert (await (await connection.execute('SELECT task_id,done FROM subtasks WHERE id=27')).fetchone())[:] == (12,1)
            assert (await (await connection.execute('SELECT task_id FROM deliveries WHERE id=35')).fetchone())[0] == 12
            assert (await (await connection.execute('SELECT task_id FROM task_advances')).fetchone())[0] == 12
        with pytest.raises(sqlite3.IntegrityError):
            async with db.transaction() as connection:
                await connection.execute("INSERT INTO subtasks(task_id,title,created_at,updated_at) VALUES (999,'Ошибка',1,2)")


async def test_failed_rebuild_rolls_back_tasks_and_references(tmp_path,monkeypatch):
    path = tmp_path/'fail-v6.sqlite3'
    v6_database(path)
    seventh = migrations.MIGRATIONS[6]
    monkeypatch.setattr(migrations,'MIGRATIONS',migrations.MIGRATIONS[:6]+(
        migrations.Migration(7,seventh.statements+('INSERT INTO missing VALUES (1)',),rebuild_tables=True),))
    with pytest.raises(sqlite3.OperationalError):
        await Database(path).connect()
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute('PRAGMA user_version').fetchone()[0] == 6
        assert connection.execute('PRAGMA foreign_key_check').fetchall() == []
        assert connection.execute('SELECT title FROM tasks WHERE id=12').fetchone()[0] == 'Не потерять'
        assert connection.execute('SELECT task_id FROM subtasks WHERE id=27').fetchone()[0] == 12
