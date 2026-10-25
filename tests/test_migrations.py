from contextlib import closing
import sqlite3
import asyncio

import pytest

from bot.database import migrations
from bot.database.db import Database


def legacy_database(path):
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.executescript(migrations.SCHEMA)
        connection.execute("PRAGMA user_version=1")
        connection.execute("INSERT INTO users VALUES (1,1,'Asia/Tokyo',0,1,1)")
        connection.execute("INSERT INTO tasks VALUES (1,1,'Сохранить',2000000000,'daily','active','Asia/Tokyo',0,0,1,1)")
        connection.execute("INSERT INTO deliveries(task_id,generation,scheduled_at,due_at,next_attempt_at,created_at,updated_at) VALUES (1,0,1900000000,1900000000,1900000000,1,1)")


async def test_new_database_and_repeated_start(tmp_path):
    path = tmp_path / 'new.sqlite3'
    for _ in range(2):
        async with Database(path) as database:
            async with database.read() as connection:
                assert await migrations.schema_version(connection) == len(migrations.MIGRATIONS)
    assert not (tmp_path / 'backups').exists()


async def test_legacy_upgrades_in_order_and_backup_preserves_data(tmp_path, monkeypatch):
    path = tmp_path / 'legacy.sqlite3'
    legacy_database(path)
    base = migrations.MIGRATIONS[:1]
    monkeypatch.setattr(migrations, 'MIGRATIONS', base + (
        migrations.Migration(2, ('ALTER TABLE tasks ADD COLUMN marker INTEGER NOT NULL DEFAULT 7',)),
        migrations.Migration(3, ('UPDATE tasks SET marker=marker+1',)),
    ))
    for _ in range(2):
        async with Database(path) as database:
            async with database.read() as connection:
                assert await migrations.schema_version(connection) == 3
                assert (await (await connection.execute('SELECT title,marker FROM tasks')).fetchone())[:] == ('Сохранить',8)
                assert (await (await connection.execute('SELECT timezone FROM users')).fetchone())[0] == 'Asia/Tokyo'
                assert (await (await connection.execute('SELECT COUNT(*) FROM deliveries')).fetchone())[0] == 1
    backups = list((tmp_path / 'backups').glob('*.sqlite3'))
    assert len(backups) == 1
    with closing(sqlite3.connect(backups[0])) as connection, connection:
        assert connection.execute('PRAGMA user_version').fetchone()[0] == 1
        assert connection.execute('SELECT title FROM tasks').fetchone()[0] == 'Сохранить'


async def test_failed_or_interrupted_migration_rolls_back_all_changes(tmp_path, monkeypatch):
    path = tmp_path / 'failure.sqlite3'
    legacy_database(path)
    monkeypatch.setattr(migrations, 'MIGRATIONS', migrations.MIGRATIONS[:1] + (
        migrations.Migration(2, ('ALTER TABLE tasks ADD COLUMN marker INTEGER',)),
        migrations.Migration(3, ('INSERT INTO missing_table VALUES (1)',)),
    ))
    with pytest.raises(sqlite3.OperationalError):
        await Database(path).connect()
    with closing(sqlite3.connect(path)) as connection, connection:
        assert connection.execute('PRAGMA user_version').fetchone()[0] == 1
        assert 'marker' not in [row[1] for row in connection.execute('PRAGMA table_info(tasks)')]
        assert connection.execute('SELECT title FROM tasks').fetchone()[0] == 'Сохранить'


async def test_backup_failure_prevents_upgrade(tmp_path, monkeypatch):
    path = tmp_path / 'backup.sqlite3'
    legacy_database(path)
    monkeypatch.setattr(migrations, 'MIGRATIONS', migrations.MIGRATIONS[:1] + (
        migrations.Migration(2, ('ALTER TABLE tasks ADD COLUMN marker INTEGER',)),
    ))
    def fail(*args):
        raise OSError('disk full')
    monkeypatch.setattr(migrations, 'backup_database', fail)
    with pytest.raises(OSError):
        await Database(path).connect()
    with closing(sqlite3.connect(path)) as connection, connection:
        assert connection.execute('PRAGMA user_version').fetchone()[0] == 1


async def test_newer_and_unversioned_databases_rejected(tmp_path):
    for version in (0, 999):
        path = tmp_path / f'{version}.sqlite3'
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute('CREATE TABLE unknown (value TEXT)')
            connection.execute(f'PRAGMA user_version={version}')
        with pytest.raises(RuntimeError):
            await Database(path).connect()


