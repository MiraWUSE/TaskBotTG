import sqlite3

import pytest

from bot.database import migrations
from bot.database.db import Database


def legacy_database(path):
    with sqlite3.connect(path) as connection:
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
    with sqlite3.connect(backups[0]) as connection:
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
    with sqlite3.connect(path) as connection:
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
    with sqlite3.connect(path) as connection:
        assert connection.execute('PRAGMA user_version').fetchone()[0] == 1


async def test_newer_and_unversioned_databases_rejected(tmp_path):
    for version in (0, 999):
        path = tmp_path / f'{version}.sqlite3'
        with sqlite3.connect(path) as connection:
            connection.execute('CREATE TABLE unknown (value TEXT)')
            connection.execute(f'PRAGMA user_version={version}')
        with pytest.raises(RuntimeError):
            await Database(path).connect()