async def test_v1_to_current_preserves_and_assigns_defaults(tmp_path):
    path = tmp_path / 'v1.sqlite3'
    legacy_database(path)
    async with Database(path) as database:
        async with database.read() as connection:
            assert await migrations.schema_version(connection) == len(migrations.MIGRATIONS)
            row = await (await connection.execute('SELECT title,priority FROM tasks')).fetchone()
            assert row[:] == ('Сохранить', 'medium')
            assert (await (await connection.execute('SELECT task_sort FROM users')).fetchone())[0] == 'time'
            assert (await (await connection.execute('SELECT category FROM tasks')).fetchone())[0] == 'other'
            assert (await (await connection.execute('SELECT COUNT(*) FROM categories')).fetchone())[0] == 5


@pytest.mark.parametrize('version', [1,2,3,4,5,6,7,8,9])
async def test_all_released_schemas_upgrade_without_losing_delivery_states(tmp_path, version):
    path = tmp_path / f'old-v{version}.sqlite3'
    with closing(sqlite3.connect(path)) as connection, connection:
        for migration in migrations.MIGRATIONS[:version]:
            for statement in migration.statements:
                connection.execute(statement)
        connection.execute(f'PRAGMA user_version={version}')
        connection.execute("INSERT INTO users(id,chat_id,timezone,blocked,created_at,updated_at) VALUES (11,11,'Asia/Tokyo',1,1,2)")
        connection.execute("INSERT INTO tasks(id,user_id,title,remind_at,repeat,status,timezone,generation,revision,created_at,updated_at) VALUES (15,11,'Серия',2000000000,'daily','active','Asia/Tokyo',3,7,1,2)")
        if version >= 2:
            connection.execute("UPDATE tasks SET priority='high'")
            connection.execute("UPDATE users SET task_sort='priority'")
        if version >= 3:
            connection.execute("UPDATE tasks SET category='work'")
            connection.execute("UPDATE users SET task_category='work'")
        for i, status in enumerate(('pending','sending','sent','completed','cancelled'), 21):
            connection.execute('''INSERT INTO deliveries(id,task_id,generation,scheduled_at,due_at,status,attempts,
                next_attempt_at,lease_until,claim_token,message_id,sent_at,last_error,created_at,updated_at)
                VALUES (?,15,3,?,?,?,2,123,456,'lease',789,100,'offline',1,2)''', (i, 1900000000+i, 1900000000+i, status))
        before = connection.execute('SELECT id,task_id,generation,scheduled_at,due_at,status,attempts,next_attempt_at,lease_until,claim_token,message_id,sent_at,last_error,created_at,updated_at FROM deliveries ORDER BY id').fetchall()
    for _ in range(2):
        async with Database(path) as database:
            async with database.read() as connection:
                after = await (await connection.execute('SELECT * FROM deliveries ORDER BY id')).fetchall()
                assert [tuple(row)[:15] for row in after] == before
                assert all(row['kind'] == 'main' and row['offset_minutes'] == 0 for row in after)
                assert (await (await connection.execute('SELECT COUNT(*) FROM task_advances')).fetchone())[0] == 0
                assert (await (await connection.execute('SELECT generation,revision FROM tasks')).fetchone())[:] == (3,7)
                assert (await (await connection.execute('SELECT timezone,blocked FROM users')).fetchone())[:] == ('Asia/Tokyo',1)
                assert (await (await connection.execute('SELECT priority,category FROM tasks')).fetchone())[:] == ('high' if version >= 2 else 'medium', 'work' if version >= 3 else 'other')
                assert (await (await connection.execute('SELECT task_sort,task_category FROM users')).fetchone())[:] == ('priority' if version >= 2 else 'time', 'work' if version >= 3 else 'all')
                assert (await (await connection.execute('PRAGMA quick_check')).fetchone())[0] == 'ok'


async def test_cancellation_rolls_back_ddl_and_version(tmp_path, monkeypatch):
    import aiosqlite

    path = tmp_path / 'cancelled.sqlite3'
    legacy_database(path)
    original = aiosqlite.Connection.execute
    def interrupted(self, sql, *args):
        if sql.startswith('ALTER TABLE users ADD COLUMN task_sort'):
            raise asyncio.CancelledError
        return original(self, sql, *args)
    monkeypatch.setattr(aiosqlite.Connection, 'execute', interrupted)
    with pytest.raises(asyncio.CancelledError):
        await Database(path).connect()
    with closing(sqlite3.connect(path)) as connection, connection:
        assert connection.execute('PRAGMA user_version').fetchone()[0] == 1
        assert 'priority' not in [row[1] for row in connection.execute('PRAGMA table_info(tasks)')]
